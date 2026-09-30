"""Goals page: the Oct 2026 - Mar 2027 team plan, actual vs target for every row.

Targets are the owner's plan (PLAN below). Actuals come from CTE (closings, contracts,
pending), Follow Up Boss (leads, speed to lead, appointments, recruit calls, agents),
QuickBooks (net income, expenses) and a few numbers typed in on the page (open houses,
ad spend) that no connected system records.
"""
from datetime import date, datetime, timedelta

import cte_reports as CTE
import qbo
from reports import APPT_CLASS, CONTACT_TYPES, NOT_LEAD_STAGES, REAL_LEADS, DEAL_CLASS, fetch, pct, table_exists

PLAN_MONTHS = [(2026, 10), (2026, 11), (2026, 12), (2027, 1), (2027, 2), (2027, 3)]
MAIN_NET_GOAL = 1_000_000
MODELED_NOI = (550_000, 600_000)  # annualized NOI the plan reaches by end of Q1 2027

# (key, label, targets per plan month, higher_is_better)
MONTHLY = [
    ("closings", "Total closings", [10, 10, 12, 11, 11, 14], True),
    ("joe", "Joe", [2, 2, 2, 2, 2, 2], True),
    ("established", "Established (11)", [7, 7, 8, 7, 7, 8], True),
    ("ramping", "Ramping (6)", [1, 1, 2, 2, 2, 3], True),
    ("recruits", "Recruits signed", [0, 1, 1, 2, 2, 1], True),
    ("headcount", "Headcount", [18, 19, 20, 22, 24, 25], True),
    ("expenses", "Expense cap", [46_000, 46_000, 46_000, 50_000, 50_000, 50_000], False),
]
GROUPS = [("joe", "Joe"), ("established", "Established"), ("ramping", "Ramping"), ("none", "Not counted")]


def quarter_target(day, q4, q1):
    return q4 if day < date(2027, 1, 1) else q1


WEEKLY_TARGETS = {  # per week; (Q4 2026, Q1 2027)
    "contracts": (3, 3.5), "pending": (14, 16), "open_houses": (10, 14), "recruit_convos": (3, 4)}
APPTS_PER_WEEK = {"established": 2, "ramping": 3}  # held appointments per agent per week

PPC_BUDGET = {"google": 1_100, "meta": 700, "youtube": 0}
PPC_COST_GATE = 2_500
PPC_GATE_DATE = date(2027, 1, 15)
# Follow Up Boss lead sources that are paid ads (organic search is left out)
PPC_SOURCE_RE = r"(google|ppc|adwords|meta|facebook|\mfb|instagram|youtube)"
PPC_EXCLUDE_RE = r"organic"

NEWEST_AGENTS_GATE_DATE = date(2027, 1, 15)
PENDING_GATE_DATE = date(2026, 12, 1)


def month_bounds(y, m, tz):
    start = datetime(y, m, 1, tzinfo=tz)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=tz)
    return start, end


# ---------------------------------------------------------------- stored settings

def ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS goal_agent_groups (
                       user_id BIGINT PRIMARY KEY, grp TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT now())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS goal_inputs (
                       key TEXT, period DATE, value NUMERIC, updated_at TIMESTAMPTZ DEFAULT now(),
                       PRIMARY KEY (key, period))""")


def save_groups(cur, groups):
    ensure_tables(cur)
    for uid, grp in groups.items():
        cur.execute("""INSERT INTO goal_agent_groups (user_id, grp) VALUES (%s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET grp = EXCLUDED.grp, updated_at = now()""", (uid, grp))


def save_input(cur, key, period, value):
    ensure_tables(cur)
    if value is None:
        cur.execute("DELETE FROM goal_inputs WHERE key = %s AND period = %s", (key, period))
    else:
        cur.execute("""INSERT INTO goal_inputs (key, period, value) VALUES (%s, %s, %s)
                       ON CONFLICT (key, period) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                    (key, period, value))


def inputs(cur):
    ensure_tables(cur)
    return {(r["key"], r["period"]): float(r["value"]) for r in fetch(cur, "SELECT key, period, value FROM goal_inputs", {})}


# ---------------------------------------------------------------- roster

def roster(cur):
    """Active agents with their plan group (saved, else suggested) and CTE name."""
    ensure_tables(cur)
    has_created = bool(fetch(cur, """SELECT 1 FROM information_schema.columns
                                     WHERE table_name = 'agents' AND column_name = 'created_at'""", {}))
    rows = fetch(cur, f"""
        SELECT a.user_id, a.name, {'a.created_at' if has_created else 'NULL::timestamptz AS created_at'}, g.grp
        FROM agents a LEFT JOIN goal_agent_groups g ON g.user_id = a.user_id
        WHERE LOWER(COALESCE(a.status, '')) IN ('', 'active') AND LOWER(COALESCE(a.role, '')) <> 'lender'
          AND a.name !~* 'follow up boss'
        ORDER BY a.name""", {})
    cte_ready = CTE.ready(cur)
    this_year = date.today().year
    for r in rows:
        r["cte_name"] = CTE.name_for(cur, r["name"]) if cte_ready else None
        if "joe corbisiero" in r["name"].lower():
            suggested = "joe"
        elif r["cte_name"] and fetch(cur, """SELECT 1 FROM cte_deals WHERE status = 'Closed' AND file_year < %(y)s
                                               AND LOWER(TRIM(primary_agent)) = LOWER(%(n)s) LIMIT 1""",
                                     {"y": this_year, "n": r["cte_name"]}):
            suggested = "established"  # closed a deal in an earlier year
        else:
            suggested = "ramping"
        r["suggested"] = suggested
        r["group"] = r["grp"] or suggested
    return rows


def _cte_group_map(people):
    return {p["cte_name"].lower(): p["group"] for p in people if p["cte_name"]}


# ---------------------------------------------------------------- monthly targets

def monthly(cur, tz, people):
    today = datetime.now(tz)
    cte_map = _cte_group_map(people)
    books = qbo.status(cur)
    books_live = bool(books and books["env"] == "production" and not books["needs_reconnect"])
    agent_rows = fetch(cur, """SELECT user_id, created_at FROM agents
                               WHERE LOWER(COALESCE(role, '')) <> 'lender'""", {}) \
        if any(p["created_at"] for p in people) else []
    cols = []
    for i, (y, m) in enumerate(PLAN_MONTHS):
        start, end = month_bounds(y, m, tz)
        state = "future" if start > today else "current" if end > today else "done"
        col = {"label": start.strftime("%b %Y"), "state": state, "start": start, "end": end}
        if state != "future":
            closed = fetch(cur, """SELECT LOWER(TRIM(primary_agent)) AS a, COUNT(*) AS n FROM cte_deals
                                   WHERE status = 'Closed' AND close_date >= %(s)s AND close_date < %(e)s
                                     AND EXTRACT(YEAR FROM close_date) = file_year GROUP BY 1""",
                           {"s": start.date(), "e": end.date()}) if CTE.ready(cur) else []
            col["closings"] = sum(r["n"] for r in closed)
            for g in ("joe", "established", "ramping"):
                col[g] = sum(r["n"] for r in closed if cte_map.get(r["a"] or "") == g)
            col["contracts"] = fetch(cur, """SELECT COUNT(*) AS n FROM cte_deals
                                             WHERE under_contract_date >= %(s)s AND under_contract_date < %(e)s
                                               AND EXTRACT(YEAR FROM under_contract_date) = file_year""",
                                     {"s": start.date(), "e": end.date()})[0]["n"] if CTE.ready(cur) else None
            if agent_rows:
                col["recruits"] = sum(1 for a in agent_rows if a["created_at"] and start <= a["created_at"] < end)
            if state == "current":
                col["headcount"] = len(people)
            elif agent_rows:  # agents added by month end who are still active (leavers aren't known)
                col["headcount"] = sum(1 for p in people if p["created_at"] and p["created_at"] < end)
            if books_live:
                exp = fetch(cur, "SELECT expenses FROM qbo_pnl WHERE month = %(m)s", {"m": start.date()})
                col["expenses"] = float(exp[0]["expenses"]) if exp else None
        cols.append(col)
    rows = []
    for key, label, targets, higher in MONTHLY:
        cells = []
        for col, target in zip(cols, targets):
            actual = col.get(key)
            if col["state"] == "future" or actual is None:
                status = "none"
            elif higher:
                status = "ok" if actual >= target else ("behind" if col["state"] == "done" else "running")
            else:
                status = "ok" if actual <= target else "behind"
            cells.append({"actual": actual, "target": target, "status": status, "state": col["state"]})
        rows.append({"key": key, "label": label, "cells": cells, "money": key == "expenses"})
    return {"cols": cols, "rows": rows, "books_live": books_live}


# ---------------------------------------------------------------- weekly scorecard

def week_start(d):
    return d - timedelta(days=d.weekday())  # Monday


def weekly(cur, tz, people, weeks=6):
    today = datetime.now(tz)
    this_monday = week_start(today.date())
    vals = inputs(cur)
    by_uid = {p["user_id"]: p["group"] for p in people}
    n_group = {g: sum(1 for p in people if p["group"] == g) for g in ("established", "ramping")}
    joe = next((p["user_id"] for p in people if p["group"] == "joe"), None)
    cols = []
    for k in range(weeks - 1, -1, -1):
        monday = this_monday - timedelta(weeks=k)
        start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
        end = start + timedelta(days=7)
        c = {"label": f"{monday.strftime('%b %d')}", "monday": monday, "current": k == 0}
        c["contracts"] = fetch(cur, """SELECT COUNT(*) AS n FROM cte_deals
                                       WHERE under_contract_date >= %(s)s AND under_contract_date < %(e)s
                                         AND EXTRACT(YEAR FROM under_contract_date) = file_year""",
                               {"s": start.date(), "e": end.date()})[0]["n"] if CTE.ready(cur) else None
        c["open_houses"] = vals.get(("open_houses", monday))
        held = fetch(cur, f"""SELECT unnest(a.agent_ids) AS uid, COUNT(*) AS n FROM appointments a
                              WHERE {APPT_CLASS} = 'held' AND a.start_at >= %(s)s AND a.start_at < %(e)s
                              GROUP BY 1""", {"s": start, "e": end}) if table_exists(cur, "appointments") else []
        for g in ("established", "ramping"):
            total = sum(r["n"] for r in held if by_uid.get(r["uid"]) == g)
            c[f"held_{g}"] = total
            c[f"held_{g}_per"] = round(total / n_group[g], 1) if n_group[g] else None
        c["recruit_convos"] = fetch(cur, """
            SELECT COUNT(*) AS n FROM agent_events e JOIN people p ON p.person_id = e.person_id
            WHERE e.event_type = 'conversation' AND e.user_id = %(u)s AND LOWER(TRIM(p.stage)) = 'real estate agent'
              AND e.created_at >= %(s)s AND e.created_at < %(e)s""", {"u": joe, "s": start, "e": end})[0]["n"] \
            if joe else None
        lead = lead_flow(cur, start, end)
        c.update(leads=lead["leads"], fast=lead["fast"], fast_pct=lead["fast_pct"], appts_set=lead["appts_set"])
        cols.append(c)
    pending_now = None
    if CTE.ready(cur):
        pending_now = fetch(cur, """SELECT COUNT(*) AS n FROM cte_deals WHERE status = 'Pending'
                                    AND file_year = (SELECT MAX(file_year) FROM cte_deals)""", {})[0]["n"]

    def t(key, monday):
        q4, q1 = WEEKLY_TARGETS[key]
        return quarter_target(monday, q4, q1)
    rows = [
        {"label": "Contracts written (team)", "cells": [(c["contracts"], t("contracts", c["monday"])) for c in cols]},
        {"label": "Open houses (team)", "cells": [(c["open_houses"], t("open_houses", c["monday"])) for c in cols],
         "manual": True},
        {"label": f"Appointments held: established ({n_group['established']} agents, 2/wk each)",
         "cells": [(c["held_established"], APPTS_PER_WEEK["established"] * n_group["established"]) for c in cols],
         "per": [c["held_established_per"] for c in cols]},
        {"label": f"Appointments held: ramping ({n_group['ramping']} agents, 3/wk each)",
         "cells": [(c["held_ramping"], APPTS_PER_WEEK["ramping"] * n_group["ramping"]) for c in cols],
         "per": [c["held_ramping_per"] for c in cols]},
        {"label": "Recruit conversations (Joe)", "cells": [(c["recruit_convos"], t("recruit_convos", c["monday"]))
                                                           for c in cols], "note": "calls of 2+ min with FUB contacts in stage Real Estate Agent"},
    ]
    for r in rows:
        r["cells"] = [{"actual": a, "target": tg, "current": col["current"],
                       "status": "none" if a is None else "ok" if a >= tg else ("running" if col["current"] else "behind")}
                      for (a, tg), col in zip(r["cells"], cols)]
    track = [
        {"label": "Leads received", "vals": [c["leads"] for c in cols]},
        {"label": "Contacted within 5 min", "vals": [f"{c['fast']} ({c['fast_pct']:.0f}%)" if c["leads"] else "0"
                                                     for c in cols]},
        {"label": "Appointments set", "vals": [c["appts_set"] for c in cols]},
    ]
    today_d = today.date()
    return {"cols": cols, "rows": rows, "track": track, "pending_now": pending_now,
            "pending_target": quarter_target(today_d, *WEEKLY_TARGETS["pending"])}


def lead_flow(cur, start, end, by_channel=False):
    """Leads created in [start, end), how many got a first call/text/email within 5 minutes,
    and appointments set in the same window; optionally split by lead source."""
    p = {"s": start, "e": end, "contact_types": CONTACT_TYPES, "not_lead_stages": NOT_LEAD_STAGES, "source": None}
    leads = fetch(cur, f"""
        SELECT COALESCE(NULLIF(TRIM(p.source), ''), '<unspecified>') AS source,
               (SELECT MIN(e.created_at) FROM agent_events e WHERE e.person_id = p.person_id
                  AND e.event_type = ANY(%(contact_types)s) AND e.created_at >= p.created_at) - p.created_at AS wait
        FROM people p WHERE p.created_at >= %(s)s AND p.created_at < %(e)s AND {REAL_LEADS}""", p)
    appts = fetch(cur, """SELECT COALESCE(NULLIF(TRIM(p.source), ''), '<unspecified>') AS source, COUNT(*) AS n
                          FROM appointments a LEFT JOIN people p ON p.person_id = a.person_id
                          WHERE a.created_at >= %(s)s AND a.created_at < %(e)s GROUP BY 1""", p) \
        if table_exists(cur, "appointments") else []
    fast = [l for l in leads if l["wait"] is not None and l["wait"] <= timedelta(minutes=5)]
    out = {"leads": len(leads), "fast": len(fast), "fast_pct": pct(len(fast), len(leads), 0),
           "appts_set": sum(a["n"] for a in appts)}
    if by_channel:
        chans = {}
        for l in leads:
            ch = chans.setdefault(l["source"], {"source": l["source"], "leads": 0, "fast": 0, "appts": 0})
            ch["leads"] += 1
            ch["fast"] += l in fast
        for a in appts:
            chans.setdefault(a["source"], {"source": a["source"], "leads": 0, "fast": 0, "appts": 0})["appts"] += a["n"]
        for ch in chans.values():
            ch["fast_pct"] = pct(ch["fast"], ch["leads"], 0)
        out["channels"] = sorted(chans.values(), key=lambda c: (-c["leads"], -c["appts"]))
    return out


# ---------------------------------------------------------------- lead spend

def lead_spend(cur, tz):
    """Ad spend (typed in) vs budget and cost per closing from PPC leads, by lead-created month."""
    vals = inputs(cur)
    today = datetime.now(tz)
    months = []
    has_deals = table_exists(cur, "deals")
    for y, m in PLAN_MONTHS:
        start, end = month_bounds(y, m, tz)
        if start > today:
            months.append({"label": start.strftime("%b %Y"), "future": True, "period": start.date()})
            continue
        spend = {ch: vals.get((f"ppc_{ch}", start.date())) for ch in PPC_BUDGET}
        total = sum(v for v in spend.values() if v is not None) if any(v is not None for v in spend.values()) else None
        closings = fetch(cur, f"""
            SELECT COUNT(DISTINCT d.deal_id) AS n FROM deals d JOIN people p ON p.person_id = ANY(d.person_ids)
            WHERE {DEAL_CLASS} = 'closed' AND p.created_at >= %(s)s AND p.created_at < %(e)s
              AND p.source ~* %(re)s AND p.source !~* %(ex)s""",
                         {"s": start, "e": end, "re": PPC_SOURCE_RE, "ex": PPC_EXCLUDE_RE})[0]["n"] if has_deals else 0
        leads = fetch(cur, """SELECT COUNT(*) AS n FROM people p WHERE p.created_at >= %(s)s AND p.created_at < %(e)s
                              AND p.source ~* %(re)s AND p.source !~* %(ex)s""",
                      {"s": start, "e": end, "re": PPC_SOURCE_RE, "ex": PPC_EXCLUDE_RE})[0]["n"]
        months.append({"label": start.strftime("%b %Y"), "future": False, "period": start.date(), "spend": spend,
                       "total": total, "budget": sum(PPC_BUDGET.values()), "leads": leads, "closings": closings,
                       "cost_per_closing": total / closings if total is not None and closings else None})
    spent = sum(mo["total"] or 0 for mo in months if not mo["future"])
    closed = sum(mo["closings"] for mo in months if not mo["future"])
    sources = [r["s"] for r in fetch(cur, """SELECT DISTINCT TRIM(source) AS s FROM people
                                             WHERE source ~* %(re)s AND source !~* %(ex)s ORDER BY 1""",
                                     {"re": PPC_SOURCE_RE, "ex": PPC_EXCLUDE_RE})]
    return {"months": months, "spent": spent, "closings": closed,
            "cost_per_closing": spent / closed if closed and spent else None, "sources": sources}


# ---------------------------------------------------------------- gates

def gates(cur, tz, people, mon, week, spend):
    today = datetime.now(tz).date()
    out = []

    def gate(name, status, detail):
        out.append({"name": name, "status": status, "detail": detail})

    # 1. newest 6 agents without a contract by Jan 15
    dated = sorted((p for p in people if p["created_at"] and p["group"] != "joe"),
                   key=lambda p: p["created_at"], reverse=True)[:6]
    if dated and CTE.ready(cur):
        none = [p["name"] for p in dated if not p["cte_name"] or not fetch(cur, """
            SELECT 1 FROM cte_deals WHERE under_contract_date IS NOT NULL
              AND LOWER(TRIM(primary_agent)) = LOWER(%(n)s) LIMIT 1""", {"n": p["cte_name"]})]
        status = ("trigger" if len(none) >= 3 else "ok") if today >= NEWEST_AGENTS_GATE_DATE else \
            ("watch" if len(none) >= 3 else "ok")
        gate("3+ of newest 6 agents with no contract by Jan 15 → pause recruiting, coach", status,
             f"{len(none)} of the newest {len(dated)} have no contract yet" + (f": {', '.join(none)}" if none else ""))
    else:
        gate("3+ of newest 6 agents with no contract by Jan 15 → pause recruiting, coach", "none",
             "Needs agent start dates from Follow Up Boss (filled on the next sync).")

    # 2. contracts under 12 in a month
    done = [c for c in mon["cols"] if c["state"] == "done" and c.get("contracts") is not None]
    cur_col = next((c for c in mon["cols"] if c["state"] == "current"), None)
    low = [c["label"] for c in done if c["contracts"] < 12]
    detail = ", ".join(f"{c['label']}: {c['contracts']}" for c in done) or "No full month yet"
    if cur_col and cur_col.get("contracts") is not None:
        detail += f" · {cur_col['label']} so far: {cur_col['contracts']}"
    gate("Contracts under 12 in a month → cut recruiting to 1", "trigger" if low else "ok", detail)

    # 3. pending under 12 on Dec 1
    p_now = week["pending_now"]
    if p_now is None:
        gate("Pending under 12 on Dec 1 → push open houses and Zillow follow-up", "none", "No CTE data")
    else:
        status = ("trigger" if p_now < 12 else "ok") if today >= PENDING_GATE_DATE else ("watch" if p_now < 12 else "ok")
        gate("Pending under 12 on Dec 1 → push open houses and Zillow follow-up", status, f"{p_now} pending now")

    # 4. spend over cap two months running
    exp_row = next(r for r in mon["rows"] if r["key"] == "expenses")
    over = [c["status"] == "behind" for c in exp_row["cells"] if c["state"] == "done" and c["actual"] is not None]
    two = any(a and b for a, b in zip(over, over[1:]))
    if not mon["books_live"]:
        gate("Spend over cap 2 months in a row → review", "none", "Needs QuickBooks connected to the real books.")
    else:
        gate("Spend over cap 2 months in a row → review", "trigger" if two else ("watch" if any(over) else "ok"),
             f"{sum(over)} month(s) over cap so far")

    # 5. TC/ops hire before headcount 22
    hc = len(people)
    gate("Hire TC/ops person before headcount hits 22", "trigger" if hc >= 22 else "watch" if hc >= 20 else "ok",
         f"Headcount now {hc}")

    # 6. PPC scale gate on Jan 15
    cpc = spend["cost_per_closing"]
    if cpc is None:
        status, detail = "none", "Needs ad spend typed in and PPC closings"
    else:
        status = ("ok" if cpc <= PPC_COST_GATE else "trigger") if today >= PPC_GATE_DATE else \
            ("ok" if cpc <= PPC_COST_GATE else "watch")
        detail = f"${cpc:,.0f} per closing so far (gate ${PPC_COST_GATE:,})"
    gate("Jan 15: scale PPC only if cost per closing ≤ ~$2.5K", status, detail)
    return out


# ---------------------------------------------------------------- main goal

def main_goal(cur):
    """Annualized net from QuickBooks (last 3 full months × 4) and trailing 12 months, vs $1M."""
    books = qbo.status(cur)
    if not books or books["env"] != "production" or books["needs_reconnect"]:
        return {"live": False, "goal": MAIN_NET_GOAL, "modeled": MODELED_NOI}
    first = date.today().replace(day=1)
    rows = fetch(cur, "SELECT month, net_income FROM qbo_pnl WHERE month < %(m)s ORDER BY month DESC LIMIT 12",
                 {"m": first})
    last3 = rows[:3]
    annualized = sum(float(r["net_income"]) for r in last3) * 4 if len(last3) == 3 else None
    ttm = sum(float(r["net_income"]) for r in rows) if len(rows) == 12 else None
    return {"live": True, "goal": MAIN_NET_GOAL, "modeled": MODELED_NOI, "annualized": annualized, "ttm": ttm,
            "pct": annualized / MAIN_NET_GOAL * 100 if annualized is not None else None,
            "last3": [r["month"] for r in last3]}


def page(cur, tz):
    people = roster(cur)
    mon = monthly(cur, tz, people)
    week = weekly(cur, tz, people)
    spend = lead_spend(cur, tz)
    today = datetime.now(tz)
    four_weeks_ago = datetime.combine(week_start(today.date()) - timedelta(weeks=3), datetime.min.time(), tz)
    channels = lead_flow(cur, four_weeks_ago, today + timedelta(days=1), by_channel=True)
    return {"people": people, "monthly": mon, "weekly": week, "spend": spend, "channels": channels,
            "gates": gates(cur, tz, people, mon, week, spend), "main": main_goal(cur), "groups": GROUPS,
            "ppc_budget": PPC_BUDGET, "ppc_re": PPC_SOURCE_RE}
