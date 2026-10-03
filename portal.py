"""Agent portal: each agent signs in with the username and password the owners made for them and sees only
their own numbers.

It is a separate Flask app mounted at /portal on the same service (app.py), kept apart from the owners'
dashboard on purpose:
- its own session cookie (name, secret key, path /portal), so an agent's sign-in is never valid on the
  dashboard and the dashboard's sign-in means nothing here;
- its own database login (dht_portal) that can only read the activity, deal and contract tables and note
  when someone signed in; it can't read the QuickBooks / Gmail / Drive tokens, coaching notes or anything else;
- no owner pages at all: every query is filtered by the Follow Up Boss user the owners linked to the
  signed-in login (Settings > Agent portal), never by anything the browser sends.

Sign-in: a username and password the owners create for each agent on Settings (stored as a salted hash), with
too many wrong tries locked out for a while. Nobody can sign themselves up.
"""
import hashlib
import hmac
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import psycopg2.extensions
from flask import Flask, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import cte_reports as CTE
import reports as R
import splits as SP

ROLE = "dht_portal"


# What the portal's database login may read. Everything else (tokens, QuickBooks, coaching notes, goals
# plans, ...) stays out of reach even if a bug in the portal let someone run their own query.
READ_TABLES = ("agent_events", "agents", "appointments", "people", "people_stage_history", "deals",
               "action_plan_people", "agent_goals", "cte_activity", "cte_deals", "cte_import_log", "cte_check",
               "compass_payments", "compass_payment_items", "owner_decisions", "drive_contracts", "pull_state")

TABS = [("home", "My numbers"), ("calculator", "Activity Calculator"), ("pay", "My deals & pay"),
        ("leaderboard", "Leaderboard")]


def _derive(secret, label):
    return hmac.new(secret.encode(), label.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- users (the owners' side uses these too)

def ensure_users_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS portal_users (
            email TEXT PRIMARY KEY,
            name TEXT, picture TEXT,
            status TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | disabled
            fub_user_id BIGINT,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            decided_at TIMESTAMPTZ, last_seen TIMESTAMPTZ
        )""")
    cur.execute("ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS password_hash TEXT")  # username = the email column


USERNAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._-@")


def add_login(cur, username, password, fub_user_id):
    """An owner creates (or resets) an agent's username and password; it's approved at once.
    Returns an error message, or None."""
    username = (username or "").strip().lower()
    if not (3 <= len(username) <= 80) or set(username) - USERNAME_OK:
        return "Username: 3-80 letters, numbers, dots, dashes or @."
    if len(password or "") < 8:
        return "Password: at least 8 characters."
    if not fub_user_id:
        return "Pick the agent."
    ensure_users_table(cur)
    cur.execute("""INSERT INTO portal_users (email, status, fub_user_id, password_hash, decided_at)
                   VALUES (%s, 'approved', %s, %s, now())
                   ON CONFLICT (email) DO UPDATE SET status = 'approved', fub_user_id = EXCLUDED.fub_user_id,
                                                     password_hash = EXCLUDED.password_hash, decided_at = now()""",
                (username, fub_user_id, generate_password_hash(password)))
    return None


def users(cur):
    ensure_users_table(cur)
    return R.fetch(cur, """SELECT u.*, a.name AS agent_name FROM portal_users u
                           LEFT JOIN agents a ON a.user_id = u.fub_user_id
                           ORDER BY (u.status = 'pending') DESC, u.requested_at DESC""", {})


def set_user(cur, email, status, fub_user_id=None):
    if status not in ("approved", "disabled", "pending", "delete"):
        return
    if status == "delete":
        cur.execute("DELETE FROM portal_users WHERE email = %s", (email,))
        return
    cur.execute("""UPDATE portal_users SET status = %s, fub_user_id = COALESCE(%s, fub_user_id), decided_at = now()
                   WHERE email = %s""", (status, fub_user_id, email))


# ---------------------------------------------------------------- the portal's own database login

def setup_role(owner_url, secret):
    """Create / refresh the read-only dht_portal login with the owners' connection and return
    (database url for the portal, mode). mode 'role' = the limited login; 'readonly' = the database refused
    to create a login, so the portal uses the main one with every transaction read-only (only the sign-in time
    is then written through the main login)."""
    explicit = os.environ.get("PORTAL_DATABASE_URL")
    if explicit:
        return explicit, "role"
    password = _derive(secret, "dht-portal-db")
    try:
        conn = psycopg2.connect(owner_url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(74220601)")  # one server worker at a time sets it up
        ensure_users_table(cur)
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
        verb = "ALTER" if cur.fetchone() else "CREATE"
        cur.execute(f"{verb} ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %s", (password,))
        cur.execute("SELECT current_database()")
        dbname = cur.fetchone()[0]
        cur.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO {ROLE}')
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
        cur.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
        cur.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}")
        for t in READ_TABLES:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (t,))
            if cur.fetchone()[0]:
                cur.execute(f"GRANT SELECT ON {t} TO {ROLE}")
        cur.execute(f"GRANT SELECT ON portal_users TO {ROLE}")
        cur.execute(f"GRANT UPDATE (last_seen) ON portal_users TO {ROLE}")
        cur.execute(f"ALTER ROLE {ROLE} SET statement_timeout = '30s'")
        conn.close()
    except psycopg2.Error as e:
        print(f"portal: could not set up the {ROLE} login ({str(e).strip()[:200]}); using read-only transactions")
        return owner_url, "readonly"
    u = urlsplit(owner_url)
    host = u.hostname + (f":{u.port}" if u.port else "")
    return urlunsplit((u.scheme, f"{ROLE}:{password}@{host}", u.path, u.query, u.fragment)), "role"


class _Cursor(psycopg2.extensions.cursor):
    """The report code creates its tables if they're missing (CREATE TABLE IF NOT EXISTS ...); the portal
    never does: the tables exist (the dashboard made them) and the portal login isn't allowed to."""
    def execute(self, query, vars=None):
        head = (query if isinstance(query, str) else query.decode()).lstrip()[:12].upper()
        if head.startswith(("CREATE ", "ALTER ", "DROP ")):
            return None
        return super().execute(query, vars)


# ---------------------------------------------------------------- the app

def create(main):
    """The portal app. `main` is app.py (shared filters and the activity statistics)."""
    owner_url = main.DATABASE_URL
    db_url, db_mode = setup_role(owner_url, main.app.secret_key)
    status = {"mode": db_mode}

    p = Flask(__name__, template_folder=main.app.template_folder, static_folder=main.app.static_folder)
    p.secret_key = os.environ.get("PORTAL_SECRET_KEY") or _derive(main.app.secret_key, "dht-portal-session")
    p.config.update(SESSION_COOKIE_NAME="dht_portal", SESSION_COOKIE_PATH="/portal", SESSION_COOKIE_HTTPONLY=True,
                    SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
                    PERMANENT_SESSION_LIFETIME=timedelta(days=14))
    for name in ("money", "dollars", "num", "fmt_date", "pair", "board_item"):
        p.add_template_filter(getattr(main, name), name)
    p.add_template_global(main.qs, "qs")

    @p.context_processor
    def ctx():
        return {"portal": True, "portal_tabs": TABS, "nav": [], "presets": main.PRESETS, "money": main.money,
                "demo": False, "theme": main.current_theme(), "me": getattr(request, "portal_user", None),
                "asset_v": (os.environ.get("RENDER_GIT_COMMIT") or "dev")[:7],
                "month_names": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]}

    @p.after_request
    def headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @contextmanager
    def db():
        kw = {} if db_mode == "role" else {"options": "-c default_transaction_read_only=on"}
        conn = psycopg2.connect(db_url, cursor_factory=_Cursor, **kw)
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()
            conn.close()

    @contextmanager
    def db_write():
        """Only for noting when someone signed in (portal_users.last_seen): the limited login when there is one."""
        conn = psycopg2.connect(db_url if db_mode == "role" else owner_url)
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()
            conn.close()

    def signed_in(view):
        """The agent this browser is signed in as, checked against the database on every request, so
        switching someone off on the dashboard takes effect at once."""
        @wraps(view)
        def wrapped(*args, **kwargs):
            email = session.get("email")
            if not email:
                return redirect(url_for("signin"))
            with db() as cur:
                rows = R.fetch(cur, """SELECT u.email, u.name, u.picture, u.status, u.fub_user_id, a.name AS agent_name
                                       FROM portal_users u LEFT JOIN agents a ON a.user_id = u.fub_user_id
                                       WHERE u.email = %(e)s""", {"e": email})
            u = rows[0] if rows else None
            if not u or u["status"] != "approved" or not u["fub_user_id"]:
                session.clear()
                return redirect(url_for("signin"))
            request.portal_user = u
            return view(*args, **kwargs)
        return wrapped

    # -------------------------------------------------------------- sign in / out

    @p.route("/signin")
    def signin():
        if session.get("email"):
            with db() as cur:
                rows = R.fetch(cur, "SELECT status, fub_user_id FROM portal_users WHERE email = %(e)s", {"e": session["email"]})
            if rows and rows[0]["status"] == "approved" and rows[0]["fub_user_id"]:
                return redirect(url_for("home"))
            session.clear()
        return render_template("portal_signin.html", error=request.args.get("error"))

    failures = {}  # ip -> [time of each wrong try]; this server worker only

    @p.route("/login", methods=["POST"])
    def password_login():
        ip = request.access_route[0] if request.access_route else request.remote_addr
        now = time.time()
        recent = [t for t in failures.get(ip, []) if now - t < 900]
        if len(recent) >= 8:
            return redirect(url_for("signin", error="Too many wrong tries. Wait 15 minutes and try again."))
        username = (request.form.get("username") or "").strip().lower()[:80]
        password = request.form.get("password") or ""
        with db() as cur:
            rows = R.fetch(cur, """SELECT email, status, fub_user_id, password_hash FROM portal_users
                                   WHERE email = %(u)s""", {"u": username})
        u = rows[0] if rows else None
        ok = bool(u and u["password_hash"] and check_password_hash(u["password_hash"], password))
        if not ok or u["status"] != "approved" or not u["fub_user_id"]:
            if not u or not u["password_hash"]:
                check_password_hash(generate_password_hash("x"), password)  # same time whether the user exists or not
            failures[ip] = recent + [now]
            return redirect(url_for("signin", error="Wrong username or password." if not ok else "This login is switched off. Ask the owners."))
        failures.pop(ip, None)
        with db_write() as cur:
            cur.execute("UPDATE portal_users SET last_seen = now() WHERE email = %s", (username,))
        session.clear()
        session.permanent = True
        session["email"] = username
        return redirect(url_for("home"))

    @p.route("/signout", methods=["POST"])
    def signout():
        session.clear()
        return redirect(url_for("signin"))

    @p.route("/theme", methods=["POST"])
    def theme():
        t = request.form.get("theme", "light")
        resp = redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/portal/") else url_for("home"))
        resp.set_cookie("theme", t if t in main.THEMES else "light", max_age=365 * 24 * 3600, path="/portal",
                        samesite="Lax", secure=bool(os.environ.get("RENDER")), httponly=True)
        return resp

    # -------------------------------------------------------------- pages (always the signed-in agent)

    def filters(default):
        rng = main.date_range(default)
        uid = request.portal_user["fub_user_id"]
        return rng, R.Filters(rng["start"], rng["end"], uid, None, main.TEAM_TZ_NAME)

    @p.route("/")
    @signed_in
    def home():
        rng, f = filters("last90")
        uid = f.agent
        with db() as cur:
            ready = R.tables_ready(cur, "people", "agent_events")
            data = {}
            if ready:
                data["info"] = R.agent_info(cur, uid)
                data["funnel"] = R.agent_funnel(cur, f)
                if CTE.ready(cur):
                    data["cte_closed"] = CTE.deal_counts(cur, f.start, f.end, main.cte_agent_for(cur, uid), None)
        return render_template("portal_home.html", ready=ready, rng=rng, **data)

    @p.route("/calculator")
    @signed_in
    def calculator():
        rng, f = filters("year")
        with db() as cur:
            ready = R.tables_ready(cur, "people", "agent_events")
            data = {}
            if ready:
                data = dict(stats=main.activity_stats(cur, f),
                            team=main.activity_stats(cur, R.Filters(f.start, f.end, None, None, f.tz)),
                            agent_name="You")
        return render_template("calculator.html", ready=ready, rng=rng, **data)

    @p.route("/leaderboard")
    @signed_in
    def leaderboard():
        rng = main.date_range("today")
        with db() as cur:
            agents, totals = main.leaderboard_rows(cur, rng["start"], rng["end"])
        return render_template("leaderboard.html", podium=agents[:3], rest=agents[3:], totals=totals,
                               has_data=bool(agents), rng=rng, me_uid=request.portal_user["fub_user_id"])

    @p.route("/pay")
    @signed_in
    def pay():
        today = datetime.now(main.TEAM_TZ).date()
        try:
            year = int(request.args.get("year") or today.year)
        except ValueError:
            year = today.year
        year = min(max(year, 2024), today.year)
        u = request.portal_user
        with db() as cur:
            name = CTE.name_for(cur, u["agent_name"]) if CTE.ready(cur) else None
            data = {"cte_name": name}
            if name:
                rows = [r for r in SP.deal_check(cur, date(year, 1, 1), date(year + 1, 1, 1))
                        if (r["agent"] or "").strip().lower() == name.strip().lower()]
                for r in rows:
                    r["base"] = SP.base(r["gci"]) if r["gci"] else None
                    # the agent's side: what's left after Compass (7.5% + fee) and the team's share on the statement
                    r["mine"] = (r["base"] - r["company"]) if r["base"] and r["company"] is not None and not r.get("zillow_fee") else None
                contracts = [c for c in SP.decided_contracts(cur) if c["agent"].lower() == name.lower()]
                current = SP.contract_for(name, today, rows=contracts)
                miles = SP.milestones(cur)
                m = miles.get((current["agent"], current["since"])) if current else None
                pending = [d for d in CTE.agent_deals(cur, name, limit=60)
                           if (d["status"] or "").lower() in ("pending", "active", "coming soon", "signed", "pre-signed")
                           and (d["primary_agent"] or "").strip().lower() == name.strip().lower()]
                data.update(rows=sorted(rows, key=lambda r: r["close_date"] or date.min, reverse=True), contract=current,
                            milestone=m, bonus_volume=SP.BONUS_VOLUME, pending=pending,
                            totals={"gci": sum(float(r["gci"] or 0) for r in rows),
                                    "volume": sum(float(r["sale_price"] or 0) for r in rows),
                                    "company": sum(r["company"] or 0 for r in rows),
                                    "mine": sum(r["mine"] or 0 for r in rows),
                                    "mine_n": sum(1 for r in rows if r["mine"] is not None)})
        return render_template("portal_pay.html", year=year, year_choices=list(range(today.year, 2023, -1)), **data)

    p.portal_status = status
    return p
