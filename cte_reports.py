"""Report queries for the CTE workbook data loaded by cte_import.py.

Agents are identified by the name typed in CTE (not a FUB id). Each deal
event is counted only from the file of the year it happened in, so deals
carried over from one year's file into the next aren't double counted.
Team income comes from the Financial Statement (it includes referral and
other income that isn't in the deal log); per-agent GCI comes from deals.
"""
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
                   COALESCE(AVG(d.commission_pct) FILTER (WHERE cl AND d.commission_pct > 0), 0) AS avg_pct
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
                   COALESCE(SUM(d.primary_gci) FILTER (WHERE cl), 0) AS agent_gci
            FROM (SELECT d.*, (d.status = 'Closed' AND d.close_date >= %(start)s AND d.close_date < %(end)s
                               AND EXTRACT(YEAR FROM d.close_date) = d.file_year) AS cl FROM cte_deals d) d
            WHERE COALESCE(TRIM(d.primary_agent), '') <> '' GROUP BY 1""", p):
        entry = agents.setdefault(r["name"].lower(), {"name": r["name"]})
        entry.update({k: v for k, v in r.items() if k != "name"})
    rows = []
    for a in agents.values():
        for k in ACTIVITY + ["accepted", "closed_deals", "volume", "gci", "agent_gci"]:
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
