import csv
import io
import os
import secrets
from contextlib import contextmanager
from functools import wraps
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from flask import Flask, Response, render_template, request, session, redirect, url_for

import psycopg2

import cte_reports as CTE
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
                          ("call_time", "Best Call Time Report"), ("cte", "CTE Year by Year")]),
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


@app.route("/")
@login_required
def leaderboard():
    rng = date_range("today")
    agents, totals = get_agents(rng["start"], rng["end"])
    return render_template("leaderboard.html", podium=agents[:3], rest=agents[3:],
                           totals=totals, has_data=len(agents) > 0, rng=rng)


# ---------------------------------------------------------------- Dashboard

DASHBOARD_TABS = {"pipeline", "response", "source"}


@app.route("/dashboard")
@login_required
def dashboard():
    tab = request.args.get("tab", "pipeline")
    if tab not in DASHBOARD_TABS:
        tab = "pipeline"
    rng, f = page_filters("last30")
    data = {}
    with db() as cur:
        ready = R.tables_ready(cur, "people")
        if ready:
            data.update(filter_options(cur))
            if tab == "pipeline":
                data["pipeline"] = R.pipeline_health(cur, f)
            elif tab == "response":
                data["response"] = R.lead_response(cur, f, min(f.end, datetime.now(TEAM_TZ)))
            else:
                data["sources"] = R.best_sources(cur, f)
    return render_template("dashboard.html", tab=tab, ready=ready, rng=rng, **data)


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
    return view_by, request.args.get("type") or None, status, request.args.get("stage") or None


@app.route("/appointments")
@login_required
def appointments():
    view_by, appt_type, status, stage = _appt_args()
    page = max(request.args.get("page", 1, type=int), 1)
    rng, f = page_filters("last90")
    with db() as cur:
        ready = R.tables_ready(cur, "appointments")
        data = {}
        if ready:
            rows, total = R.appointment_list(cur, f, view_by, appt_type, status, page, stage=stage)
            data = dict(kpis=R.appointment_kpis(cur, f, view_by, appt_type, stage), rows=rows, total=total,
                        pages=max((total + 24) // 25, 1),
                        type_choices=[("", "All types")] + [(t, t) for t in R.appt_type_options(cur)],
                        stage_choices=[("", "All stages")] + [(s, s) for s in R.stage_options(cur)],
                        **filter_options(cur))
    return render_template("appointments.html", ready=ready, rng=rng, view_by=view_by, appt_type=appt_type,
                           status=status, page=page, **data)


@app.route("/appointments.csv")
@login_required
def appointments_csv():
    view_by, appt_type, status, stage = _appt_args()
    _, f = page_filters("last90")
    with db() as cur:
        rows, _ = R.appointment_list(cur, f, view_by, appt_type, status, 1, per_page=100000, stage=stage)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Agent(s)", "Lead", "Created", "Appointment Time", "Type", "Outcome", "Lead Source",
                "Current Stage", "Created By"])
    for r in rows:
        w.writerow([r["agent_names"], r["lead_name"],
                    r["created_at"].astimezone(TEAM_TZ).strftime("%Y-%m-%d") if r["created_at"] else "",
                    r["start_at"].astimezone(TEAM_TZ).strftime("%Y-%m-%d %H:%M") if r["start_at"] else "",
                    r["type"], r["outcome"], r["source"], r["stage"], r["created_by_name"]])
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
                ov=R.lead_source_overview(cur, f), top_agents=R.top_agents_closed(cur, f),
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

@app.route("/business-overview")
@login_required
def business_overview():
    today = today_start()
    year = request.args.get("year", today.year, type=int)
    top_days = 365 if request.args.get("top") == "365" else 30
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
                            if agent and name.lower() == agent.lower()), None)
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
            data = dict(
                source_name="CTE", metrics=metrics, quarters=quarters, total=total,
                yoy={y: [m["closed"] for m in CTE.business_months(cur, y, agent, source)] for y in (year - 2, year - 1)}
                | {year: hide_future([m["closed"] for m in months], year)},
                top=CTE.business_top(cur, today - timedelta(days=top_days), source),
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
                    top=R.business_top(cur, f, today - timedelta(days=top_days)),
                    extra_filters=[
                        {"name": "agent", "label": "Agent", "default": "",
                         "options": [("", "All agents")] + [(str(i), n) for i, n in opts["agent_options"]]},
                        {"name": "source", "label": "Lead source", "default": "",
                         "options": [("", "All sources")] + [(s, s) for s in opts["source_options"]]},
                        {"name": "year", "label": "Year", "default": str(year),
                         "options": [(str(y), str(y)) for y in range(today.year, today.year - 4, -1)]}])
    return render_template("business_overview.html", ready=ready, year=year, top_days=top_days, **data)


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
    with db() as cur:
        ready = CTE.ready(cur)
        data = {}
        if ready:
            options = CTE.agent_options(cur)
            if agent and agent not in options:
                agent = None
            data = dict(
                kpi=CTE.period(cur, rng["start"], rng["end"], agent),
                years=CTE.by_year(cur, agent),
                agents=[] if agent else CTE.by_agent(cur, rng["start"], rng["end"]),
                trend={"year": year, "this": hide_future(CTE.monthly_gci(cur, year, agent), year),
                       "last": CTE.monthly_gci(cur, year - 1, agent)},
                agent_choices=[("", "Whole team")] + [(n, n) for n in options],
                imported_at=CTE.last_import(cur))
    return render_template("cte.html", ready=ready, rng=rng, agent=agent, **data)


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
    return redirect(url_for("agent_snapshot", agent=uid, tab="goals"))


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
