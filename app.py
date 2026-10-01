import csv
import io
import os
import secrets
import time as time_mod
from contextlib import contextmanager
from functools import wraps
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from flask import Flask, Response, abort, render_template, request, session, redirect, url_for

import psycopg2

import cte_reports as CTE
import goals as G
import qbo
import gmail_import
import cte_import
import splits as SP
import home as HOME
import decisions as DEC
import threading
import reports as R

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

DATABASE_URL = os.environ["DATABASE_URL"]
DASHBOARD_USERNAME = os.environ["DASHBOARD_USERNAME"]
DASHBOARD_PASSWORD = os.environ["DASHBOARD_PASSWORD"]

PALETTE = ['#E8A87C', '#7B8FF0', '#8FD3C8', '#F2B84B', '#C99BE0',
           '#7ECF8B', '#E88BA0', '#8FB8E0', '#D9A066', '#9AA5B1']

TEAM_TZ_NAME = os.environ.get("TEAM_TZ", "America/Los_Angeles")
TEAM_TZ = ZoneInfo(TEAM_TZ_NAME)

# Date-filter presets shown on every page, in display order
PRESETS = [("today", "Today"), ("week", "This Week"), ("month", "This Month"),
           ("last30", "Last 30 Days"), ("last90", "Last 90 Days"), ("year", "This Year")]
PRESET_KEYS = {k for k, _ in PRESETS}

# Top menu, modeled on MaverickRE: (menu, [(endpoint, label)])
NAV = [
    ("Business Reports", [("dashboard", "Dashboard"), ("business_overview", "Business Overview"),
                          ("call_time", "Best Call Time Report"), ("cte", "CTE Year by Year"),
                          ("quickbooks", "QuickBooks P&L"), ("compass_invoices", "Compass Invoices"), ("splits_page", "Agent Splits"), ("decisions_page", "Decisions"), ("goals_page", "Goals: Oct–Mar Plan")]),
    ("Sales Reports", [("sales_manager", "Sales Manager Report"), ("appointments", "Appointments Report"),
                       ("lead_source", "Lead Source Report"), ("leaderboard", "Leaderboard")]),
    ("Agent Reports", [("agent_snapshot", "Agent Snapshot")]),
]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# Public pages Intuit requires for the QuickBooks app (EULA + privacy policy)
LEGAL_EFFECTIVE = "September 29, 2026"


@app.route("/legal/terms")
def legal_terms():
    return render_template("legal.html", page="terms", effective=LEGAL_EFFECTIVE,
                           contact=os.environ.get("LEGAL_CONTACT_EMAIL", ""))


@app.route("/legal/privacy")
def legal_privacy():
    return render_template("legal.html", page="privacy", effective=LEGAL_EFFECTIVE,
                           contact=os.environ.get("LEGAL_CONTACT_EMAIL", ""))


# ---------------------------------------------------------------- QuickBooks

def _qbo_redirect_uri():
    # Render terminates HTTPS in front of the app, so build the https URL explicitly
    return os.environ.get("QBO_REDIRECT_URI") or f"https://{request.host}/quickbooks/callback"


@app.route("/quickbooks")
@login_required
def quickbooks():
    """Launch / connect URL for the QuickBooks app: status, Connect, and the P&L pulled so far."""
    with db() as cur:
        conn = qbo.status(cur)
        rows = qbo.pnl_rows(cur) if conn else []
    return render_template("quickbooks.html", configured=qbo.configured(), env=qbo.env(), conn=conn, rows=rows,
                           message=request.args.get("msg"), error=request.args.get("err"))


@app.route("/quickbooks/connect", methods=["POST"])
@login_required
def quickbooks_connect():
    if not qbo.configured():
        return redirect(url_for("quickbooks", err="QuickBooks keys are not set in Render yet."))
    session["qbo_state"] = secrets.token_urlsafe(24)  # checked on the way back (CSRF)
    return redirect(qbo.authorize_url(_qbo_redirect_uri(), session["qbo_state"]))


@app.route("/quickbooks/callback")
@login_required
def quickbooks_callback():
    expected = session.pop("qbo_state", None)
    if not expected or not secrets.compare_digest(request.args.get("state", ""), expected):
        return redirect(url_for("quickbooks", err="Connection check failed (state mismatch). Please try again."))
    if request.args.get("error"):
        return redirect(url_for("quickbooks", err=f"QuickBooks did not connect: {request.args.get('error')}"))
    code, realm = request.args.get("code"), request.args.get("realmId")
    if not code or not realm:
        return redirect(url_for("quickbooks", err="QuickBooks did not return a company. Please try again."))
    try:
        qbo.connect(DATABASE_URL, code, realm, _qbo_redirect_uri())
        months = qbo.pull_pnl(DATABASE_URL)
        entries = qbo.pull_income_detail(DATABASE_URL)
    except qbo.NeedsReconnect as e:
        return redirect(url_for("quickbooks", err=str(e)))
    except Exception as e:  # noqa: BLE001 - show it instead of a 500
        return redirect(url_for("quickbooks", err=f"Connected, but the first pull failed: {e}"))
    return redirect(url_for("quickbooks", msg=f"Connected. Pulled {months} months of Profit & Loss."))


@app.route("/quickbooks/refresh", methods=["POST"])
@login_required
def quickbooks_refresh():
    try:
        months = qbo.pull_pnl(DATABASE_URL)
        entries = qbo.pull_income_detail(DATABASE_URL)
    except qbo.NeedsReconnect as e:
        return redirect(url_for("quickbooks", err=str(e)))
    except Exception as e:  # noqa: BLE001
        return redirect(url_for("quickbooks", err=f"Pull failed: {e}"))
    return redirect(url_for("quickbooks", msg=f"Updated {months} months and {entries} income entries."))


@app.route("/quickbooks/disconnect", methods=["GET", "POST"])
@login_required
def quickbooks_disconnect():
    """Intuit sends people here after disconnecting in QuickBooks; the POST deletes everything."""
    if request.method == "POST":
        qbo.disconnect(DATABASE_URL)
        return redirect(url_for("quickbooks", msg="Disconnected. All QuickBooks data was deleted from the dashboard."))
    with db() as cur:
        conn = qbo.status(cur)
    return render_template("quickbooks.html", configured=qbo.configured(), env=qbo.env(), conn=conn, rows=[],
                           confirm_disconnect=True, message=None, error=None)


# ---------------------------------------------------------------- Gmail: Compass remittances

def _gmail_redirect_uri():
    return os.environ.get("GMAIL_REDIRECT_URI") or f"https://{request.host}/gmail/callback"


@app.route("/compass-invoices")
@login_required
def compass_invoices():
    """Connected mailboxes and every imported Compass remittance, with Assist Contr split out."""
    with db() as cur:
        accounts = gmail_import.accounts(cur)
        payments = gmail_import.payments(cur)
    years = {}
    for p in sorted(payments, key=lambda p: (p["paid_on"] or date.min)):
        y = years.setdefault(p["paid_on"].year if p["paid_on"] else 0,
                             {"emails": 0, "invoices": 0, "gross": 0.0, "net": 0.0, "assist": 0.0,
                              "escrow": 0, "escrow_gross": 0.0, "escrow_net": 0.0,
                              "ytd_income": None, "ytd_as_of": None, "books": None})
        y["emails"] += 1
        # The year's total is the highest YTD Income on its statements (YTD only grows; statements for
        # late-December closings run in January already show the new year's YTD)
        # YTD Income is as of the day the statement was sent, so it counts toward that year
        sent = p["received_at"].astimezone(TEAM_TZ).date() if p["received_at"] else p["paid_on"]
        if p["ytd_income"] is not None and sent:
            yy = years.setdefault(sent.year, {"emails": 0, "invoices": 0, "gross": 0.0, "net": 0.0, "assist": 0.0,
                                              "escrow": 0, "escrow_gross": 0.0, "escrow_net": 0.0,
                                              "ytd_income": None, "ytd_as_of": None, "books": None})
            if yy["ytd_income"] is None or float(p["ytd_income"]) >= yy["ytd_income"]:
                yy["ytd_income"], yy["ytd_as_of"] = float(p["ytd_income"]), sent
        for it in p["items"]:
            amt = float(it["amount"] or 0)
            if it["is_assist"]:
                y["assist"] += amt
            elif p["kind"] == "escrow":
                y["escrow"] += 1
                y["escrow_net"] += amt
                y["escrow_gross"] += float(it["gross"] or amt)
            else:
                y["invoices"] += 1
                y["net"] += amt
                y["gross"] += float(it["gross"] or amt)
    today = datetime.now(TEAM_TZ).date()
    try:
        year = int(request.args.get("year") or today.year)
    except ValueError:
        year = today.year
    with db() as cur:
        for yr, v in years.items():
            books = qbo.year_totals(cur, yr) if yr else None
            v["books"] = books["income"] if books else None
        monthly = gmail_import.monthly_vs_books(cur, year)
        match = gmail_import.books_match(cur, year)
        deals, unmatched = gmail_import.deal_receipts(cur, date(year, 1, 1), date(year + 1, 1, 1))
    deal_filter = request.args.get("deals", "all")
    deal_stats = {"total": len(deals), "with": sum(1 for d in deals if d["receipts"])}
    deal_stats["missing"] = deal_stats["total"] - deal_stats["with"]
    deal_stats["typos"] = sum(1 for d in deals if not d["receipts"] and d["suggestions"])
    return render_template("compass_invoices.html", configured=gmail_import.configured(), accounts=accounts,
                           payments=payments, years=sorted(years.items(), reverse=True),
                           query=gmail_import.REMITTANCE_QUERY, show=request.args.get("show"),
                           year=year, year_choices=list(range(today.year, 2023, -1)), monthly=monthly, match=match,
                           deals=[d for d in deals if deal_filter != "missing" or not d["receipts"]],
                           deal_filter=deal_filter, deal_stats=deal_stats, unmatched=unmatched,
                           message=request.args.get("msg"), error=request.args.get("err"))


@app.route("/compass-invoices/text/<message_id>")
@login_required
def compass_invoice_text(message_id):
    with db() as cur:
        doc = gmail_import.payment_text(cur, message_id)
    if not doc:
        abort(404)
    body = "\n".join([doc["subject"] or "", doc["pdf_name"] or "(no PDF)", "", doc["pdf_text"] or ""])
    return Response(body, mimetype="text/plain")


@app.route("/gmail/connect", methods=["POST"])
@login_required
def gmail_connect():
    if not gmail_import.configured():
        return redirect(url_for("compass_invoices", err="Google keys are not set in Render yet."))
    session["gmail_state"] = secrets.token_urlsafe(24)
    return redirect(gmail_import.authorize_url(_gmail_redirect_uri(), session["gmail_state"]))


@app.route("/gmail/callback")
@login_required
def gmail_callback():
    expected = session.pop("gmail_state", None)
    if not expected or not secrets.compare_digest(request.args.get("state", ""), expected):
        return redirect(url_for("compass_invoices", err="Connection check failed (state mismatch). Please try again."))
    if request.args.get("error") or not request.args.get("code"):
        return redirect(url_for("compass_invoices", err=f"Gmail did not connect: {request.args.get('error', 'no code')}"))
    try:
        email = gmail_import.connect(DATABASE_URL, request.args["code"], _gmail_redirect_uri())
    except (gmail_import.NeedsReconnect, gmail_import.GmailError) as e:
        return redirect(url_for("compass_invoices", err=str(e)))
    gmail_import.pull_in_background(DATABASE_URL, email)
    return redirect(url_for("compass_invoices", msg=f"Connected {email}. Importing the remittance emails now; refresh in a minute."))


@app.route("/gmail/refresh", methods=["POST"])
@login_required
def gmail_refresh():
    email, reimport = request.form.get("email", ""), request.form.get("reimport") == "1"
    if not gmail_import.pull_in_background(DATABASE_URL, email, reimport=reimport,
                                           retry=request.form.get("retry") == "1"):
        return redirect(url_for("compass_invoices", msg="An import is already running. Refresh in a minute."))
    return redirect(url_for("compass_invoices", msg="Import started. It reads every PDF, so give it a minute or two, then refresh."))


@app.route("/gmail/disconnect", methods=["POST"])
@login_required
def gmail_disconnect():
    email = request.form.get("email", "")
    gmail_import.disconnect(DATABASE_URL, email)
    return redirect(url_for("compass_invoices", msg=f"Disconnected {email}. Everything imported from it was deleted."))


@app.route("/splits")
@login_required
def splits_page():
    """Every closed deal's company share (from the Compass receipts) against the agent's contract."""
    today = datetime.now(TEAM_TZ).date()
    try:
        year = int(request.args.get("year") or today.year)
    except ValueError:
        year = today.year
    with db() as cur:
        rows = SP.deal_check(cur, date(year, 1, 1), date(year + 1, 1, 1))
        miles = SP.milestones(cur)
        contracts = SP.contracts_table(cur)
    for c in contracts:
        m = miles.get((c["agent"], c["since"]))
        c["volume"], c["crossed"] = (m["volume"], m["crossed"]) if m else (None, None)
    notes = {c["agent"]: c["note"] for c in contracts}
    agents, total = {}, {"checked": 0, "ok": 0, "under": 0, "over": 0, "company": 0.0, "expected": 0.0, "gap": 0.0}
    for r in rows:
        if r["status"] not in ("ok", "under", "over"):
            continue
        a = agents.setdefault(r["agent"], {"agent": r["agent"], "note": notes.get(r["agent"], ""), "checked": 0, "ok": 0,
                                           "under": 0, "over": 0, "company": 0.0, "expected": 0.0, "gap": 0.0})
        for t in (a, total):
            t["checked"] += 1
            t[r["status"]] += 1
            t["company"] += r["company"]
            t["expected"] += r["expected"]
            t["gap"] += r["gap"]
    return render_template("splits.html", year=year, year_choices=list(range(today.year, 2023, -1)),
                           rows=rows, agents=sorted(agents.values(), key=lambda a: a["gap"]), total=total,
                           contracts=contracts, bonus_volume=SP.BONUS_VOLUME, no_contract=SP.NO_CONTRACT, show=request.args.get("show"))


@app.route("/version")
def version():
    """Which commit is running (Render sets RENDER_GIT_COMMIT), to tell when a deploy is live."""
    return {"commit": (os.environ.get("RENDER_GIT_COMMIT") or "local")[:7]}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if secrets.compare_digest(username, DASHBOARD_USERNAME) and secrets.compare_digest(password, DASHBOARD_PASSWORD):
            session["logged_in"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Incorrect username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    finally:
        cur.close()
        conn.close()


def today_start():
    return datetime.now(TEAM_TZ).replace(hour=0, minute=0, second=0, microsecond=0)


def date_range(default):
    """Date filter shared by every page: a preset (?period=) or a custom
    ?from=YYYY-MM-DD&to=YYYY-MM-DD, both inclusive, in the team's timezone.
    Returns start (inclusive) and end (exclusive) datetimes."""
    today = today_start()
    key = request.args.get("period") or None
    try:
        d_from = date.fromisoformat(request.args["from"])
        d_to = date.fromisoformat(request.args["to"])
    except (KeyError, ValueError):
        d_from = d_to = None

    if key not in PRESET_KEYS and d_from and d_to:
        if d_to < d_from:
            d_from, d_to = d_to, d_from
        key = "custom"
        start = datetime.combine(d_from, time(), TEAM_TZ)
        end = datetime.combine(d_to + timedelta(days=1), time(), TEAM_TZ)
    else:
        if key not in PRESET_KEYS:
            key = default
        start = {
            "today": today,
            "week": today - timedelta(days=today.weekday()),
            "month": today.replace(day=1),
            "last30": today - timedelta(days=29),
            "last90": today - timedelta(days=89),
            "year": today.replace(month=1, day=1),
        }[key]
        end = today + timedelta(days=1)

    last_day = end - timedelta(days=1)
    return {"key": key, "start": start, "end": end,
            "from": start.date().isoformat(), "to": last_day.date().isoformat(),
            "label": f"{start:%m/%d/%Y} - {last_day:%m/%d/%Y}"}


def page_filters(default_period, agent_arg="agent"):
    """Date range + optional agent/source from the query string."""
    rng = date_range(default_period)
    agent = request.args.get(agent_arg, type=int)
    source = request.args.get("source") or None
    return rng, R.Filters(rng["start"], rng["end"], agent, source, TEAM_TZ_NAME)


FUB_APP_URL = os.environ.get("FUB_APP_URL", "https://app.followupboss.com").rstrip("/")


@app.template_global()
def fub_lead_url(person_id):
    """The lead's page in Follow Up Boss (set FUB_APP_URL if the account has its own address)."""
    return f"{FUB_APP_URL}/2/people/view/{person_id}"


@app.template_global()
def qs(**changes):
    """Current query string with some keys changed; None removes a key."""
    args = {k: v for k, v in request.args.items()}
    for k, v in changes.items():
        if v is None:
            args.pop(k, None)
        else:
            args[k] = v
    return "?" + urlencode(args)


@app.template_filter("money")
def money(v):
    v = float(v or 0)
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M".replace(".0M", "M")
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,.0f}"


@app.template_filter("dollars")
def dollars(v):
    """Exact whole dollars for accounting numbers: $12,345 / -$1,200."""
    v = float(v or 0)
    return f"{'-' if v < 0 else ''}${abs(v):,.0f}"


@app.template_filter("num")
def num(v):
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{int(v or 0):,}"


@app.template_filter("fmt_date")
def fmt_date(dt, with_time=False):
    dt = dt.astimezone(TEAM_TZ)
    return dt.strftime("%b %d %Y %I:%M %p" if with_time else "%b %d %Y")


@app.template_filter("pair")
def pair(v):
    """x -> (x, x), for select options whose value and label match."""
    return (v, v)


@app.template_filter("board_item")
def board_item(row, key):
    """A report row -> {name, value} for the top-5 board macro."""
    return {"name": row["name"], "value": row[key]}


@app.context_processor
def layout_context():
    return {"nav": NAV, "presets": PRESETS, "money": money,
            "month_names": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]}


def hide_future(values, year):
    """Blank out months that haven't happened yet, so charts don't drop to 0."""
    today = today_start()
    if year != today.year:
        return values
    return [v if i < today.month else None for i, v in enumerate(values)]


def cte_agent_for(cur, fub_uid):
    """CTE name for a FUB agent filter (None = whole team). An agent who isn't
    in CTE gets a name that matches nothing, so their CTE numbers are 0."""
    if not fub_uid:
        return None
    return CTE.name_for(cur, R.agent_names(cur).get(fub_uid)) or "(not in CTE)"


def filter_options(cur, agents=True, sources=True):
    opts = {}
    if agents:
        opts["agent_options"] = R.agent_options(cur)
    if sources:
        opts["source_options"] = R.source_options(cur)
    return opts


# ---------------------------------------------------------------- Leaderboard

def duration_label(minutes):
    minutes = minutes or 0
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


LEADERBOARD_COUNTS = """
    SELECT user_id, MAX(agent_name) AS agent_name,
           COUNT(*) FILTER (WHERE event_type = 'appt') AS appts,
           COUNT(*) FILTER (WHERE event_type = 'conversation') AS conversations,
           COALESCE(SUM(duration_min) FILTER (WHERE event_type = 'conversation'), 0) AS conversations_dur_min,
           COUNT(*) FILTER (WHERE event_type = 'attempt') AS attempts,
           COUNT(*) FILTER (WHERE event_type = 'text') AS texts,
           COUNT(*) FILTER (WHERE event_type = 'zillow') AS zillow,
           COUNT(*) FILTER (WHERE event_type = 'email') AS emails
    FROM agent_events
    WHERE created_at >= %(start)s AND created_at < %(end)s
    GROUP BY user_id
"""


def get_agents(start, end):
    """Every active agent on the FUB roster (lenders excluded), with 0s for
    no activity, like FUB's own leaderboard. Anyone with activity who isn't
    on the roster still shows up."""
    with db() as cur:
        if R.tables_ready(cur, "agents"):
            rows = R.fetch(cur, f"""
                WITH c AS ({LEADERBOARD_COUNTS}),
                     r AS (SELECT user_id, name, picture_url FROM agents
                           WHERE LOWER(COALESCE(status, '')) IN ('', 'active')
                             AND LOWER(COALESCE(role, '')) <> 'lender')
                SELECT COALESCE(r.user_id, c.user_id) AS user_id, COALESCE(r.name, c.agent_name) AS agent_name, r.picture_url,
                       c.appts, c.conversations, c.conversations_dur_min, c.attempts, c.texts, c.zillow, c.emails
                FROM r FULL JOIN c ON c.user_id = r.user_id
            """, {"start": start, "end": end})
        else:
            rows = R.fetch(cur, LEADERBOARD_COUNTS, {"start": start, "end": end})

    agents = []
    for r in rows:
        appts, conversations, attempts = r["appts"] or 0, r["conversations"] or 0, r["attempts"] or 0
        texts, zillow, emails = r["texts"] or 0, r["zillow"] or 0, r["emails"] or 0
        agents.append({
            "uid": r["user_id"], "name": r["agent_name"], "picture": r.get("picture_url") or "",
            "initials": "".join(w[0] for w in r["agent_name"].split()[:2]).upper(),
            "appts": appts, "conversations": conversations,
            "conversations_dur_label": duration_label(r["conversations_dur_min"]),
            "attempts": attempts, "texts": texts, "zillow": zillow, "emails": emails,
            "score": appts * 500 + conversations * 100 + attempts * 10 + texts * 2 + emails * 1 + zillow * 5,
        })
    agents.sort(key=lambda a: (-a["score"], a["name"].lower()))
    for i, a in enumerate(agents):
        a["rank"] = i + 1
        a["avatar_bg"] = PALETTE[i % len(PALETTE)]

    totals = {
        "appts": sum(a["appts"] for a in agents),
        "conversations": sum(a["conversations"] for a in agents),
        "conversations_dur_label": duration_label(sum(r["conversations_dur_min"] or 0 for r in rows)),
        "attempts": sum(a["attempts"] for a in agents),
        "texts": sum(a["texts"] for a in agents),
        "zillow": sum(a["zillow"] for a in agents),
        "emails": sum(a["emails"] for a in agents),
    }
    return agents, totals


@app.route("/leaderboard")
@login_required
def leaderboard():
    rng = date_range("today")
    agents, totals = get_agents(rng["start"], rng["end"])
    return render_template("leaderboard.html", podium=agents[:3], rest=agents[3:],
                           totals=totals, has_data=len(agents) > 0, rng=rng)


# ---------------------------------------------------------------- Dashboard

DASHBOARD_TABS = {"pipeline", "response", "source"}


@app.route("/")
@app.route("/dashboard")
@login_required
def dashboard():
    tab = request.args.get("tab", "pipeline")
    if tab not in DASHBOARD_TABS:
        tab = "pipeline"
    rng, f = page_filters("last30")
    data = {}
    started = time_mod.perf_counter()
    HOME.reset_timings()
    with db() as cur:
        overview = HOME.overview(cur, today_start(), TEAM_TZ_NAME, _year_totals)
        t_overview = time_mod.perf_counter()
        ready = R.tables_ready(cur, "people")
        if ready:
            data.update(filter_options(cur))
            if tab == "pipeline":
                data["pipeline"] = R.pipeline_health(cur, f)
            elif tab == "response":
                data["response"] = R.lead_response(cur, f, min(f.end, datetime.now(TEAM_TZ)))
            else:
                data["sources"] = R.best_sources(cur, f)
    t_lead = time_mod.perf_counter()
    html = render_template("dashboard.html", tab=tab, ready=ready, rng=rng, home=overview,
                           decider=session.get("decider", ""), **data)
    # how long each part took, to find what makes the page slow (browser dev tools > Network > Timing)
    parts = {**HOME.timings(), "lead_health": t_lead - t_overview, "render": time_mod.perf_counter() - t_lead,
             "total": time_mod.perf_counter() - started}
    app.logger.warning("dashboard timing: %s", ", ".join(f"{k}={v:.2f}s" for k, v in sorted(parts.items(), key=lambda x: -x[1])))
    resp = Response(html + "\n<!-- timing: " + ", ".join(f"{k}={v:.2f}s" for k, v in sorted(parts.items(), key=lambda x: -x[1])) + " -->")
    resp.headers["Server-Timing"] = ", ".join(f"{k.replace(' ', '_')};dur={v * 1000:.0f}" for k, v in parts.items())
    return resp


# ---------------------------------------------------------------- Decisions on Needs-attention items

def _apply_saved_decisions():
    try:
        with db() as cur:
            DEC.apply_fixes(cur)
    except Exception:
        app.logger.exception("applying saved decisions")


def _keep_dashboard_warm():
    """Rebuild the dashboard numbers every 90 seconds (they're reused for 120), and every few minutes load
    the CTE page once, so its 5-minute appointment numbers are always ready: a visit never waits."""
    import time as _t
    _t.sleep(5)
    n = 0
    while True:
        try:
            with db() as cur:
                HOME.refresh(cur, today_start(), TEAM_TZ_NAME, _year_totals)
            if n % 3 == 0:
                client = app.test_client()
                with client.session_transaction() as s:
                    s["logged_in"] = True
                client.get("/cte")
        except Exception:
            app.logger.exception("dashboard warm-up")
        n += 1
        _t.sleep(90)


_apply_saved_decisions()
_warm = {"pid": None}


@app.before_request
def _start_warm_thread():
    """Start the background refresh in each server process (a thread started before the server forks
    its worker processes doesn't carry over, so it's started on the first visit instead)."""
    if _warm["pid"] != os.getpid() and os.environ.get("DASHBOARD_WARM", "1") == "1":
        _warm["pid"] = os.getpid()
        threading.Thread(target=_keep_dashboard_warm, name="dashboard-warm", daemon=True).start()


@app.route("/decisions/add", methods=["POST"])
@login_required
def decisions_add():
    """Joe (or the office) answers a Needs-attention item: a dropdown choice and/or a comment."""
    f = request.form
    by = (f.get("by") or "").strip()[:60]
    if by:
        session["decider"] = by
    if f.get("key") and (f.get("choice") or (f.get("comment") or "").strip()):
        with db() as cur:
            DEC.add(cur, f["key"][:300], (f.get("title") or "")[:500], (f.get("detail") or "")[:4000],
                    (f.get("choice") or "")[:200], (f.get("comment") or "").strip()[:4000], by)
            DEC.apply_fixes(cur)
        HOME.clear_cache()
    return redirect(url_for("dashboard", saved=f.get("key")) + "#attention")


@app.route("/decisions")
@login_required
def decisions_page():
    status = request.args.get("status")
    status = status if status in dict(DEC.STATUSES) else None
    with db() as cur:
        rows = DEC.all_(cur, status)
        counts = {s: len(DEC.all_(cur, s)) for s, _ in DEC.STATUSES}
    return render_template("decisions.html", rows=rows, status=status, statuses=DEC.STATUSES, counts=counts)


@app.route("/decisions/<int:decision_id>/status", methods=["POST"])
@login_required
def decisions_status(decision_id):
    status = request.form.get("status")
    if status in dict(DEC.STATUSES):
        with db() as cur:
            DEC.set_status(cur, decision_id, status, (request.form.get("fix_note") or "").strip()[:2000])
        HOME.clear_cache()
    return redirect(url_for("decisions_page", status=request.form.get("back") or None))


@app.route("/funnel")
@login_required
def funnel():
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------- Sales Manager

MANAGER_VIEWS = {"funnel", "stages", "outreach"}


@app.route("/sales-manager")
@login_required
def sales_manager():
    view = request.args.get("view", "funnel")
    if view not in MANAGER_VIEWS:
        view = "funnel"
    rng, f = page_filters("last30")
    today = today_start()
    with db() as cur:
        ready = R.tables_ready(cur, "people", "appointments")
        data = {}
        if ready:
            rows = R.team_overview(cur, f)
            kpis = R.sales_manager_kpis(cur, f, today)
            top = R.top_performers(cur, f, today)
            has_cte = CTE.ready(cur)
            if has_cte:
                # CTE deal numbers next to the FUB ones (the owner asked for both)
                cte_agent = cte_agent_for(cur, f.agent)
                y = R.ytd(f, today)
                for band, ff, prev in (("period", f, f.previous()), ("ytd", y, y.year_earlier())):
                    c = CTE.deal_counts(cur, ff.start, ff.end, cte_agent, f.source)
                    pc = CTE.deal_counts(cur, prev.start, prev.end, cte_agent, f.source)
                    c["chg_written"] = R.change(c["written"], pc["written"])
                    c["chg_closed"] = R.change(c["closed"], pc["closed"])
                    c["conversion"] = R.pct(c["closed"] + c["pending"], kpis[band]["new_leads"], 2)
                    kpis[band]["cte"] = c
                by_agent = CTE.deals_by_agent(cur, f.start, f.end, f.source)
                for r in rows:
                    r["cte"] = by_agent.get(r["name"].lower(), {"written": 0, "pending": 0, "closed": 0})
                top["cte_closers"] = CTE.top_deals(cur, today.replace(month=1, day=1), today + timedelta(days=1),
                                                   "closed", f.source)
                top["cte_written"] = CTE.top_deals(cur, today - timedelta(days=90), today + timedelta(days=1),
                                                   "written", f.source)
            data = dict(
                kpis=kpis, rows=rows, has_cte=has_cte,
                avg=R.team_average(rows, ["total_leads", "appts", "held", "held_pct", "accepted",
                                          "accepted_pct", "pending", "closed", "conversion",
                                          "calls", "conversations", "texts", "emails"]),
                top=top,
                **filter_options(cur))
            if has_cte and rows:
                data["avg"]["cte"] = {k: round(sum(r["cte"][k] for r in rows) / len(rows), 2)
                                      for k in ("written", "pending", "closed")}
    return render_template("sales_manager.html", view=view, ready=ready, rng=rng,
                           buckets=R.BUCKETS, **data)


# ---------------------------------------------------------------- Appointments

def _appt_args():
    view_by = request.args.get("view_by", "created")
    if view_by not in R.APPT_DATE_FIELDS:
        view_by = "created"
    status = request.args.get("status", "all")
    if status not in R.APPT_STATUSES:
        status = "all"
    stage_mode = request.args.get("stage_at", "current")
    if stage_mode not in R.STAGE_MODES:
        stage_mode = "current"
    return view_by, request.args.get("type") or None, status, request.args.get("stage") or None, stage_mode


@app.route("/appointments")
@login_required
def appointments():
    view_by, appt_type, status, stage, stage_mode = _appt_args()
    page = max(request.args.get("page", 1, type=int), 1)
    rng, f = page_filters("last90")
    with db() as cur:
        ready = R.tables_ready(cur, "appointments")
        data = {}
        if ready:
            rows, total = R.appointment_list(cur, f, view_by, appt_type, status, page, stage=stage,
                                             stage_mode=stage_mode)
            data = dict(kpis=R.appointment_kpis(cur, f, view_by, appt_type, stage, stage_mode), rows=rows,
                        total=total, pages=max((total + 24) // 25, 1),
                        type_choices=[("", "All types")] + [(t, t) for t in R.appt_type_options(cur)],
                        stage_choices=[("", "All stages")] + [
                            (s, s) for s in R.stage_options(cur, with_history=stage_mode == "appt")],
                        **filter_options(cur))
    return render_template("appointments.html", ready=ready, rng=rng, view_by=view_by, appt_type=appt_type,
                           status=status, page=page, stage_mode=stage_mode, **data)


@app.route("/lead/<int:person_id>")
@login_required
def lead(person_id):
    with db() as cur:
        data = R.lead_detail(cur, person_id)
    if not data["person"] and not data["items"]:
        return render_template("lead.html", missing=True, person_id=person_id), 404
    back = request.args.get("back") or ""
    if not back.startswith("/") or back.startswith("//"):  # only links back into the dashboard
        back = url_for("appointments")
    return render_template("lead.html", missing=False, person_id=person_id, back=back, **data)


@app.route("/appointments.csv")
@login_required
def appointments_csv():
    view_by, appt_type, status, stage, stage_mode = _appt_args()
    _, f = page_filters("last90")
    with db() as cur:
        rows, _ = R.appointment_list(cur, f, view_by, appt_type, status, 1, per_page=100000, stage=stage,
                                     stage_mode=stage_mode)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Agent(s)", "Lead", "Created", "Appointment Time",
                "Stage at Appointment" if stage_mode == "appt" else "Current Stage", "Created By",
                "Type", "Held / Not Held", "Outcome in FUB", "Lead Source"])
    for r in rows:
        w.writerow([r["agent_names"], r["lead_name"],
                    r["created_at"].astimezone(TEAM_TZ).strftime("%Y-%m-%d") if r["created_at"] else "",
                    r["start_at"].astimezone(TEAM_TZ).strftime("%Y-%m-%d %H:%M") if r["start_at"] else "",
                    r["stage"], r["created_by_name"], r["type"],
                    {"held": "Held", "not_held": "Not Held"}.get(r["status"], "No outcome yet"), r["outcome"],
                    r["source"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=appointments.csv"})


# ---------------------------------------------------------------- Lead Source

@app.route("/lead-source")
@login_required
def lead_source():
    rng, f = page_filters("year")
    year = (f.end - timedelta(days=1)).year
    with db() as cur:
        ready = R.tables_ready(cur, "people")
        data = {}
        if ready:
            data = dict(
                ov=R.lead_source_overview(cur, f), funnel=R.lead_funnel(cur, f), top_agents=R.top_agents_closed(cur, f),
                stages=R.grouped_stages(R.stage_counts(cur, f)), months=R.leads_by_month(cur, f),
                trend={"year": year,
                       "leads": [hide_future(R.monthly_series(cur, f, year, "leads"), year),
                                 R.monthly_series(cur, f, year - 1, "leads")],
                       "deals": [hide_future(R.monthly_series(cur, f, year, "deals"), year),
                                 R.monthly_series(cur, f, year - 1, "deals")]},
                **filter_options(cur))
            if CTE.ready(cur):  # CTE deal numbers next to the FUB ones
                cte_agent = cte_agent_for(cur, f.agent)
                c = CTE.deal_counts(cur, f.start, f.end, cte_agent, f.source)
                prev = f.previous()
                pc = CTE.deal_counts(cur, prev.start, prev.end, cte_agent, f.source)
                leads = data["ov"]["leads"]
                c["closed_pct"] = R.pct(c["closed"], leads, 2)
                c["closed_pending_pct"] = R.pct(c["closed"] + c["pending"], leads, 2)
                c["chg_deals"] = R.change(c["closed"] + c["pending"], pc["closed"] + pc["pending"])
                data["ov"]["cte"] = c
                data["top_agents_cte"] = CTE.top_deals(cur, f.start, f.end, "closed", f.source)
    return render_template("lead_source.html", ready=ready, rng=rng, **data)


# ---------------------------------------------------------------- Business Overview

def _year_totals(cur, year, today):
    """Team gross (CTE) and net (QuickBooks) for the year against the owner's goals, with pace."""
    if year < today.year:
        pace = 1.0
    elif year > today.year:
        pace = 0.0
    else:
        start = date(year, 1, 1)
        pace = ((today.date() - start).days + 1) / ((date(year + 1, 1, 1) - start).days)
    income = CTE.year_income(cur, year)
    books = qbo.year_totals(cur, year)
    goals = CTE.year_goals(cur, year)

    def vs_goal(actual, goal):
        if goal is None or actual is None:
            return None
        return {"goal": goal, "diff": actual - goal, "pct": actual / goal * 100 if goal else 0,
                "pace_target": goal * pace, "pace_diff": actual - goal * pace}

    net = books["net_income"] if books else None
    margin = None
    if net is not None:  # net as a share of gross: of total GCI (CTE) and of the company's booked income (QuickBooks)
        margin = {"of_gci": net / income["gross"] * 100 if income["gross"] else None,
                  "of_income": net / books["income"] * 100 if books["income"] else None}
    return {"year": year, "gross": income["gross"], "pending": income["pending"], "books": books, "net": net,
            "margin": margin,
            "goals": goals, "gross_vs": vs_goal(income["gross"], goals["gross"]), "net_vs": vs_goal(net, goals["net"]),
            "pace_pct": pace * 100}


@app.route("/goals")
@login_required
def goals_page():
    with db() as cur:
        data = G.page(cur, TEAM_TZ)
    return render_template("goals.html", months=G.PLAN_MONTHS, **data)


@app.route("/goals/groups", methods=["POST"])
@login_required
def goals_groups():
    valid = {g for g, _ in G.GROUPS}
    groups = {int(k[4:]): v for k, v in request.form.items() if k.startswith("grp_") and k[4:].isdigit() and v in valid}
    with db() as cur:
        G.save_groups(cur, groups)
    return redirect(url_for("goals_page") + "#settings")


@app.route("/goals/inputs", methods=["POST"])
@login_required
def goals_inputs():
    """Numbers no connected system records: open houses per week, ad spend per month."""
    with db() as cur:
        for k, v in request.form.items():
            parts = k.split("|")  # "<key>|<YYYY-MM-DD>"
            if len(parts) != 2 or parts[0] not in ("open_houses", "ppc_google", "ppc_meta", "ppc_youtube"):
                continue
            try:
                period = date.fromisoformat(parts[1])
                value = float(v.replace("$", "").replace(",", "")) if v.strip() else None
            except ValueError:
                continue
            G.save_input(cur, parts[0], period, value)
    return redirect(url_for("goals_page") + (request.form.get("anchor") or ""))


@app.route("/business-overview/goals", methods=["POST"])
@login_required
def business_goals():
    year = request.form.get("year", type=int)

    def amount(name):
        v = (request.form.get(name) or "").replace("$", "").replace(",", "").strip().lower()
        mult = 1_000_000 if v.endswith("m") else 1_000 if v.endswith("k") else 1
        try:
            return float(v.rstrip("mk")) * mult if v else None
        except ValueError:
            return None
    if year:
        with db() as cur:
            CTE.save_year_goals(cur, year, amount("gross_goal"), amount("net_goal"))
    return redirect(url_for("business_overview", year=year) + "#year-totals")


@app.route("/business-overview")
@login_required
def business_overview():
    today = today_start()
    year = request.args.get("year", today.year, type=int)
    # Top Performers periods, all rendered at once and switched in the browser: (key, label, since, until)
    q_start = today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1)
    lq_start = q_start.replace(year=q_start.year - 1, month=10) if q_start.month == 1 else q_start.replace(month=q_start.month - 3)
    top_periods = [("30", "Last 30 Days", today - timedelta(days=30), None),
                   ("quarter", f"Last Quarter (Q{(lq_start.month - 1) // 3 + 1} {lq_start.year})", lq_start, q_start),
                   ("ytd", f"YTD {today.year}", today.replace(month=1, day=1), None)]
    source = request.args.get("source") or None
    with db() as cur:
        if CTE.ready(cur):
            # The team's deal log lives in CTE; FUB deals are barely used
            agent = request.args.get("cte_agent") or None
            options = CTE.agent_options(cur)
            agent = agent if agent in options else None
            months = CTE.business_months(cur, year, agent, source)
            # FUB deals as extra rows under the CTE ones (the owner asked for both)
            fub_uid = next((uid for uid, name in R.agent_names(cur).items()
                            if agent and (CTE.name_for(cur, name, options) or "").lower() == agent.lower()), None)
            if agent and not fub_uid:
                fub_uid = -1  # agent isn't in FUB: show zeros
            fub = R.business_months(cur, R.Filters(today, today, fub_uid, source, TEAM_TZ_NAME), year) \
                if R.table_exists(cur, "deals") else [{"accepted": 0, "deals": 0, "volume": 0}] * 12
            for m, fm in zip(months, fub):
                m.update(fub_accepted=fm["accepted"], fub_deals=fm["deals"], fub_volume=float(fm["volume"]))
            metrics = CTE.BUSINESS_METRICS + [("fub_accepted", "Accepted Deals (FUB)", "n"),
                                              ("fub_deals", "Pending + Closed Deals (FUB)", "n"),
                                              ("fub_volume", "Volume (FUB)", "money")]
            quarters, total = CTE.business_quarters(months, metrics)
            status = request.args.get("status")
            status = status if status in dict(CTE.STATUS_TILES) else None
            data = dict(
                totals=_year_totals(cur, year, today),
                statuses=CTE.status_summary(cur, year, agent, source), status=status,
                status_deals=CTE.deals_with_status(cur, year, status, agent, source) if status else [],
                source_name="CTE", metrics=metrics, quarters=quarters, total=total,
                yoy={y: [m["closed"] for m in CTE.business_months(cur, y, agent, source)] for y in (year - 2, year - 1)}
                | {year: hide_future([m["closed"] for m in months], year)},
                tops=[(k, label, CTE.business_top(cur, since, source, until)) for k, label, since, until in top_periods],
                extra_filters=[
                    {"name": "cte_agent", "label": "Agent", "default": "",
                     "options": [("", "Whole team")] + [(n, n) for n in options]},
                    {"name": "source", "label": "Lead source", "default": "",
                     "options": [("", "All sources")] + [(s, s) for s in CTE.source_options(cur)]},
                    {"name": "year", "label": "Year", "default": str(year),
                     "options": [(str(y), str(y)) for y in CTE.years(cur)]}])
            ready = True
        else:
            f = R.Filters(today, today, request.args.get("agent", type=int), source, TEAM_TZ_NAME)
            ready = R.tables_ready(cur, "deals")
            data = {}
            if ready:
                months = R.business_months(cur, f, year)
                quarters, total = R.business_quarters(months)
                opts = filter_options(cur)
                data = dict(
                    source_name="FUB", quarters=quarters, total=total,
                    metrics=[("accepted", "Accepted Deals", "n"), ("deals", "All Deals", "n"),
                             ("volume", "Volume", "money"), ("avg", "Avg. Sales Price", "money")],
                    yoy={y: [m["deals"] for m in R.business_months(cur, f, y)] for y in (year - 2, year - 1)}
                    | {year: hide_future([m["deals"] for m in months], year)},
                    tops=[(k, label, R.business_top(cur, f, since, until)) for k, label, since, until in top_periods],
                    extra_filters=[
                        {"name": "agent", "label": "Agent", "default": "",
                         "options": [("", "All agents")] + [(str(i), n) for i, n in opts["agent_options"]]},
                        {"name": "source", "label": "Lead source", "default": "",
                         "options": [("", "All sources")] + [(s, s) for s in opts["source_options"]]},
                        {"name": "year", "label": "Year", "default": str(year),
                         "options": [(str(y), str(y)) for y in range(today.year, today.year - 4, -1)]}])
    return render_template("business_overview.html", ready=ready, year=year, **data)


# ---------------------------------------------------------------- Best Call Time

@app.route("/call-time")
@login_required
def call_time():
    rng, f = page_filters("last30")
    day = request.args.get("day", "Weekly")
    if day not in R.DAYS and day != "Weekly":
        day = "Weekly"
    with db() as cur:
        ready = R.tables_ready(cur, "agent_events")
        data = {}
        if ready:
            data = dict(ct=R.call_time(cur, f), **filter_options(cur))
    return render_template("call_time.html", ready=ready, rng=rng, day=day, days=R.DAYS, **data)


# ---------------------------------------------------------------- CTE workbooks

@app.route("/cte")
@login_required
def cte():
    rng = date_range("year")
    agent = request.args.get("cte_agent") or None
    year = (rng["end"] - timedelta(days=1)).year
    parts, started = {}, time_mod.perf_counter()

    def timed(name, fn):
        t = time_mod.perf_counter()
        out = fn()
        parts[name] = time_mod.perf_counter() - t
        return out
    gmail_import.memo_start()
    try:
        with db() as cur:
            ready = CTE.ready(cur)
            data = {}
            if ready:
                options = CTE.agent_options(cur)
                if agent and agent not in options:
                    agent = None
                years = timed("by_year", lambda: CTE.by_year(cur, agent))
                timed("year_extras", lambda: _year_extras(cur, years, agent))
                agents = timed("by_agent", lambda: [] if agent else CTE.by_agent(cur, rng["start"], rng["end"]))
                timed("agent_extras", lambda: _agent_extras(cur, agents, rng["start"], rng["end"]))
                data = dict(
                    kpi=timed("kpi", lambda: CTE.period(cur, rng["start"], rng["end"], agent)),
                    years=years, agents=agents,
                    trend=timed("trend", lambda: _cte_trend(cur, year, agent)),
                    agent_choices=[("", "Whole team")] + [(n, n) for n in options],
                    imported_at=CTE.last_import(cur),
                    company=timed("company", lambda: _company_income(cur, rng["start"].date(), rng["end"].date(), agent)),
                    focus=HOME.focus_deals(cur, request.args.get("focus"), year),
                    appts=timed("appts", lambda: _fub_appts(cur, rng["start"], rng["end"], agent)))
    finally:
        gmail_import.memo_stop()
    html = render_template("cte.html", ready=ready, rng=rng, agent=agent, onedrive=cte_import.graph_configured(),
                           refresh=_cte_refresh, **data)
    parts["total"] = time_mod.perf_counter() - started
    return html + "\n<!-- timing: " + ", ".join(f"{k}={v:.2f}s" for k, v in sorted(parts.items(), key=lambda x: -x[1])) + " -->"


def _qbo_months(cur):
    """{first-of-month date: (income, net income)} pulled from QuickBooks, or {} when not connected."""
    conn = qbo.status(cur)
    if not conn or conn["env"] != "production" or conn["needs_reconnect"]:
        return {}
    cur.execute("SELECT month, income, net_income FROM qbo_pnl")
    return {m: (float(i or 0), float(n or 0)) for m, i, n in cur.fetchall()}


def _fub_name_map(cur):
    """{CTE agent name (lowercase): FUB user id}"""
    out = {}
    if R.tables_ready(cur, "agents"):
        cte_names = CTE.agent_options(cur)
        for uid, name in R.agent_names(cur).items():
            cte_name = CTE.name_for(cur, name, cte_names)
            if cte_name:
                out[cte_name.lower()] = uid
    return out


def _year_extras(cur, years, agent=None):
    """Add to each CTE year: what the company really got (Compass YTD Income; QuickBooks income and net
    income for full years in the books) and Follow Up Boss appointments set / held."""
    qm = _qbo_months(cur)
    first_qbo = min(qm) if qm else None
    has_fub = R.tables_ready(cur, "people", "appointments")
    fub_from = None
    if has_fub:
        cur.execute("SELECT MIN(start_at) FROM appointments")
        fub_from = cur.fetchone()[0]
    uid = None
    if agent:
        uid = _fub_name_map(cur).get(agent.lower())
    by_year = {}
    if has_fub and fub_from and (not agent or uid):
        start = datetime(fub_from.year, 1, 1, tzinfo=TEAM_TZ)
        by_year = R.appt_counts(cur, R.Filters(start, datetime.now(TEAM_TZ) + timedelta(days=1), uid, None, TEAM_TZ_NAME), "year")
    for y in years:
        yr = y["year"]
        y["compass"] = None if agent else HOME._safe(cur, "compass ytd", lambda: HOME._compass_ytd(cur, yr))
        full_books = first_qbo is not None and first_qbo <= date(yr, 1, 1)
        if qm and full_books and not agent:
            months = [v for m, v in qm.items() if m.year == yr]
            y["qbo_income"], y["qbo_net"] = sum(v[0] for v in months), sum(v[1] for v in months)
        else:
            y["qbo_income"] = y["qbo_net"] = None
        # CTE's own Financial Statement only counts as company income while agent splits were entered there
        cos, inc = (float(v) if v is not None else None for v in (y.get("cost_of_sales"), y.get("income")))
        y["cte_company"] = (inc - cos) if (inc and cos and cos > 0.15 * inc) else None
        y["appts"] = None
        if has_fub and fub_from and fub_from.year <= yr and (not agent or uid):
            c = by_year.get(yr, {"set": 0, "held": 0, "not_held": 0})
            y["appts"] = {**c, "partial": fub_from.year == yr}
    return years


def _agent_extras(cur, agents, start, end):
    """Add to each CTE agent row: the company's share from the Compass receipts for their deals closed in the
    period, and Follow Up Boss appointments and calls."""
    if not agents:
        return agents
    try:
        deals, _ = gmail_import.deal_receipts(cur, start.date(), end.date())
    except Exception:
        app.logger.exception("agent company share")
        deals = []
    share = {}
    for d in deals:
        if d["receipts"] and d["agent"]:
            share[d["agent"].strip().lower()] = share.get(d["agent"].strip().lower(), 0.0) + \
                sum(float(r["amount"] or 0) for r in d["receipts"])
    uids = _fub_name_map(cur)
    has_fub = R.tables_ready(cur, "people", "appointments")
    by_agent = R.appt_counts(cur, R.Filters(start, end, None, None, TEAM_TZ_NAME), "agent") if has_fub else {}
    activity = {}
    if R.tables_ready(cur, "agent_events"):
        rows = R._cached(("leaderboard", start.date(), end.date()), 300,
                         lambda: R.fetch(cur, LEADERBOARD_COUNTS, {"start": start, "end": end}))
        for r in rows:
            activity[r["user_id"]] = r
    for a in agents:
        key = a["name"].strip().lower()
        a["company"] = share.get(key)
        uid = uids.get(key)
        a["fub"] = uid is not None
        a["appts"] = None
        if uid is not None and has_fub:
            a["appts"] = by_agent.get(uid, {"set": 0, "held": 0, "not_held": 0})
        ev = activity.get(uid) if uid is not None else None
        a["calls"] = ((ev["attempts"] or 0) + (ev["conversations"] or 0)) if ev else (0 if uid is not None else None)
        a["conversations"] = (ev["conversations"] or 0) if ev else (0 if uid is not None else None)
    return agents


def _cte_trend(cur, year, agent=None):
    """Monthly chart, this year vs last: company income from QuickBooks for the team when both years are in
    the books, otherwise closed GCI from the CTE deal log."""
    qm = {} if agent else _qbo_months(cur)
    if qm and min(qm) <= date(year - 1, 1, 1):
        def series(y):
            return [qm.get(date(y, m, 1), (0.0, 0.0))[0] for m in range(1, 13)]
        return {"year": year, "this": hide_future(series(year), year), "last": series(year - 1),
                "label": "Company income by month (QuickBooks)"}
    return {"year": year, "this": hide_future(CTE.monthly_gci(cur, year, agent), year),
            "last": CTE.monthly_gci(cur, year - 1, agent), "label": "Closed GCI by month (CTE deal log)"}


def _company_income(cur, start, end, agent=None):
    """What the company actually received from Compass for the deals that closed in [start, end):
    the team's share after the agent's split and Compass's fees (net), from the remittance statements.
    For the whole team, receipts in the period that match no CTE deal (referrals etc.) are added."""
    try:
        deals, unmatched = gmail_import.deal_receipts(cur, start, end)
    except Exception:
        app.logger.exception("company income")
        return None
    if agent:
        deals = [d for d in deals if (d["agent"] or "").strip().lower() == agent.strip().lower()]
    paid = [r for d in deals for r in d["receipts"]]
    if not paid and not unmatched:
        return None
    other = [] if agent else unmatched
    return {"net": sum(float(r["amount"] or 0) for r in paid + other),
            "gross": sum(float(r["gross"] or r["amount"] or 0) for r in paid + other),
            "other": sum(float(r["amount"] or 0) for r in other),
            "deals": len(deals), "with": sum(1 for d in deals if d["receipts"])}


def _fub_appts(cur, start, end, agent=None):
    """Appointments from Follow Up Boss with the held / not-held rule (reports.APPT_CLASS):
    set = created in the period; held / not held = took place in the period."""
    if not R.tables_ready(cur, "people", "appointments"):
        return None
    uid = None
    if agent:
        names = CTE.agent_options(cur)
        uid = next((u for u, n in R.agent_names(cur).items() if CTE.name_for(cur, n, names) == agent), None)
        if uid is None:
            return {"missing": True}
    c = R.funnel_counts(cur, R.Filters(start, end, uid, None, TEAM_TZ_NAME))
    decided = c["held"] + c["not_held"]
    return {"set": c["appts_set"], "sched": c["appts_sched"], "held": c["held"], "not_held": c["not_held"],
            "held_rate": R.pct(c["held"], decided, 0) if decided else None}


# One OneDrive import at a time, in the background (downloading the workbooks can take longer
# than a web request is allowed to run). Read-only: files are only downloaded, never changed.
_cte_refresh = {"running": False, "log": [], "finished": None}


@app.route("/cte/refresh", methods=["POST"])
@login_required
def cte_refresh():
    if not cte_import.graph_configured():
        return redirect(url_for("cte"))
    if not _cte_refresh["running"]:
        _cte_refresh.update(running=True, log=[], finished=None)

        def run():
            try:
                cte_import.import_from_onedrive(DATABASE_URL, log=_cte_refresh["log"].append)
            except Exception as e:  # noqa: BLE001 - shown on the page
                _cte_refresh["log"].append(f"CTE import failed: {e}")
            finally:
                _cte_refresh.update(running=False, finished=datetime.now(TEAM_TZ))

        threading.Thread(target=run, daemon=True).start()
    return redirect(url_for("cte", **request.args))


# ---------------------------------------------------------------- Agent Snapshot

AGENT_TABS = [("funnel", "Sales Funnel"), ("opportunities", "Opportunities Waiting"),
              ("financial", "Financial Insights"), ("goals", "Goals & Pacing"), ("coaching", "Coaching Notes")]


@app.route("/agent")
@login_required
def agent_snapshot():
    tab = request.args.get("tab", "funnel")
    if tab not in dict(AGENT_TABS):
        tab = "funnel"
    rng, f = page_filters("month" if tab == "goals" else "last90")
    now = datetime.now(TEAM_TZ)
    with db() as cur:
        ready = R.tables_ready(cur, "people", "agent_events")
        data = {}
        if ready:
            options = R.agent_options(cur)
            uid = f.agent or R.most_active_agent(cur) or (options[0][0] if options else None)
            data = dict(agent_options=options, source_options=R.source_options(cur), uid=uid,
                        info=R.agent_info(cur, uid) if uid else None)
            if uid:
                f.agent = uid
                if tab == "funnel":
                    data["funnel"] = R.agent_funnel(cur, f)
                    if CTE.ready(cur):
                        data["cte_closed"] = CTE.deal_counts(cur, f.start, f.end, cte_agent_for(cur, uid), f.source)
                elif tab == "opportunities":
                    data["opp"] = R.opportunities(cur, uid, now)
                elif tab == "financial":
                    fub_fin = R.agent_financials(cur, f, uid, now)
                    data["fin"] = fub_fin
                    cte_name = CTE.name_for(cur, data["info"]["name"]) if data["info"] else None
                    if cte_name:
                        # Performance cards from the CTE deal log (GCI + commission %); FUB kept for comparison
                        year_ago = today_start() - timedelta(days=365)
                        l12m = R.Filters(year_ago, today_start() + timedelta(days=1), uid, None, TEAM_TZ_NAME)
                        leads = R.funnel_counts(cur, l12m)["new_leads"]
                        team_leads = R.funnel_counts(cur, R.Filters(l12m.start, l12m.end, None, None, TEAM_TZ_NAME))["new_leads"]
                        fin = CTE.agent_financials(cur, cte_name, now)
                        fin["conversion"] = R.pct(fin["deals"], leads, 3)
                        fin["team_conversion"] = R.pct(CTE.closed_count_since(cur, year_ago), team_leads, 3)
                        fin["fub"] = fub_fin
                        data["fin"] = fin
                    data["fin"]["this_year"] = hide_future(data["fin"]["this_year"], now.year)
                    if cte_name:
                        jan1 = today_start().replace(month=1, day=1)
                        data["cte"] = dict(
                            name=cte_name,
                            ytd=CTE.period(cur, jan1, today_start() + timedelta(days=1), cte_name),
                            l12m=CTE.period(cur, today_start() - timedelta(days=365),
                                            today_start() + timedelta(days=1), cte_name),
                            years=CTE.by_year(cur, cte_name), deals=CTE.agent_deals(cur, cte_name))
                elif tab == "goals":
                    R.ensure_app_tables(cur)
                    # Appointments -> under contract uses CTE deals when the agent is in CTE
                    uc = None
                    if CTE.ready(cur):
                        cte_name = CTE.name_for(cur, data["info"]["name"]) if data["info"] else None
                        if cte_name:
                            uc = CTE.deal_counts(cur, f.start, f.end, cte_name)["written"]
                    data["goals"] = R.goals_view(cur, f, uid, uc)
                else:
                    R.ensure_app_tables(cur)
                    data["notes"] = R.coaching_notes(cur, uid, request.args.get("author") or None,
                                                     request.args.get("note_type") or None)
                    data["authors"] = sorted({n["author"] for n in R.coaching_notes(cur, uid)})
    return render_template("agent.html", ready=ready, rng=rng, tab=tab, tabs=AGENT_TABS,
                           note_types=R.NOTE_TYPES, **data)


@app.route("/agent/goals", methods=["POST"])
@login_required
def save_goals():
    uid = request.form.get("agent", type=int)
    with db() as cur:
        R.ensure_app_tables(cur)
        for key, *_ in R.GOAL_METRICS:
            raw = (request.form.get(key) or "").strip()
            if raw == "":
                cur.execute("DELETE FROM agent_goals WHERE user_id = %s AND metric = %s", (uid, key))
                continue
            try:
                target = float(raw)
            except ValueError:
                continue
            cur.execute("""
                INSERT INTO agent_goals (user_id, metric, target) VALUES (%s, %s, %s)
                ON CONFLICT (user_id, metric) DO UPDATE SET target = EXCLUDED.target
            """, (uid, key, target))
    # uid 0 = team defaults; go back to the agent the form was opened from
    back = request.form.get("back", type=int) if uid == R.TEAM_GOALS_ID else uid
    return redirect(url_for("agent_snapshot", agent=back, tab="goals"))


@app.route("/agent/notes", methods=["POST"])
@login_required
def add_note():
    uid = request.form.get("agent", type=int)
    body = (request.form.get("body") or "").strip()
    author = (request.form.get("author") or "").strip()[:80] or "Manager"
    note_type = request.form.get("note_type")
    if note_type not in R.NOTE_TYPES:
        note_type = R.NOTE_TYPES[0]
    if uid and body:
        with db() as cur:
            R.ensure_app_tables(cur)
            cur.execute("INSERT INTO coaching_notes (user_id, author, note_type, body) VALUES (%s, %s, %s, %s)",
                        (uid, author, note_type, body[:5000]))
    return redirect(url_for("agent_snapshot", agent=uid, tab="coaching"))


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
