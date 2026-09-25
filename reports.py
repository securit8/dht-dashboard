"""Report queries behind every dashboard page.

Each function takes an open cursor (and usually a Filters) and returns plain
dicts/lists for the templates. Tables are filled by fub_agent_activity_pull.py:
agent_events, people, appointments, deals, agents. The web app owns
agent_goals and coaching_notes (see ensure_app_tables).
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

# ---------------------------------------------------------------- stages

# Pipeline buckets, in display order, with chart colors.
BUCKETS = [
    ("new", "New / No Contact Made", "#8FB8E0"),
    ("attempted", "Attempted Contact / Unresponsive", "#F2B84B"),
    ("appt", "Appointment Outstanding", "#C99BE0"),
    ("short", "Short Term Nurture", "#7B8FF0"),
    ("long", "Long Term Nurture", "#E88BA0"),
    ("active", "Actively Listed / Showing Homes", "#8FD3C8"),
    ("closed", "Closed / Under Contract", "#2FA867"),
    ("trash", "Rejected / Trash", "#9AA5B1"),
    ("other", "Other (unmapped stage)", "#D3D8E0"),
]
BUCKET_LABEL = {k: label for k, label, _ in BUCKETS}

# Follow Up Boss stage name (lowercase) -> bucket. Edit this if the team
# renames or adds stages; anything not listed shows up as "Other".
STAGE_BUCKETS = {
    "lead": "new", "new lead": "new", "new": "new",
    "attempted contact": "attempted", "unresponsive": "attempted",
    "appointment set": "appt",
    "spoke with customer": "short", "hot prospect": "short", "met with customer": "short",
    "short term nurture": "short",
    "nurture": "long", "long term nurture": "long", "sphere": "long", "past client": "long",
    "showing homes": "active", "active client": "active", "listing agreement": "active",
    "active listing": "active", "submitting offers": "active",
    "under contract": "closed", "pending": "closed", "listing | pending": "closed", "closed": "closed",
    "sale closed": "closed",
    "trash": "trash", "rejected": "trash", "archived": "trash", "archive": "trash", "do not contact": "trash",
}
CLOSED_STAGES = [s for s, b in STAGE_BUCKETS.items() if b == "closed"]
TRASH_STAGES = [s for s, b in STAGE_BUCKETS.items() if b == "trash"]
# FUB contacts that are not leads (other agents, vendors): left out of every lead count
NOT_LEAD_STAGES = ["real estate agent", "vendor"]
# Bulk uploads (source "Import", "BT Mass Upload ...") would swamp new-lead counts;
# they are left out unless that source is picked in the filter
IMPORT_SOURCES = "(import|mass upload)"


def bucket_for(stage):
    return STAGE_BUCKETS.get((stage or "").strip().lower(), "other")


# ---------------------------------------------------------------- SQL pieces

CONTACT_TYPES = ["attempt", "text", "email"]  # outbound touches
SRC = "COALESCE(NULLIF(TRIM(p.source), ''), '<unspecified>')"

# Deal stage names vary by pipeline, so deals are classified by keywords.
# "cancelled" is checked first so "Closed Lost" doesn't count as closed.
DEAL_CLASS = """(CASE
    WHEN d.stage_name ~* '(cancel|terminat|fell|lost|dead|withdrawn|expired)' THEN 'cancelled'
    WHEN d.stage_name ~* '(closed|sold|won|funded)' THEN 'closed'
    WHEN d.stage_name ~* '(pending|under contract|escrow)' THEN 'pending'
    WHEN d.stage_name ~* 'accepted' THEN 'accepted'
    ELSE 'active' END)"""
# When a closed/pending deal "happened": the day it entered that stage
DEAL_DATE = "COALESCE(d.entered_stage_at, d.projected_close, d.created_at)"

# Appointment outcomes are free-text in FUB; blank = no outcome recorded yet
APPT_CLASS = """(CASE
    WHEN COALESCE(a.outcome, '') = '' THEN 'none'
    WHEN a.outcome ~* '(not|no.?show|cancel|resched|miss|didn)' THEN 'not_held'
    ELSE 'held' END)"""

# Real leads only: not agents/vendors, and no bulk imports unless that source is picked
REAL_LEADS = f"""LOWER(TRIM(COALESCE(p.stage, ''))) <> ALL(%(not_lead_stages)s)
    AND (%(source)s::text IS NOT NULL OR COALESCE(p.source, '') !~* '{IMPORT_SOURCES}')"""
PEOPLE_F = f"""(%(agent)s::bigint IS NULL OR p.assigned_user_id = %(agent)s)
    AND (%(source)s::text IS NULL OR {SRC} = %(source)s)
    AND {REAL_LEADS}"""
EVENTS_F = f"""(%(agent)s::bigint IS NULL OR e.user_id = %(agent)s)
    AND (%(source)s::text IS NULL OR EXISTS (
        SELECT 1 FROM people p WHERE p.person_id = e.person_id AND {SRC} = %(source)s))"""
APPTS_F = f"""(%(agent)s::bigint IS NULL OR %(agent)s = ANY(a.agent_ids))
    AND (%(source)s::text IS NULL OR EXISTS (
        SELECT 1 FROM people p WHERE p.person_id = a.person_id AND {SRC} = %(source)s))"""
DEALS_F = f"""(%(agent)s::bigint IS NULL OR %(agent)s = ANY(d.user_ids))
    AND (%(source)s::text IS NULL OR EXISTS (
        SELECT 1 FROM people p WHERE p.person_id = ANY(d.person_ids) AND {SRC} = %(source)s))"""


@dataclass
class Filters:
    start: datetime
    end: datetime  # exclusive
    agent: int = None
    source: str = None
    tz: str = "America/Los_Angeles"

    def params(self, **extra):
        p = {"start": self.start, "end": self.end, "agent": self.agent, "source": self.source,
             "tz": self.tz, "contact_types": CONTACT_TYPES,
             "closed_stages": CLOSED_STAGES, "trash_stages": TRASH_STAGES,
             "not_lead_stages": NOT_LEAD_STAGES}
        p.update(extra)
        return p

    def previous(self):
        """Same-length period right before this one."""
        length = self.end - self.start
        return replace(self, start=self.start - length, end=self.start)

    def year_earlier(self):
        return replace(self, start=_minus_year(self.start), end=_minus_year(self.end))


def _minus_year(dt):
    try:
        return dt.replace(year=dt.year - 1)
    except ValueError:  # Feb 29
        return dt.replace(year=dt.year - 1, day=28)


def fetch(cur, sql, params):
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def one(cur, sql, params):
    return fetch(cur, sql, params)[0]


def pct(part, whole, digits=1):
    return round(part / whole * 100, digits) if whole else 0


def change(cur_val, prev_val):
    """% change vs previous period: None when both are 0, "new" when prev is 0."""
    if not prev_val:
        return None if not cur_val else "new"
    return round((cur_val - prev_val) / prev_val * 100, 1)


# ---------------------------------------------------------------- lookups

def agent_names(cur):
    """{user_id: name} from the agents table, falling back to event history."""
    names = {}
    for r in fetch(cur, "SELECT DISTINCT user_id, agent_name FROM agent_events", {}):
        names[r["user_id"]] = r["agent_name"]
    if table_exists(cur, "agents"):
        for r in fetch(cur, "SELECT user_id, name FROM agents", {}):
            names[r["user_id"]] = r["name"]
    return names


def agent_options(cur):
    """Agents with any activity, for filter dropdowns: [(id, name)] by name."""
    names = agent_names(cur)
    active = {r["user_id"] for r in fetch(cur, "SELECT DISTINCT user_id FROM agent_events", {})}
    return sorted(((uid, names[uid]) for uid in active if uid in names), key=lambda x: x[1].lower())


def most_active_agent(cur, days=30):
    """The FUB user with the most logged activity lately (Agent Snapshot's default)."""
    rows = fetch(cur, """SELECT user_id FROM agent_events WHERE created_at >= NOW() - make_interval(days => %(d)s)
                         GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1""", {"d": days})
    return rows[0]["user_id"] if rows else None


def source_options(cur):
    return [r["src"] for r in fetch(cur, f"""
        SELECT {SRC} AS src, COUNT(*) FROM people p GROUP BY 1 ORDER BY 2 DESC""", {})]


# FUB's default stage order, used to sort the stage filter (other stages go after)
FUB_STAGE_ORDER = ["lead", "attempted contact", "spoke with customer", "appointment set", "met with customer",
                   "showing homes", "listing agreement", "active listing", "submitting offers", "under contract",
                   "closed", "sphere", "nurture", "unresponsive", "trash", "archive"]


def stage_options(cur):
    stages = {r["s"].strip() for r in fetch(cur, "SELECT DISTINCT stage AS s FROM people WHERE COALESCE(TRIM(stage), '') <> ''", {})}
    rank = {s: i for i, s in enumerate(FUB_STAGE_ORDER)}
    return sorted(stages, key=lambda s: (rank.get(s.lower(), len(rank)), s.lower()))


def appt_type_options(cur):
    return [r["type"] for r in fetch(cur, """
        SELECT DISTINCT type FROM appointments WHERE COALESCE(type, '') <> '' ORDER BY 1""", {})]


def table_exists(cur, name):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return cur.fetchone()[0]


def tables_ready(cur, *names):
    """True when every table exists and has at least one row."""
    for n in names:
        if not table_exists(cur, n):
            return False
        cur.execute(f"SELECT EXISTS (SELECT 1 FROM {n})")
        if not cur.fetchone()[0]:
            return False
    return True


def ensure_app_tables(cur):
    """Tables the web app writes to (goals and coaching notes)."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_goals (
            user_id BIGINT NOT NULL,
            metric TEXT NOT NULL,
            target NUMERIC NOT NULL,
            PRIMARY KEY (user_id, metric)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coaching_notes (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            author TEXT NOT NULL,
            note_type TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


# ---------------------------------------------------------------- shared counts

def funnel_counts(cur, f):
    """Headline counts for a period: leads, appointments and deals."""
    p = f.params()
    c = one(cur, f"SELECT COUNT(*) AS new_leads FROM people p WHERE p.created_at >= %(start)s AND p.created_at < %(end)s AND {PEOPLE_F}", p)
    c.update(one(cur, f"""
        SELECT COUNT(*) FILTER (WHERE a.created_at >= %(start)s AND a.created_at < %(end)s) AS appts_set,
               COUNT(*) FILTER (WHERE a.start_at >= %(start)s AND a.start_at < %(end)s) AS appts_sched,
               COUNT(*) FILTER (WHERE a.start_at >= %(start)s AND a.start_at < %(end)s AND {APPT_CLASS} = 'held') AS held,
               COUNT(*) FILTER (WHERE a.start_at >= %(start)s AND a.start_at < %(end)s AND {APPT_CLASS} = 'not_held') AS not_held
        FROM appointments a WHERE {APPTS_F}""", p))
    c.update(one(cur, f"""
        SELECT COUNT(*) FILTER (WHERE w) AS written, COALESCE(SUM(price) FILTER (WHERE w), 0) AS written_vol,
               COUNT(*) FILTER (WHERE cls = 'cancelled' AND made) AS cancelled,
               COUNT(*) FILTER (WHERE cls = 'pending' AND happened) AS pending,
               COALESCE(SUM(price) FILTER (WHERE cls = 'pending' AND happened), 0) AS pending_vol,
               COUNT(*) FILTER (WHERE cls = 'closed' AND happened) AS closed,
               COALESCE(SUM(price) FILTER (WHERE cls = 'closed' AND happened), 0) AS closed_vol
        FROM (SELECT d.price, {DEAL_CLASS} AS cls,
                     (d.created_at >= %(start)s AND d.created_at < %(end)s) AS made,
                     (d.created_at >= %(start)s AND d.created_at < %(end)s AND {DEAL_CLASS} <> 'cancelled') AS w,
                     ({DEAL_DATE} >= %(start)s AND {DEAL_DATE} < %(end)s) AS happened
              FROM deals d WHERE {DEALS_F}) x""", p))
    return c


def _kpi_block(cur, f, prev):
    c, p = funnel_counts(cur, f), funnel_counts(cur, prev)
    return {
        **c,
        "set_rate": pct(c["appts_set"], c["new_leads"], 2),
        "held_rate": pct(c["held"], c["appts_sched"], 2),
        "conversion": pct(c["closed"] + c["pending"], c["new_leads"], 2),
        "chg_set": change(c["appts_set"], p["appts_set"]),
        "chg_sched": change(c["appts_sched"], p["appts_sched"]),
        "chg_written": change(c["written"], p["written"]),
        "chg_closed": change(c["closed"], p["closed"]),
    }


def ytd(f, today):
    """Jan 1 of the filter's year through today (or the filter end, if earlier)."""
    end = min(f.end, today + timedelta(days=1))
    start = end.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if end == start:
        start = start.replace(year=start.year - 1)
    return replace(f, start=start, end=end)


# ---------------------------------------------------------------- Sales Manager

def sales_manager_kpis(cur, f, today):
    y = ytd(f, today)
    return {"period": _kpi_block(cur, f, f.previous()),
            "ytd": _kpi_block(cur, y, y.year_earlier()), "ytd_year": y.start.year}


def _per_agent(cur, sql, p):
    out = {}
    for r in fetch(cur, sql, p):
        out.setdefault(r["uid"], {})[r["k"]] = r["n"]
    return out


def team_overview(cur, f):
    """Per-agent rows for the Sales Funnel / Stage Breakdown / Outreach views."""
    p = f.params()
    names = agent_names(cur)
    leads = _per_agent(cur, f"""
        SELECT p.assigned_user_id AS uid, p.stage AS k, COUNT(*) AS n FROM people p
        WHERE p.created_at >= %(start)s AND p.created_at < %(end)s AND p.assigned_user_id IS NOT NULL
          AND {PEOPLE_F} GROUP BY 1, 2""", p)
    appts = _per_agent(cur, f"""
        SELECT uid, k, COUNT(*) AS n FROM (
            SELECT unnest(a.agent_ids) AS uid,
                   CASE WHEN a.created_at >= %(start)s AND a.created_at < %(end)s THEN 'set' END AS k
            FROM appointments a WHERE {APPTS_F}
            UNION ALL
            SELECT unnest(a.agent_ids), 'held' FROM appointments a
            WHERE a.start_at >= %(start)s AND a.start_at < %(end)s AND {APPT_CLASS} = 'held' AND {APPTS_F}
        ) x WHERE k IS NOT NULL GROUP BY 1, 2""", p)
    deals = _per_agent(cur, f"""
        SELECT uid, k, COUNT(*) AS n FROM (
            SELECT unnest(d.user_ids) AS uid, CASE
                WHEN {DEAL_CLASS} IN ('closed', 'pending') AND {DEAL_DATE} >= %(start)s AND {DEAL_DATE} < %(end)s
                     THEN {DEAL_CLASS}
                WHEN {DEAL_CLASS} <> 'cancelled' AND d.created_at >= %(start)s AND d.created_at < %(end)s
                     THEN 'written' END AS k
            FROM deals d WHERE {DEALS_F}
        ) x WHERE k IS NOT NULL GROUP BY 1, 2""", p)
    outreach = _per_agent(cur, f"""
        SELECT e.user_id AS uid, e.event_type AS k, COUNT(*) AS n FROM agent_events e
        WHERE e.created_at >= %(start)s AND e.created_at < %(end)s AND {EVENTS_F} GROUP BY 1, 2""", p)

    rows = []
    for uid in set(leads) | set(appts) | set(deals) | set(outreach):
        by_stage = {}
        for stage, n in leads.get(uid, {}).items():
            b = bucket_for(stage)
            by_stage[b] = by_stage.get(b, 0) + n
        total = sum(by_stage.values())
        a, d, o = appts.get(uid, {}), deals.get(uid, {}), outreach.get(uid, {})
        written = d.get("written", 0) + d.get("pending", 0) + d.get("closed", 0)
        rows.append({
            "uid": uid, "name": names.get(uid, f"User {uid}"),
            "total_leads": total, "appts": a.get("set", 0), "held": a.get("held", 0),
            "held_pct": pct(a.get("held", 0), a.get("set", 0)),
            "accepted": written, "accepted_pct": pct(written, total),
            "pending": d.get("pending", 0), "closed": d.get("closed", 0),
            "conversion": pct(d.get("closed", 0) + d.get("pending", 0), total),
            "stages": {k: by_stage.get(k, 0) for k, _, _ in BUCKETS},
            "calls": o.get("attempt", 0), "conversations": o.get("conversation", 0),
            "texts": o.get("text", 0), "emails": o.get("email", 0),
        })
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def team_average(rows, keys):
    if not rows:
        return {}
    avg = {k: round(sum(r[k] for r in rows) / len(rows), 2) for k in keys}
    avg["stages"] = {k: round(sum(r["stages"][k] for r in rows) / len(rows), 2) for k, _, _ in BUCKETS}
    return avg


def _top5(cur, sql, p, names):
    return [{"name": names.get(r["uid"], f"User {r['uid']}"), "value": r["n"]}
            for r in fetch(cur, sql + " ORDER BY n DESC LIMIT 5", p)]


def top_performers(cur, f, today):
    names = agent_names(cur)
    jan1 = today.replace(month=1, day=1)
    base = replace(f, end=today + timedelta(days=1))
    return {
        "closers": _top5(cur, f"""
            SELECT unnest(d.user_ids) AS uid, COUNT(*) AS n FROM deals d
            WHERE {DEAL_CLASS} = 'closed' AND {DEAL_DATE} >= %(start)s AND {DEALS_F} GROUP BY 1""",
            replace(base, start=jan1).params(), names),
        "bookers": _top5(cur, f"""
            SELECT unnest(a.agent_ids) AS uid, COUNT(*) AS n FROM appointments a
            WHERE a.created_at >= %(start)s AND {APPTS_F} GROUP BY 1""",
            replace(base, start=jan1).params(), names),
        "written": _top5(cur, f"""
            SELECT unnest(d.user_ids) AS uid, COUNT(*) AS n FROM deals d
            WHERE {DEAL_CLASS} <> 'cancelled' AND d.created_at >= %(start)s AND {DEALS_F} GROUP BY 1""",
            replace(base, start=today - timedelta(days=90)).params(), names),
        "attempts": _top5(cur, f"""
            SELECT e.user_id AS uid, COUNT(*) AS n FROM agent_events e
            WHERE e.event_type = ANY(%(contact_types)s) AND e.created_at >= %(start)s AND {EVENTS_F} GROUP BY 1""",
            replace(base, start=today - timedelta(days=30)).params(), names),
    }


# ---------------------------------------------------------------- Appointments

APPT_DATE_FIELDS = {"created": "a.created_at", "start": "a.start_at"}
# Filter appointments by the lead's current FUB stage (e.g. "Submitting offers")
APPT_STAGE_F = """
        AND (%(stage)s::text IS NULL OR EXISTS (SELECT 1 FROM people ps WHERE ps.person_id = a.person_id
                                                AND LOWER(TRIM(ps.stage)) = LOWER(TRIM(%(stage)s))))"""
APPT_STATUSES = {"all", "held", "not_held", "none"}


def appointment_kpis(cur, f, view_by, appt_type, stage=None):
    col = APPT_DATE_FIELDS[view_by]
    sql = f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {APPT_CLASS} = 'held') AS held,
               COUNT(*) FILTER (WHERE {APPT_CLASS} = 'not_held') AS not_held,
               COUNT(*) FILTER (WHERE {APPT_CLASS} = 'none') AS none
        FROM appointments a
        WHERE {col} >= %(start)s AND {col} < %(end)s AND {APPTS_F}
          AND (%(type)s::text IS NULL OR a.type = %(type)s){APPT_STAGE_F}"""
    c = one(cur, sql, f.params(type=appt_type, stage=stage))
    p = one(cur, sql, f.previous().params(type=appt_type, stage=stage))
    leads = funnel_counts(cur, f)["new_leads"]
    return {**c, "set_rate": pct(c["total"], leads, 0),
            "held_rate": pct(c["held"], c["total"], 0), "not_held_rate": pct(c["not_held"], c["total"], 0),
            "none_rate": pct(c["none"], c["total"], 0),
            "chg": {k: change(c[k], p[k]) for k in ("total", "held", "not_held", "none")}}


def appointment_list(cur, f, view_by, appt_type, status, page, per_page=25, stage=None):
    col = APPT_DATE_FIELDS[view_by]
    where = f"""{col} >= %(start)s AND {col} < %(end)s AND {APPTS_F}
        AND (%(type)s::text IS NULL OR a.type = %(type)s)
        AND (%(status)s = 'all' OR {APPT_CLASS} = %(status)s){APPT_STAGE_F}"""
    p = f.params(type=appt_type, status=status, stage=stage, limit=per_page, offset=(page - 1) * per_page)
    total = one(cur, f"SELECT COUNT(*) AS n FROM appointments a WHERE {where}", p)["n"]
    rows = fetch(cur, f"""
        SELECT a.agent_names, COALESCE(NULLIF(a.lead_name, ''), p.name, '') AS lead_name,
               a.created_at, a.start_at, a.type, a.outcome, {APPT_CLASS} AS status,
               COALESCE(NULLIF(TRIM(p.source), ''), '<unspecified>') AS source, p.stage, a.created_by_name
        FROM appointments a LEFT JOIN people p ON p.person_id = a.person_id
        WHERE {where}
        ORDER BY {col} DESC LIMIT %(limit)s OFFSET %(offset)s""", p)
    return rows, total


# ---------------------------------------------------------------- Lead Source

# Lead Source / Best Lead Source fold the 8 buckets into Maverick's groups
SOURCE_GROUPS = [
    ("Top of Funnel", "#F2B84B", ("new", "attempted", "appt")),
    ("Active Buyer/Seller", "#8FD3C8", ("active",)),
    ("Long Nurture", "#E88BA0", ("long",)),
    ("Short Nurture", "#7B8FF0", ("short",)),
    ("Closed", "#2FA867", ("closed",)),
    ("Trash", "#9AA5B1", ("trash", "other")),
]


def lead_source_overview(cur, f):
    def counts(ff):
        p = ff.params()
        c = one(cur, f"SELECT COUNT(*) AS leads FROM people p WHERE p.created_at >= %(start)s AND p.created_at < %(end)s AND {PEOPLE_F}", p)
        c.update(one(cur, f"""
            SELECT COUNT(*) FILTER (WHERE e.event_type = 'attempt') AS calls,
                   COUNT(*) FILTER (WHERE e.event_type = 'text') AS texts,
                   COUNT(*) FILTER (WHERE e.event_type = 'email') AS emails
            FROM agent_events e WHERE e.created_at >= %(start)s AND e.created_at < %(end)s AND {EVENTS_F}""", p))
        fc = funnel_counts(cur, ff)
        c.update(appts=fc["appts_set"], held=fc["held"], written=fc["written"],
                 pending=fc["pending"], closed=fc["closed"])
        c["outbound"] = c["calls"] + c["texts"] + c["emails"]
        c["closed_pct"] = pct(c["closed"], c["leads"], 2)
        c["closed_pending_pct"] = pct(c["closed"] + c["pending"], c["leads"], 2)
        return c

    c, p = counts(f), counts(f.previous())
    c["held_pct"] = pct(c["held"], c["appts"], 0)
    for k in ("calls", "texts", "emails"):
        c[k + "_share"] = pct(c[k], c["outbound"], 0)
    c["chg"] = {"leads": change(c["leads"], p["leads"]), "outbound": change(c["outbound"], p["outbound"]),
                "appts": change(c["appts"], p["appts"]),
                "deals": change(c["closed"] + c["pending"], p["closed"] + p["pending"]),
                "conversion": change(c["closed_pending_pct"], p["closed_pending_pct"])}
    return c


def top_agents_closed(cur, f, limit=5):
    names = agent_names(cur)
    return [{"name": names.get(r["uid"], f"User {r['uid']}"), "deals": r["n"], "volume": r["vol"]}
            for r in fetch(cur, f"""
                SELECT unnest(d.user_ids) AS uid, COUNT(*) AS n, COALESCE(SUM(d.price), 0) AS vol FROM deals d
                WHERE {DEAL_CLASS} = 'closed' AND {DEAL_DATE} >= %(start)s AND {DEAL_DATE} < %(end)s AND {DEALS_F}
                GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT {int(limit)}""", f.params())]


def stage_counts(cur, f, date_col="created_at"):
    """{bucket: count} for leads whose date_col falls in the period."""
    counts = {k: 0 for k, _, _ in BUCKETS}
    for r in fetch(cur, f"""
            SELECT p.stage, COUNT(*) AS n FROM people p
            WHERE p.{date_col} >= %(start)s AND p.{date_col} < %(end)s AND {PEOPLE_F} GROUP BY 1""", f.params()):
        counts[bucket_for(r["stage"])] += r["n"]
    return counts


def grouped_stages(counts):
    total = sum(counts.values())
    return [{"label": label, "color": color, "count": sum(counts[m] for m in members),
             "pct": pct(sum(counts[m] for m in members), total)}
            for label, color, members in SOURCE_GROUPS]


def leads_by_month(cur, f):
    return fetch(cur, f"""
        SELECT date_trunc('month', p.created_at AT TIME ZONE %(tz)s) AS month,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE p.assigned_user_id IS NOT NULL) AS handed_out,
               COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM agent_events e WHERE e.person_id = p.person_id
                                              AND e.event_type = ANY(%(contact_types)s))) AS contacted,
               COUNT(*) FILTER (WHERE LOWER(TRIM(p.stage)) = ANY(%(closed_stages)s)) AS closed
        FROM people p
        WHERE p.created_at >= %(start)s AND p.created_at < %(end)s AND {PEOPLE_F}
        GROUP BY 1 ORDER BY 1 DESC""", f.params())


def monthly_series(cur, f, year, kind):
    """12 monthly values for `year`: kind = 'leads' | 'deals' (closed + pending) | 'volume' (closed $)."""
    tzs = "AT TIME ZONE %(tz)s"
    if kind == "leads":
        sql = f"""SELECT EXTRACT(MONTH FROM p.created_at {tzs})::int AS m, COUNT(*) AS n FROM people p
                  WHERE EXTRACT(YEAR FROM p.created_at {tzs}) = %(year)s AND {PEOPLE_F} GROUP BY 1"""
    else:
        classes = "('closed', 'pending')" if kind == "deals" else "('closed')"
        agg = "COUNT(*)" if kind == "deals" else "COALESCE(SUM(d.price), 0)"
        sql = f"""SELECT EXTRACT(MONTH FROM {DEAL_DATE} {tzs})::int AS m, {agg} AS n FROM deals d
                  WHERE {DEAL_CLASS} IN {classes} AND EXTRACT(YEAR FROM {DEAL_DATE} {tzs}) = %(year)s
                    AND {DEALS_F} GROUP BY 1"""
    vals = [0] * 12
    for r in fetch(cur, sql, f.params(year=year)):
        vals[r["m"] - 1] = float(r["n"])
    return vals


# ---------------------------------------------------------------- Business Overview

def business_months(cur, f, year):
    """Per-month deal KPIs for a calendar year."""
    p = f.params(year=year)
    tzs = "AT TIME ZONE %(tz)s"
    written = {r["m"]: r["n"] for r in fetch(cur, f"""
        SELECT EXTRACT(MONTH FROM d.created_at {tzs})::int AS m, COUNT(*) AS n FROM deals d
        WHERE {DEAL_CLASS} <> 'cancelled' AND EXTRACT(YEAR FROM d.created_at {tzs}) = %(year)s AND {DEALS_F}
        GROUP BY 1""", p)}
    done = {r["m"]: r for r in fetch(cur, f"""
        SELECT EXTRACT(MONTH FROM {DEAL_DATE} {tzs})::int AS m, COUNT(*) AS n, COALESCE(SUM(d.price), 0) AS vol
        FROM deals d WHERE {DEAL_CLASS} IN ('closed', 'pending') AND EXTRACT(YEAR FROM {DEAL_DATE} {tzs}) = %(year)s
          AND {DEALS_F} GROUP BY 1""", p)}
    months = []
    for m in range(1, 13):
        d = done.get(m, {"n": 0, "vol": 0})
        months.append({"month": m, "accepted": written.get(m, 0), "deals": d["n"], "volume": d["vol"],
                       "avg": (d["vol"] / d["n"]) if d["n"] else 0})
    return months


def business_quarters(months):
    quarters = []
    for q in range(4):
        ms = months[q * 3:(q + 1) * 3]
        n, vol = sum(m["deals"] for m in ms), sum(m["volume"] for m in ms)
        quarters.append({"q": q + 1, "accepted": sum(m["accepted"] for m in ms), "deals": n,
                         "volume": vol, "avg": vol / n if n else 0, "months": ms})
    n, vol = sum(q["deals"] for q in quarters), sum(q["volume"] for q in quarters)
    total = {"accepted": sum(q["accepted"] for q in quarters), "deals": n, "volume": vol,
             "avg": vol / n if n else 0}
    return quarters, total


def business_top(cur, f, since):
    names = agent_names(cur)
    rows = fetch(cur, f"""
        SELECT unnest(d.user_ids) AS uid, COUNT(*) AS n, COALESCE(SUM(d.price), 0) AS vol FROM deals d
        WHERE {DEAL_CLASS} IN ('closed', 'pending') AND {DEAL_DATE} >= %(since)s AND {DEALS_F}
        GROUP BY 1""", f.params(since=since))
    for r in rows:
        r["name"] = names.get(r["uid"], f"User {r['uid']}")
        r["avg"] = r["vol"] / r["n"] if r["n"] else 0
    top = lambda key: sorted(rows, key=lambda r: r[key], reverse=True)[:5]
    return {"volume": top("vol"), "deals": top("n"), "avg": top("avg")}


# ---------------------------------------------------------------- Best Call Time

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
CALL_HOURS = list(range(8, 20))  # 8AM–7PM, like Maverick
MIN_CALLS_FOR_INSIGHT = 5


def hour_label(h):
    return f"{(h - 1) % 12 + 1}{'AM' if h < 12 else 'PM'}"


def call_time(cur, f):
    grid = {(d, h): [0, 0] for d in range(1, 8) for h in range(24)}  # (isodow, hour) -> [calls, convos]
    for r in fetch(cur, f"""
            SELECT EXTRACT(ISODOW FROM e.created_at AT TIME ZONE %(tz)s)::int AS d,
                   EXTRACT(HOUR FROM e.created_at AT TIME ZONE %(tz)s)::int AS h,
                   COUNT(*) FILTER (WHERE e.event_type = 'attempt') AS calls,
                   COUNT(*) FILTER (WHERE e.event_type = 'conversation') AS convos
            FROM agent_events e
            WHERE e.event_type IN ('attempt', 'conversation')
              AND e.created_at >= %(start)s AND e.created_at < %(end)s AND {EVENTS_F}
            GROUP BY 1, 2""", f.params()):
        grid[(r["d"], r["h"])] = [r["calls"], r["convos"]]

    def series(days):
        calls = [sum(grid[(d, h)][0] for d in days) for h in CALL_HOURS]
        convos = [sum(grid[(d, h)][1] for d in days) for h in CALL_HOURS]
        return {"calls": calls, "rate": [pct(c, n) for c, n in zip(convos, calls)]}

    views = {"Weekly": series(range(1, 8))}
    for i, name in enumerate(DAYS, start=1):
        views[name] = series([i])

    total_calls = sum(v[0] for v in grid.values())
    total_convos = sum(v[1] for v in grid.values())
    avg_rate = pct(total_convos, total_calls, 2)

    # Quick insights
    slots = [(d, h, c, n) for (d, h), (c, n) in grid.items() if c >= MIN_CALLS_FOR_INSIGHT]
    by_day = {d: [sum(grid[(d, h)][0] for h in range(24)), sum(grid[(d, h)][1] for h in range(24))]
              for d in range(1, 8)}
    by_hour = {h: [sum(grid[(d, h)][0] for d in range(1, 8)), sum(grid[(d, h)][1] for d in range(1, 8))]
               for h in range(24)}
    working, improve = [], []

    def slot_text(d, h):
        return f"{DAY_NAMES[d - 1]} at {hour_label(h)} – {hour_label(h + 1)}"

    if slots:
        best = max(slots, key=lambda s: (pct(s[3], s[2]), s[2]))
        worst = min(slots, key=lambda s: (pct(s[3], s[2]), -s[2]))
        working.append(f"Your team's best conversation rate is {slot_text(best[0], best[1])}: "
                       f"{pct(best[3], best[2], 2)}% of {best[2]} calls.")
        improve.append(f"Your team's lowest conversation rate is {slot_text(worst[0], worst[1])}: "
                       f"{pct(worst[3], worst[2], 2)}% of {worst[2]} calls.")
    called_days = {d: v for d, v in by_day.items() if v[0]}
    if called_days:
        most = max(called_days, key=lambda d: called_days[d][0])
        least = min(called_days, key=lambda d: called_days[d][0])
        working.append(f"Your team made the most calls on {DAY_NAMES[most - 1]}s ({called_days[most][0]} calls).")
        improve.append(f"Your team made the fewest calls on {DAY_NAMES[least - 1]}s ({called_days[least][0]} calls).")
        good_days = [DAY_NAMES[d - 1] for d, (c, n) in called_days.items()
                     if c >= MIN_CALLS_FOR_INSIGHT and pct(n, c, 2) > avg_rate]
        bad_days = [DAY_NAMES[d - 1] for d, (c, n) in called_days.items()
                    if c >= MIN_CALLS_FOR_INSIGHT and pct(n, c, 2) < avg_rate]
        if good_days:
            working.append(f"Above-average conversation rate on: {', '.join(good_days)}.")
        if bad_days:
            improve.append(f"Below-average conversation rate on: {', '.join(bad_days)}.")
    good_hours = [hour_label(h) for h, (c, n) in sorted(by_hour.items())
                  if c >= MIN_CALLS_FOR_INSIGHT and pct(n, c, 2) > avg_rate]
    if good_hours:
        working.append(f"Hours with above-average conversation rate: {', '.join(good_hours)}.")

    return {"views": views, "hours": [hour_label(h) for h in CALL_HOURS], "avg_rate": avg_rate,
            "total_calls": total_calls, "total_convos": total_convos,
            "working": working, "improve": improve}


# ---------------------------------------------------------------- Agent Snapshot

def agent_info(cur, uid):
    if table_exists(cur, "agents"):
        rows = fetch(cur, "SELECT user_id, name, email, phone, picture_url FROM agents WHERE user_id = %(u)s",
                     {"u": uid})
        if rows:
            return rows[0]
    name = agent_names(cur).get(uid)
    return {"user_id": uid, "name": name or f"User {uid}", "email": "", "phone": "", "picture_url": ""} if name else None


def agent_funnel(cur, f):
    """New leads assigned in the period, and how far each got."""
    def counts(ff):
        return one(cur, f"""
            SELECT COUNT(*) AS new_leads,
                   COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM agent_events e WHERE e.person_id = p.person_id
                                                  AND e.event_type = ANY(%(contact_types)s))) AS contacted,
                   COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM appointments a WHERE a.person_id = p.person_id)) AS appt_set,
                   COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM appointments a WHERE a.person_id = p.person_id
                                                  AND {APPT_CLASS} = 'held')) AS appt_met,
                   COUNT(*) FILTER (WHERE LOWER(TRIM(p.stage)) = ANY(%(closed_stages)s)
                                    OR EXISTS (SELECT 1 FROM deals d WHERE p.person_id = ANY(d.person_ids)
                                               AND {DEAL_CLASS} IN ('closed', 'pending'))) AS closed
            FROM people p WHERE p.created_at >= %(start)s AND p.created_at < %(end)s AND {PEOPLE_F}""",
            ff.params())
    c, p = counts(f), counts(f.previous())
    steps = [("New Leads", "new_leads"), ("Contacted", "contacted"), ("Appt. Set", "appt_set"),
             ("Appt. Met", "appt_met"), ("Closed Deal", "closed")]
    return [{"label": label, "count": c[k], "pct": pct(c[k], c["new_leads"]), "chg": change(c[k], p[k])}
            for label, k in steps]


# Follow-up rules for Opportunities Waiting: contact every N days in each bucket
FOLLOW_UP_DAYS = {"new": 1, "attempted": 3, "appt": 7, "short": 7, "active": 3, "long": 30}
AT_RISK_SHARE = 0.75  # "at risk" once 75% of the window has passed without contact


def opportunities(cur, uid, now):
    rows = fetch(cur, f"""
        SELECT p.person_id, p.name, p.stage, p.created_at, c.last_contact
        FROM people p
        LEFT JOIN (SELECT person_id, MAX(created_at) AS last_contact FROM agent_events
                   WHERE person_id IS NOT NULL AND event_type = ANY(%(contact_types)s)
                   GROUP BY person_id) c ON c.person_id = p.person_id
        WHERE p.assigned_user_id = %(u)s AND {REAL_LEADS}""",
        {"u": uid, "contact_types": CONTACT_TYPES, "not_lead_stages": NOT_LEAD_STAGES, "source": None})
    rules = {b: {"label": BUCKET_LABEL[b], "days": d, "completed": 0, "at_risk": 0, "past_due": 0}
             for b, d in FOLLOW_UP_DAYS.items()}
    past_due = []
    for r in rows:
        b = bucket_for(r["stage"])
        if b not in rules:
            continue
        window = timedelta(days=FOLLOW_UP_DAYS[b])
        since = r["last_contact"] or r["created_at"]
        age = now - since if since else window * 2
        if r["last_contact"] and age <= window * AT_RISK_SHARE:
            status = "completed"
        elif age <= window:
            status = "at_risk"
        else:
            status = "past_due"
            past_due.append({"name": r["name"] or f"Lead {r['person_id']}", "stage": r["stage"],
                             "last_contact": r["last_contact"], "days_over": (age - window).days})
        rules[b][status] += 1
    totals = {k: sum(v[k] for v in rules.values()) for k in ("completed", "at_risk", "past_due")}
    totals["all"] = sum(totals.values())
    past_due.sort(key=lambda x: x["days_over"], reverse=True)
    return {"totals": totals, "rules": list(rules.values()), "past_due": past_due[:50],
            "past_due_count": len(past_due)}


def agent_financials(cur, f, uid, now):
    """Last 12 months of deals for one agent (source filter applies)."""
    ff = replace(f, agent=uid, start=now - timedelta(days=365), end=now + timedelta(days=1))
    p = ff.params()
    closed = fetch(cur, f"""
        SELECT d.price, {DEAL_DATE} AS at,
               (SELECT {SRC} FROM people p WHERE p.person_id = ANY(d.person_ids) LIMIT 1) AS source
        FROM deals d WHERE {DEAL_CLASS} = 'closed' AND {DEAL_DATE} >= %(start)s AND {DEALS_F}""", p)
    other = one(cur, f"""
        SELECT COALESCE(SUM(d.price) FILTER (WHERE {DEAL_CLASS} = 'pending'), 0) AS pending,
               COALESCE(SUM(d.price) FILTER (WHERE {DEAL_CLASS} IN ('accepted', 'active')
                                             AND d.created_at >= %(start)s), 0) AS accepted
        FROM deals d WHERE {DEALS_F}""", p)
    leads = funnel_counts(cur, ff)["new_leads"]
    team = replace(ff, agent=None)
    team_closed = one(cur, f"""SELECT COUNT(*) AS n FROM deals d WHERE {DEAL_CLASS} = 'closed'
                                AND {DEAL_DATE} >= %(start)s AND {DEALS_F}""", team.params())["n"]
    team_leads = funnel_counts(cur, team)["new_leads"]

    prices = [float(d["price"] or 0) for d in closed]
    by_source = {}
    for d in closed:
        by_source[d["source"] or "<unspecified>"] = by_source.get(d["source"] or "<unspecified>", 0) + float(d["price"] or 0)
    total = sum(prices)
    sources = sorted(({"source": s, "volume": v, "pct": pct(v, total)} for s, v in by_source.items()),
                     key=lambda x: x["volume"], reverse=True)
    return {"volume": total, "deals": len(prices), "min": min(prices) if prices else 0,
            "max": max(prices) if prices else 0, "avg": total / len(prices) if prices else 0,
            "pending": other["pending"], "accepted": other["accepted"], "closed": total,
            "conversion": pct(len(prices), leads, 3), "team_conversion": pct(team_closed, team_leads, 3),
            "sources": sources, "year": now.year,
            "this_year": monthly_series(cur, replace(f, agent=uid), now.year, "volume"),
            "last_year": monthly_series(cur, replace(f, agent=uid), now.year - 1, "volume")}


# Goals & Pacing metrics: (key, label, higher_is_better, unit)
GOAL_METRICS = [
    ("avg_call_min", "Average Call Time (min)", True, ""),
    ("conversations", "Conversations", True, ""),
    ("convos_per_appt", "Conversations per Appointment", False, ""),
    ("texts", "Texts Sent", True, ""),
    ("emails", "Emails Sent", True, ""),
    ("appts", "Total Appointments", True, ""),
    ("held", "Appointments Held", True, ""),
    ("appt_to_contract", "Appointments to Under Contract", True, "%"),
    ("trash_rate", "Assign-to-Trash Rate", False, "%"),
]


def goal_actuals(cur, f, under_contract=None):
    """under_contract: deals that went under contract in the period (the app
    passes the CTE count); falls back to FUB deals written."""
    p = f.params()
    e = one(cur, f"""
        SELECT COALESCE(AVG(e.duration_min) FILTER (WHERE e.event_type = 'conversation'), 0) AS avg_call_min,
               COUNT(*) FILTER (WHERE e.event_type = 'conversation') AS conversations,
               COUNT(*) FILTER (WHERE e.event_type = 'text') AS texts,
               COUNT(*) FILTER (WHERE e.event_type = 'email') AS emails
        FROM agent_events e WHERE e.created_at >= %(start)s AND e.created_at < %(end)s AND {EVENTS_F}""", p)
    lp = one(cur, f"""
        SELECT COUNT(*) AS leads, COUNT(*) FILTER (WHERE LOWER(TRIM(p.stage)) = ANY(%(trash_stages)s)) AS trashed
        FROM people p WHERE p.created_at >= %(start)s AND p.created_at < %(end)s AND {PEOPLE_F}""", p)
    fc = funnel_counts(cur, f)
    uc = fc["written"] if under_contract is None else under_contract
    return {"avg_call_min": round(float(e["avg_call_min"]), 1),
            "conversations": e["conversations"],
            "convos_per_appt": round(e["conversations"] / fc["appts_set"], 1) if fc["appts_set"] else 0,
            "texts": e["texts"], "emails": e["emails"],
            "appts": fc["appts_set"], "held": fc["held"],
            "appt_to_contract": pct(uc, fc["appts_set"]), "under_contract": uc,
            "trash_rate": pct(lp["trashed"], lp["leads"])}


# Goals are monthly per agent. user_id 0 holds the team defaults that apply to
# every agent without their own goal; these starting values are seeded once.
TEAM_GOALS_ID = 0
DEFAULT_GOALS = {"avg_call_min": 5, "conversations": 40, "convos_per_appt": 5, "texts": 300, "emails": 300,
                 "appts": 8, "held": 5, "appt_to_contract": 15, "trash_rate": 20}
# Count goals scale with the selected dates (a monthly goal of 40 is ~10 for a week);
# averages, ratios and % don't
SCALED_GOALS = {"conversations", "texts", "emails", "appts", "held"}
DAYS_PER_MONTH = 30.44


def goal_targets(cur, uid):
    """({metric: monthly target}, {metric: "agent" | "team"}) for one agent."""
    cur.execute("SELECT EXISTS (SELECT 1 FROM agent_goals WHERE user_id = %s)", (TEAM_GOALS_ID,))
    if not cur.fetchone()[0]:
        for metric, target in DEFAULT_GOALS.items():
            cur.execute("INSERT INTO agent_goals (user_id, metric, target) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (TEAM_GOALS_ID, metric, target))
    team = {r["metric"]: float(r["target"]) for r in fetch(
        cur, "SELECT metric, target FROM agent_goals WHERE user_id = %(u)s", {"u": TEAM_GOALS_ID})}
    own = {r["metric"]: float(r["target"]) for r in fetch(
        cur, "SELECT metric, target FROM agent_goals WHERE user_id = %(u)s", {"u": uid})} if uid != TEAM_GOALS_ID else {}
    targets = {**team, **own}
    return targets, {k: ("agent" if k in own else "team") for k in targets}, team, own


def goals_view(cur, f, uid, under_contract=None):
    actual = goal_actuals(cur, f, under_contract)
    targets, origin, team, own = goal_targets(cur, uid)
    scale = (f.end - f.start).total_seconds() / 86400 / DAYS_PER_MONTH
    out = []
    for key, label, higher, unit in GOAL_METRICS:
        a, monthly = float(actual[key]), targets.get(key)
        t = round(monthly * scale, 1) if monthly and key in SCALED_GOALS else monthly
        if not t:
            status, progress = "none", 0
        elif higher:
            progress = min(round(a / t * 100), 100)
            status = "meeting" if a >= t else "approaching" if a >= t * 0.75 else "below"
        else:  # a limit: lower is better
            progress = min(round(a / t * 100), 100)
            status = "meeting" if a <= t else "approaching" if a <= t * 1.25 else "below"
        out.append({"key": key, "label": label, "actual": actual[key], "target": t, "monthly": monthly,
                    "origin": origin.get(key), "team": team.get(key), "own": own.get(key),
                    "scaled": key in SCALED_GOALS, "unit": unit,
                    "higher": higher, "progress": progress, "status": status,
                    "detail": {"convos_per_appt": f"{actual['conversations']:,} conversations / {actual['appts']:,} appointments",
                               "appt_to_contract": f"{actual['under_contract']:,} under contract / {actual['appts']:,} appointments",
                               }.get(key)})
    return out


NOTE_TYPES = ["Simple Note", "1:1 Meeting", "Call Review", "Goal Check-in"]


def coaching_notes(cur, uid, author=None, note_type=None):
    return fetch(cur, """
        SELECT id, author, note_type, body, created_at FROM coaching_notes
        WHERE user_id = %(u)s AND (%(a)s::text IS NULL OR author = %(a)s)
          AND (%(t)s::text IS NULL OR note_type = %(t)s)
        ORDER BY created_at DESC""", {"u": uid, "a": author, "t": note_type})


# ---------------------------------------------------------------- Dashboard tabs

BUCKET_COLOR = {k: color for k, _, color in BUCKETS}


def pipeline_health(cur, f):
    """New leads in the period by current stage, plus how many sat in "New" 48h+."""
    counts = stage_counts(cur, f)
    stale_new = 0
    unmapped = set()
    for r in fetch(cur, f"""
            SELECT p.stage, COUNT(*) FILTER (WHERE p.created_at < NOW() - INTERVAL '48 hours') AS old
            FROM people p WHERE p.created_at >= %(start)s AND p.created_at < %(end)s AND {PEOPLE_F}
            GROUP BY 1""", f.params()):
        b = bucket_for(r["stage"])
        if b == "new":
            stale_new += r["old"]
        if b == "other":
            unmapped.add(r["stage"] or "(blank)")
    total = sum(counts.values())

    rows, stops, running = [], [], 0
    for k, label, color in BUCKETS:
        if k == "other" and counts[k] == 0:
            continue
        share = pct(counts[k], total)
        rows.append({"label": label, "color": color, "count": counts[k], "pct": share})
        if counts[k]:
            stops.append(f"{color} {running}% {running + share}%")
            running += share
    return {"total": total, "rows": rows,
            "pie": "conic-gradient(" + ", ".join(stops) + ")" if stops else "#EEF0F5",
            "stale_new_pct": pct(stale_new, total), "unmapped": sorted(unmapped)}


def lead_response(cur, f, as_of):
    """Share of Active / Long Nurture leads contacted in the 3 / 30 days before as_of.
    Stage is each lead's current stage in Follow Up Boss."""
    totals = {"active": [0, 0], "long": [0, 0]}
    for r in fetch(cur, f"""
            SELECT p.stage, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE c.last_contact >= %(as_of)s - INTERVAL '3 days') AS d3,
                   COUNT(*) FILTER (WHERE c.last_contact >= %(as_of)s - INTERVAL '30 days') AS d30
            FROM people p
            LEFT JOIN (SELECT person_id, MAX(created_at) AS last_contact FROM agent_events
                       WHERE person_id IS NOT NULL AND event_type = ANY(%(contact_types)s) AND created_at < %(as_of)s
                       GROUP BY person_id) c ON c.person_id = p.person_id
            WHERE {PEOPLE_F} GROUP BY 1""", f.params(as_of=as_of)):
        b = bucket_for(r["stage"])
        if b == "active":
            totals["active"][0] += r["n"]
            totals["active"][1] += r["d3"]
        elif b == "long":
            totals["long"][0] += r["n"]
            totals["long"][1] += r["d30"]
    return [
        {"label": BUCKET_LABEL["active"], "window": "Contacted within 3 days",
         "total": totals["active"][0], "hit": totals["active"][1],
         "pct": round(pct(totals["active"][1], totals["active"][0]))},
        {"label": BUCKET_LABEL["long"], "window": "Contacted within 30 days",
         "total": totals["long"][0], "hit": totals["long"][1],
         "pct": round(pct(totals["long"][1], totals["long"][0]))},
    ]


MIN_LEADS_FOR_BEST = 5


def best_sources(cur, f):
    """Leads created in the period, per source, split by current stage group."""
    by_source = {}
    for r in fetch(cur, f"""
            SELECT {SRC} AS source, p.stage, COUNT(*) AS n FROM people p
            WHERE p.created_at >= %(start)s AND p.created_at < %(end)s AND {PEOPLE_F}
            GROUP BY 1, 2""", f.params()):
        counts = by_source.setdefault(r["source"], {k: 0 for k, _, _ in BUCKETS})
        counts[bucket_for(r["stage"])] += r["n"]

    sources = []
    for source, counts in by_source.items():
        total = sum(counts.values())
        won = counts["closed"] + counts["active"]
        sources.append({"name": source, "total": total, "segs": grouped_stages(counts),
                        "won_pct": pct(won, total)})
    # "Best" = highest share of leads now Active or Closed, among sources with enough volume
    sources.sort(key=lambda s: (s["total"] >= MIN_LEADS_FOR_BEST, s["won_pct"], s["total"]), reverse=True)
    return sources
