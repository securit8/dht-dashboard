"""Report queries for the CTE workbook data loaded by cte_import.py.

Agents are identified by the name typed in CTE (not a FUB id). Each deal
event is counted only from the file of the year it happened in, so deals
carried over from one year's file into the next aren't double counted.
Team income comes from the Financial Statement (it includes referral and
other income that isn't in the deal log); per-agent GCI comes from deals.
"""
from datetime import timedelta

from reports import fetch, one, table_exists, tables_ready

ACTIVITY = ["dials", "contacts", "nurtures", "listing_appts_set", "listing_appts_held", "listings_signed",
            "buyer_appts_set", "buyer_appts_held", "buyer_reps_signed", "written_offers", "showings",
            "open_houses", "hours"]
DEAL_AGENT_MATCH = """(%(cte_agent)s::text IS NULL OR LOWER(TRIM(%(cte_agent)s)) IN (
    LOWER(TRIM(COALESCE(d.primary_agent, ''))), LOWER(TRIM(COALESCE(d.secondary_agent, ''))),
    LOWER(TRIM(COALESCE(d.agent3, ''))), LOWER(TRIM(COALESCE(d.agent4, '')))))"""
ACT_AGENT_MATCH = "(%(cte_agent)s::text IS NULL OR LOWER(TRIM(a.agent_name)) = LOWER(TRIM(%(cte_agent)s)))"
# Financial Statement lines shown per year: (key, label in CTE, lowercase)
FIN_LINES = [("income", "total income"), ("referral_income", "referral income"),
             ("cost_of_sales", "total cost of sales"), ("gross_profit", "gross profit"),
             ("expenses", "total expenses"), ("net_profit", "net profit"),
             ("taxes", "estimated taxes"), ("net_after_taxes", "net after taxes")]
FELL_THROUGH = ("Cancelled", "Sale Failed")


def ready(cur):
    return tables_ready(cur, "cte_deals")


def agent_options(cur):
    rows = fetch(cur, """
        SELECT name FROM (
            SELECT TRIM(agent_name) AS name FROM cte_activity
            UNION SELECT TRIM(primary_agent) FROM cte_deals WHERE COALESCE(primary_agent, '') <> ''
            UNION SELECT TRIM(secondary_agent) FROM cte_deals WHERE COALESCE(secondary_agent, '') <> ''
        ) x WHERE name <> '' ORDER BY LOWER(name)""", {})
    seen, out = set(), []
    for r in rows:  # same person typed with different capitalization
        if r["name"].lower() not in seen:
            seen.add(r["name"].lower())
            out.append(r["name"])
    return out


def last_import(cur):
    if not table_exists(cur, "cte_import_log"):
        return None
    return one(cur, "SELECT MAX(imported_at) AS at FROM cte_import_log", {})["at"]


def _num(v):
    return float(v or 0)


def period(cur, start, end, cte_agent=None):
    """Activity + deal KPIs for a date range (calendar days, end exclusive)."""
    p = {"start": start.date(), "end": end.date(), "cte_agent": cte_agent}
    sums = ", ".join(f"COALESCE(SUM(a.{c}), 0) AS {c}" for c in ACTIVITY)
    out = one(cur, f"""
        SELECT {sums} FROM cte_activity a
        WHERE a.activity_date >= %(start)s AND a.activity_date < %(end)s
          AND EXTRACT(YEAR FROM a.activity_date) = a.file_year AND {ACT_AGENT_MATCH}""", p)
    out.update(one(cur, f"""
        SELECT COUNT(*) FILTER (WHERE uc) AS accepted,
               COUNT(*) FILTER (WHERE uc AND d.status IN %(fell)s) AS fell_through,
               COUNT(*) FILTER (WHERE cl) AS closed_deals,
               COUNT(*) FILTER (WHERE cl AND d.deal_type = 'Buyer') AS buyer_closed,
               COUNT(*) FILTER (WHERE cl AND d.deal_type = 'Listing') AS listing_closed,
               COALESCE(SUM(d.sale_price) FILTER (WHERE cl), 0) AS volume,
               COALESCE(SUM(d.gci) FILTER (WHERE cl), 0) AS gci,
               COALESCE(SUM(d.primary_gci) FILTER (WHERE cl), 0) AS agent_gci,
               COALESCE(AVG(d.commission_pct) FILTER (WHERE cl AND d.commission_pct > 0), 0) AS avg_pct,
               COUNT(*) FILTER (WHERE d.deal_type = 'Listing' AND d.signed_date >= %(start)s
                                AND d.signed_date < %(end)s
                                AND EXTRACT(YEAR FROM d.signed_date) = d.file_year) AS listing_agreements
        FROM (SELECT d.*,
                     (d.under_contract_date >= %(start)s AND d.under_contract_date < %(end)s
                      AND EXTRACT(YEAR FROM d.under_contract_date) = d.file_year) AS uc,
                     (d.status = 'Closed' AND d.close_date >= %(start)s AND d.close_date < %(end)s
                      AND EXTRACT(YEAR FROM d.close_date) = d.file_year) AS cl
              FROM cte_deals d WHERE {DEAL_AGENT_MATCH}) d""", {**p, "fell": FELL_THROUGH}))
    out["avg_price"] = _num(out["volume"]) / out["closed_deals"] if out["closed_deals"] else 0
    if not cte_agent:  # team income from the Financial Statement, whole months in the range
        out.update(one(cur, """
            SELECT COALESCE(SUM(amount) FILTER (WHERE LOWER(TRIM(label)) = 'total income'), 0) AS income,
                   COALESCE(SUM(amount) FILTER (WHERE LOWER(TRIM(label)) = 'net profit'), 0) AS net_profit
            FROM cte_financials
            WHERE make_date(file_year, month, 1) >= date_trunc('month', %(start)s::date)
              AND make_date(file_year, month, 1) < %(end)s""", p))
    return out


def by_year(cur, cte_agent=None):
    p = {"cte_agent": cte_agent, "fell": FELL_THROUGH}
    years = {}
    sums = ", ".join(f"COALESCE(SUM(a.{c}), 0) AS {c}" for c in ACTIVITY)
    for r in fetch(cur, f"""
            SELECT a.file_year AS year, {sums} FROM cte_activity a
            WHERE EXTRACT(YEAR FROM a.activity_date) = a.file_year AND {ACT_AGENT_MATCH} GROUP BY 1""", p):
        years.setdefault(r["year"], {}).update(r)
    for r in fetch(cur, f"""
            SELECT d.file_year AS year,
                   COUNT(*) FILTER (WHERE EXTRACT(YEAR FROM d.under_contract_date) = d.file_year) AS accepted,
                   COUNT(*) FILTER (WHERE d.status IN %(fell)s) AS fell_through,
                   COUNT(*) FILTER (WHERE cl) AS closed_deals,
                   COUNT(*) FILTER (WHERE cl AND d.deal_type = 'Buyer') AS buyer_closed,
                   COUNT(*) FILTER (WHERE cl AND d.deal_type = 'Listing') AS listing_closed,
                   COALESCE(SUM(d.sale_price) FILTER (WHERE cl), 0) AS volume,
                   COALESCE(SUM(d.gci) FILTER (WHERE cl), 0) AS gci,
                   COALESCE(AVG(d.commission_pct) FILTER (WHERE cl AND d.commission_pct > 0), 0) AS avg_pct,
                   COUNT(*) FILTER (WHERE d.deal_type = 'Listing'
                                    AND EXTRACT(YEAR FROM d.signed_date) = d.file_year) AS listing_agreements
            FROM (SELECT d.*, (d.status = 'Closed' AND EXTRACT(YEAR FROM d.close_date) = d.file_year) AS cl
                  FROM cte_deals d WHERE {DEAL_AGENT_MATCH}) d GROUP BY 1""", p):
        years.setdefault(r["year"], {}).update(r)
    if not cte_agent:
        wanted = {label: key for key, label in FIN_LINES}
        for r in fetch(cur, """SELECT file_year AS year, LOWER(TRIM(label)) AS label, SUM(amount) AS total
                               FROM cte_financials GROUP BY 1, 2""", {}):
            if r["label"] in wanted:
                years.setdefault(r["year"], {})[wanted[r["label"]]] = r["total"]
    out = []
    for y in sorted(years, reverse=True):
        row = {"year": y, "accepted": 0, "fell_through": 0, "closed_deals": 0, "buyer_closed": 0,
               "listing_agreements": 0,
               "listing_closed": 0, "volume": 0, "gci": 0, "avg_pct": 0, **{c: 0 for c in ACTIVITY},
               **{k: None for k, _ in FIN_LINES}}
        row.update({k: v for k, v in years[y].items() if v is not None})
        row["avg_price"] = _num(row["volume"]) / row["closed_deals"] if row["closed_deals"] else 0
        out.append(row)
    return out


def by_agent(cur, start, end):
    p = {"start": start.date(), "end": end.date()}
    agents = {}
    for r in fetch(cur, f"""
            SELECT TRIM(a.agent_name) AS name, {", ".join(f"SUM(a.{c}) AS {c}" for c in ACTIVITY)}
            FROM cte_activity a WHERE a.activity_date >= %(start)s AND a.activity_date < %(end)s
              AND EXTRACT(YEAR FROM a.activity_date) = a.file_year GROUP BY 1""", p):
        agents.setdefault(r["name"].lower(), {"name": r["name"]}).update(r)
    for r in fetch(cur, """
            SELECT TRIM(d.primary_agent) AS name,
                   COUNT(*) FILTER (WHERE d.under_contract_date >= %(start)s AND d.under_contract_date < %(end)s
                                    AND EXTRACT(YEAR FROM d.under_contract_date) = d.file_year) AS accepted,
                   COUNT(*) FILTER (WHERE cl) AS closed_deals,
                   COALESCE(SUM(d.sale_price) FILTER (WHERE cl), 0) AS volume,
                   COALESCE(SUM(d.gci) FILTER (WHERE cl), 0) AS gci,
                   COALESCE(SUM(d.primary_gci) FILTER (WHERE cl), 0) AS agent_gci,
                   COALESCE(AVG(d.commission_pct) FILTER (WHERE cl AND d.commission_pct > 0), 0) AS avg_pct,
                   COUNT(*) FILTER (WHERE d.deal_type = 'Listing' AND d.signed_date >= %(start)s
                                    AND d.signed_date < %(end)s
                                    AND EXTRACT(YEAR FROM d.signed_date) = d.file_year) AS listing_agreements
            FROM (SELECT d.*, (d.status = 'Closed' AND d.close_date >= %(start)s AND d.close_date < %(end)s
                               AND EXTRACT(YEAR FROM d.close_date) = d.file_year) AS cl FROM cte_deals d) d
            WHERE COALESCE(TRIM(d.primary_agent), '') <> '' GROUP BY 1""", p):
        entry = agents.setdefault(r["name"].lower(), {"name": r["name"]})
        entry.update({k: v for k, v in r.items() if k != "name"})
    rows = []
    for a in agents.values():
        for k in ACTIVITY + ["accepted", "closed_deals", "volume", "gci", "agent_gci", "listing_agreements", "avg_pct"]:
            a[k] = a.get(k) or 0
        if any(a[k] for k in ACTIVITY + ["accepted", "closed_deals"]):
            rows.append(a)
    rows.sort(key=lambda a: (-_num(a["gci"]), a["name"].lower()))
    return rows


def monthly_gci(cur, year, cte_agent=None):
    """12 monthly values: team Total Income (Financial Statement), or an agent's closed GCI."""
    if cte_agent:
        rows = fetch(cur, f"""
            SELECT EXTRACT(MONTH FROM d.close_date)::int AS m, SUM(d.gci) AS v FROM cte_deals d
            WHERE d.status = 'Closed' AND EXTRACT(YEAR FROM d.close_date) = %(y)s AND d.file_year = %(y)s
              AND {DEAL_AGENT_MATCH} GROUP BY 1""", {"y": year, "cte_agent": cte_agent})
    else:
        rows = fetch(cur, """SELECT month AS m, SUM(amount) AS v FROM cte_financials
                             WHERE file_year = %(y)s AND LOWER(TRIM(label)) = 'total income' GROUP BY 1""",
                     {"y": year})
    vals = [0.0] * 12
    for r in rows:
        vals[r["m"] - 1] = _num(r["v"])
    return vals


def agent_deals(cur, cte_agent, limit=30):
    return fetch(cur, f"""
        SELECT d.file_year, d.deal_type, d.status, d.address, d.clients, d.source, d.under_contract_date,
               d.close_date, d.sale_price, d.commission_pct, d.gci, d.primary_agent, d.primary_gci
        FROM cte_deals d WHERE {DEAL_AGENT_MATCH}
        ORDER BY COALESCE(d.close_date, d.under_contract_date, d.signed_date) DESC NULLS LAST
        LIMIT {int(limit)}""", {"cte_agent": cte_agent})


def name_for(cur, fub_name):
    """The CTE spelling of a FUB agent's name, if they appear in CTE at all."""
    if not fub_name or not ready(cur):
        return None
    wanted = fub_name.strip().lower()
    return next((n for n in agent_options(cur) if n.lower() == wanted), None)


# ---------------------------------------------------------------- Business Overview

SOURCE_MATCH = "(%(source)s::text IS NULL OR LOWER(TRIM(COALESCE(d.source, ''))) = LOWER(TRIM(%(source)s)))"
# (key, label, kind) rows on the Business Overview tables
BUSINESS_METRICS = [("accepted", "Accepted (went under contract)", "n"), ("closed", "Closed Deals", "n"),
                    ("volume", "Closed Volume", "money"), ("avg", "Avg. Sales Price", "money"),
                    ("gci", "GCI", "money"), ("pending", "Pending (projected to close)", "n"),
                    ("pending_volume", "Pending Volume", "money")]


def source_options(cur):
    rows = fetch(cur, """SELECT TRIM(source) AS s, COUNT(*) FROM cte_deals WHERE COALESCE(TRIM(source), '') <> ''
                         GROUP BY 1 ORDER BY 2 DESC""", {})
    return [r["s"] for r in rows]


def years(cur):
    return [r["y"] for r in fetch(cur, "SELECT DISTINCT file_year AS y FROM cte_deals ORDER BY 1 DESC", {})]


def business_months(cur, year, cte_agent=None, source=None):
    """Per-month deal KPIs for a calendar year, from that year's CTE file."""
    p = {"y": year, "cte_agent": cte_agent, "source": source}
    where = f"d.file_year = %(y)s AND {DEAL_AGENT_MATCH} AND {SOURCE_MATCH}"
    months = {m: {"month": m, **{k: 0 for k, _, _ in BUSINESS_METRICS}} for m in range(1, 13)}
    for r in fetch(cur, f"""
            SELECT EXTRACT(MONTH FROM d.under_contract_date)::int AS m, COUNT(*) AS n FROM cte_deals d
            WHERE EXTRACT(YEAR FROM d.under_contract_date) = %(y)s AND {where} GROUP BY 1""", p):
        months[r["m"]]["accepted"] = r["n"]
    for r in fetch(cur, f"""
            SELECT EXTRACT(MONTH FROM d.close_date)::int AS m, COUNT(*) AS n,
                   COALESCE(SUM(d.sale_price), 0) AS vol, COALESCE(SUM(d.gci), 0) AS gci FROM cte_deals d
            WHERE d.status = 'Closed' AND EXTRACT(YEAR FROM d.close_date) = %(y)s AND {where} GROUP BY 1""", p):
        months[r["m"]].update(closed=r["n"], volume=float(r["vol"]), gci=float(r["gci"]))
    for r in fetch(cur, f"""
            SELECT EXTRACT(MONTH FROM d.proj_close_date)::int AS m, COUNT(*) AS n,
                   COALESCE(SUM(d.sale_price), 0) AS vol FROM cte_deals d
            WHERE d.status = 'Pending' AND EXTRACT(YEAR FROM d.proj_close_date) = %(y)s AND {where} GROUP BY 1""", p):
        months[r["m"]].update(pending=r["n"], pending_volume=float(r["vol"]))
    out = list(months.values())
    for m in out:
        m["avg"] = m["volume"] / m["closed"] if m["closed"] else 0
    return out


def business_quarters(months, metrics=BUSINESS_METRICS):
    """Sum months into quarters and a year total ("avg" is recomputed, not summed)."""
    keys = [k for k, _, _ in metrics if k != "avg"]
    quarters = []
    for q in range(4):
        ms = months[q * 3:(q + 1) * 3]
        row = {"q": q + 1, "months": ms, **{k: sum(m[k] for m in ms) for k in keys}}
        row["avg"] = row["volume"] / row["closed"] if row["closed"] else 0
        quarters.append(row)
    total = {k: sum(q[k] for q in quarters) for k in keys}
    total["avg"] = total["volume"] / total["closed"] if total["closed"] else 0
    return quarters, total


def business_top(cur, since, source=None):
    """Top 5 by closed volume, deal count and average price since a date (Primary Agent)."""
    rows = fetch(cur, f"""
        SELECT TRIM(d.primary_agent) AS name, COUNT(*) AS n, COALESCE(SUM(d.sale_price), 0) AS vol
        FROM cte_deals d
        WHERE d.status = 'Closed' AND d.close_date >= %(since)s AND EXTRACT(YEAR FROM d.close_date) = d.file_year
          AND COALESCE(TRIM(d.primary_agent), '') <> '' AND {SOURCE_MATCH}
        GROUP BY 1""", {"since": since.date(), "source": source})
    for r in rows:
        r["vol"] = float(r["vol"])
        r["avg"] = r["vol"] / r["n"] if r["n"] else 0
    top = lambda key: sorted(rows, key=lambda r: r[key], reverse=True)[:5]  # noqa: E731
    return {"volume": top("vol"), "deals": top("n"), "avg": top("avg")}


# ---------------------------------------------------------------- deals next to FUB deals
# Same shape as the FUB deal numbers in reports.funnel_counts, so pages can
# show both side by side.

def _deal_flags():
    return """(d.under_contract_date >= %(start)s AND d.under_contract_date < %(end)s
               AND EXTRACT(YEAR FROM d.under_contract_date) = d.file_year) AS uc,
              (d.status = 'Closed' AND d.close_date >= %(start)s AND d.close_date < %(end)s
               AND EXTRACT(YEAR FROM d.close_date) = d.file_year) AS cl"""


def deal_counts(cur, start, end, cte_agent=None, source=None):
    """written = went under contract in the period; pending = still pending and
    went under contract in the period; closed = closed in the period."""
    p = {"start": start.date(), "end": end.date(), "cte_agent": cte_agent, "source": source, "fell": FELL_THROUGH}
    return one(cur, f"""
        SELECT COUNT(*) FILTER (WHERE uc) AS written,
               COALESCE(SUM(sale_price) FILTER (WHERE uc), 0) AS written_vol,
               COUNT(*) FILTER (WHERE uc AND status IN %(fell)s) AS cancelled,
               COUNT(*) FILTER (WHERE uc AND status = 'Pending') AS pending,
               COALESCE(SUM(sale_price) FILTER (WHERE uc AND status = 'Pending'), 0) AS pending_vol,
               COUNT(*) FILTER (WHERE cl) AS closed,
               COALESCE(SUM(sale_price) FILTER (WHERE cl), 0) AS closed_vol,
               COALESCE(SUM(gci) FILTER (WHERE cl), 0) AS gci
        FROM (SELECT d.*, {_deal_flags()} FROM cte_deals d
              WHERE {DEAL_AGENT_MATCH} AND {SOURCE_MATCH}) d""", p)


def deals_by_agent(cur, start, end, source=None):
    """{lowercase primary agent name: {written, pending, closed}} for a period."""
    p = {"start": start.date(), "end": end.date(), "source": source, "cte_agent": None}
    out = {}
    for r in fetch(cur, f"""
            SELECT LOWER(TRIM(d.primary_agent)) AS name,
                   COUNT(*) FILTER (WHERE uc) AS written,
                   COUNT(*) FILTER (WHERE uc AND status = 'Pending') AS pending,
                   COUNT(*) FILTER (WHERE cl) AS closed
            FROM (SELECT d.*, {_deal_flags()} FROM cte_deals d WHERE {SOURCE_MATCH}) d
            WHERE COALESCE(TRIM(d.primary_agent), '') <> '' GROUP BY 1""", p):
        out[r["name"]] = r
    return out


def top_deals(cur, since, until, kind, source=None, limit=5):
    """Top agents (Primary Agent) by closed deals or deals written since a date."""
    p = {"start": since.date(), "end": until.date(), "source": source, "cte_agent": None}
    flag = "cl" if kind == "closed" else "uc"
    return [{"name": r["name"], "value": r["n"], "volume": float(r["vol"])} for r in fetch(cur, f"""
        SELECT TRIM(d.primary_agent) AS name, COUNT(*) AS n, COALESCE(SUM(d.sale_price), 0) AS vol
        FROM (SELECT d.*, {_deal_flags()} FROM cte_deals d WHERE {SOURCE_MATCH}) d
        WHERE {flag} AND COALESCE(TRIM(d.primary_agent), '') <> ''
        GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT {int(limit)}""", p)]


# ---------------------------------------------------------------- Agent Snapshot > Financial Insights

def agent_financials(cur, cte_agent, now):
    """Last 12 months of an agent's CTE deals: volume, GCI, commission %, deal
    sizes, by lead source, plus closed volume/GCI by month this year vs last."""
    since = (now - timedelta(days=365)).date()
    p = {"since": since, "cte_agent": cte_agent}
    closed = fetch(cur, f"""
        SELECT d.sale_price, d.gci, d.commission_pct, COALESCE(NULLIF(TRIM(d.source), ''), '<unspecified>') AS source
        FROM cte_deals d
        WHERE d.status = 'Closed' AND d.close_date >= %(since)s AND EXTRACT(YEAR FROM d.close_date) = d.file_year
          AND {DEAL_AGENT_MATCH}""", p)
    other = one(cur, f"""
        SELECT COALESCE(SUM(d.sale_price) FILTER (WHERE d.status = 'Pending'), 0) AS pending,
               COALESCE(SUM(d.gci) FILTER (WHERE d.status = 'Pending'), 0) AS pending_gci,
               COALESCE(SUM(d.sale_price) FILTER (WHERE d.under_contract_date >= %(since)s
                   AND EXTRACT(YEAR FROM d.under_contract_date) = d.file_year), 0) AS accepted
        FROM cte_deals d WHERE {DEAL_AGENT_MATCH}
          AND d.file_year = (SELECT MAX(file_year) FROM cte_deals)""", p)
    prices = [_num(d["sale_price"]) for d in closed if d["sale_price"]]
    gcis = [_num(d["gci"]) for d in closed]
    pcts = [_num(d["commission_pct"]) for d in closed if d["commission_pct"]]
    volume, gci = sum(prices), sum(gcis)
    by_source = {}
    for d in closed:
        s = by_source.setdefault(d["source"], {"source": d["source"], "volume": 0.0, "gci": 0.0, "deals": 0})
        s["volume"] += _num(d["sale_price"])
        s["gci"] += _num(d["gci"])
        s["deals"] += 1
    sources = sorted(by_source.values(), key=lambda s: s["gci"], reverse=True)
    for s in sources:
        s["pct"] = round(s["gci"] / gci * 100, 1) if gci else 0

    def monthly(year, col):
        vals = [0.0] * 12
        for r in fetch(cur, f"""
                SELECT EXTRACT(MONTH FROM d.close_date)::int AS m, SUM(d.{col}) AS v FROM cte_deals d
                WHERE d.status = 'Closed' AND EXTRACT(YEAR FROM d.close_date) = %(y)s AND d.file_year = %(y)s
                  AND {DEAL_AGENT_MATCH} GROUP BY 1""", {"y": year, "cte_agent": cte_agent}):
            vals[r["m"] - 1] = _num(r["v"])
        return vals

    return {"source": "CTE", "cte_name": cte_agent, "year": now.year,
            "deals": len(closed), "volume": volume, "gci": gci,
            "avg_pct": sum(pcts) / len(pcts) if pcts else 0,
            "gci_per_deal": gci / len(closed) if closed else 0,
            "min": min(prices) if prices else 0, "max": max(prices) if prices else 0,
            "avg": volume / len(prices) if prices else 0,
            "pending": _num(other["pending"]), "pending_gci": _num(other["pending_gci"]),
            "accepted": _num(other["accepted"]), "closed": volume, "sources": sources,
            "this_year": monthly(now.year, "gci"), "last_year": monthly(now.year - 1, "gci")}


# Status tiles on Business Overview: (status in CTE, color)
# in the order a deal moves through them
STATUS_TILES = [("Coming Soon", "#F2B84B"), ("Signed", "#7B8FF0"), ("Active", "#8FD3C8"),
                ("Pending", "#C99BE0"), ("Closed", "#2FA867"), ("Cancelled", "#E0685A")]
# (value label, GCI label, date column label) per tile
STATUS_LABELS = {"Closed": ("Volume", "GCI", "Closed"), "Pending": ("Sale volume", "Projected GCI", "Under contract"),
                 "Cancelled": ("Lost volume", "Lost GCI", "Under contract")}
UNSOLD_LABELS = ("List volume", "Projected GCI", "Listed / Signed")
# Not-yet-sold listings are valued at list price (as CTE does); sold/pending at sale price
DEAL_VALUE = """(CASE WHEN d.status IN ('Active', 'Coming Soon', 'Signed', 'Pre-Signed', 'Pipeline')
                      THEN COALESCE(NULLIF(d.list_price, 0), d.sale_price)
                      ELSE COALESCE(NULLIF(d.sale_price, 0), d.list_price) END)"""


def status_summary(cur, year, cte_agent=None, source=None):
    """{status: {count, volume, gci, buyer, listing}} for one year's CTE file."""
    p = {"y": year, "cte_agent": cte_agent, "source": source}
    out = {s: {"status": s, "color": c, "count": 0, "volume": 0.0, "gci": 0.0, "buyer": 0, "listing": 0,
               "labels": STATUS_LABELS.get(s, UNSOLD_LABELS)}
           for s, c in STATUS_TILES}
    for r in fetch(cur, f"""
            SELECT d.status, COUNT(*) AS n, COALESCE(SUM({DEAL_VALUE}), 0) AS vol, COALESCE(SUM(d.gci), 0) AS gci,
                   COUNT(*) FILTER (WHERE d.deal_type = 'Buyer') AS buyer,
                   COUNT(*) FILTER (WHERE d.deal_type = 'Listing') AS listing
            FROM cte_deals d WHERE d.file_year = %(y)s AND {DEAL_AGENT_MATCH} AND {SOURCE_MATCH}
            GROUP BY 1""", p):
        if r["status"] in out:
            out[r["status"]].update(count=r["n"], volume=_num(r["vol"]), gci=_num(r["gci"]),
                                    buyer=r["buyer"], listing=r["listing"])
    return list(out.values())


def deals_with_status(cur, year, status, cte_agent=None, source=None):
    return fetch(cur, f"""
        SELECT d.deal_type, d.address, d.clients, d.source, d.primary_agent, d.signed_date, d.list_date,
               d.under_contract_date, d.close_date, d.exp_date, {DEAL_VALUE} AS value, d.commission_pct, d.gci
        FROM cte_deals d
        WHERE d.file_year = %(y)s AND d.status = %(status)s AND {DEAL_AGENT_MATCH} AND {SOURCE_MATCH}
        ORDER BY COALESCE(d.close_date, d.under_contract_date, d.list_date, d.signed_date) DESC NULLS LAST""",
        {"y": year, "status": status, "cte_agent": cte_agent, "source": source})


def closed_count_since(cur, since, cte_agent=None):
    return one(cur, f"""SELECT COUNT(*) AS n FROM cte_deals d WHERE d.status = 'Closed' AND d.close_date >= %(since)s
                        AND EXTRACT(YEAR FROM d.close_date) = d.file_year AND {DEAL_AGENT_MATCH}""",
               {"since": since.date(), "cte_agent": cte_agent})["n"]
