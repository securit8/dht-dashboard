"""Analytics for the QuickBooks page, from the monthly P&L (qbo_pnl) and every P&L entry (qbo_txns).

Year to date is compared with the same days of the year before. Expenses are everything below income:
cost of sales, expenses and other expenses (refunds and credits come in negative and reduce them).
"""
from datetime import date, timedelta

import goals as G

EXPENSE_SECTIONS = ("Cost of sales", "Expenses", "Other expenses")


def _rows(cur, sql, params):
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _has_txns(cur):
    cur.execute("SELECT to_regclass('qbo_txns') IS NOT NULL")
    if not cur.fetchone()[0]:
        return False
    cur.execute("SELECT EXISTS (SELECT 1 FROM qbo_txns)")
    return cur.fetchone()[0]


def _chg(now, before):
    return (now - before) / abs(before) * 100 if before else None


def monthly(cur, year):
    """12 months of income / expenses / net for `year` from the P&L (None for months not reached yet)."""
    rows = {r["month"].month: r for r in _rows(cur, """
        SELECT month, income + COALESCE(other_income, 0) AS income,
               COALESCE(cogs, 0) + COALESCE(expenses, 0) + COALESCE(other_expenses, 0) AS expenses, net_income AS net
        FROM qbo_pnl WHERE EXTRACT(YEAR FROM month) = %(y)s""", {"y": year})}
    today = date.today()
    out = {"income": [], "expenses": [], "net": []}
    for m in range(1, 13):
        future = year == today.year and m > today.month
        r = rows.get(m)
        for k in out:
            out[k].append(None if future else float(r[k] or 0) if r else 0.0)
    return out


def kpis(cur, today):
    """Year to date vs the same days last year, the last 3 months, and the monthly average spend."""
    start, end = date(today.year, 1, 1), today + timedelta(days=1)
    ly_start, ly_end = date(today.year - 1, 1, 1), end.replace(year=today.year - 1)

    def totals(a, b):
        r = _rows(cur, """SELECT COALESCE(SUM(amount) FILTER (WHERE section IN ('Income', 'Other income')), 0) AS income,
                                 COALESCE(SUM(amount) FILTER (WHERE section = ANY(%(exp)s)), 0) AS expenses
                          FROM qbo_txns WHERE txn_date >= %(a)s AND txn_date < %(b)s""",
                  {"a": a, "b": b, "exp": list(EXPENSE_SECTIONS)})[0]
        inc, exp = float(r["income"]), float(r["expenses"])
        return {"income": inc, "expenses": exp, "net": inc - exp, "margin": (inc - exp) / inc * 100 if inc else None}

    now, before = totals(start, end), totals(ly_start, ly_end)
    months_done = max(today.month - 1, 1)
    full = _rows(cur, """SELECT month, COALESCE(cogs, 0) + COALESCE(expenses, 0) + COALESCE(other_expenses, 0) AS exp, net_income AS net
                         FROM qbo_pnl WHERE month < %(m)s ORDER BY month DESC LIMIT 3""", {"m": today.replace(day=1)})
    return {"ytd": now, "last_year": before,
            "chg": {k: _chg(now[k], before[k]) for k in ("income", "expenses", "net")},
            "avg_spend_3m": sum(float(r["exp"]) for r in full) / len(full) if full else None,
            "net_3m": sum(float(r["net"]) for r in full) if full else None,
            "months_done": months_done, "through": today}


def by_category(cur, today, limit=10):
    """Expenses by top-level category, year to date vs the same days last year, biggest first."""
    end = today + timedelta(days=1)
    rows = _rows(cur, """
        SELECT category,
               COALESCE(SUM(amount) FILTER (WHERE txn_date >= %(a)s AND txn_date < %(b)s), 0) AS ytd,
               COALESCE(SUM(amount) FILTER (WHERE txn_date >= %(la)s AND txn_date < %(lb)s), 0) AS ly,
               COUNT(*) FILTER (WHERE txn_date >= %(a)s AND txn_date < %(b)s) AS n
        FROM qbo_txns WHERE section = ANY(%(exp)s) GROUP BY 1""",
                 {"a": date(today.year, 1, 1), "b": end, "la": date(today.year - 1, 1, 1),
                  "lb": end.replace(year=today.year - 1), "exp": list(EXPENSE_SECTIONS)})
    rows = [dict(r, ytd=float(r["ytd"]), ly=float(r["ly"])) for r in rows if float(r["ytd"]) or float(r["ly"])]
    rows.sort(key=lambda r: -r["ytd"])
    total = sum(r["ytd"] for r in rows) or 1
    for r in rows:
        r["share"] = r["ytd"] / total * 100
        r["chg"] = _chg(r["ytd"], r["ly"])
    if len(rows) > limit:
        rest = rows[limit:]
        rows = rows[:limit] + [{"category": f"Other ({len(rest)} categories)", "ytd": sum(r["ytd"] for r in rest),
                                "ly": sum(r["ly"] for r in rest), "n": sum(r["n"] for r in rest),
                                "share": sum(r["share"] for r in rest), "chg": _chg(sum(r["ytd"] for r in rest), sum(r["ly"] for r in rest))}]
    return rows


def category_months(cur, year, top=5):
    """Monthly expenses of the `top` categories of the year (the rest summed as Other): {labels, series}."""
    rows = _rows(cur, """SELECT category, EXTRACT(MONTH FROM txn_date)::int AS m, SUM(amount) AS v FROM qbo_txns
                         WHERE section = ANY(%(exp)s) AND EXTRACT(YEAR FROM txn_date) = %(y)s GROUP BY 1, 2""",
                 {"y": year, "exp": list(EXPENSE_SECTIONS)})
    totals = {}
    for r in rows:
        totals[r["category"]] = totals.get(r["category"], 0) + float(r["v"])
    keep = [c for c, _ in sorted(totals.items(), key=lambda x: -x[1])[:top]]
    series = {c: [0.0] * 12 for c in keep + ["Other"]}
    for r in rows:
        series[r["category"] if r["category"] in keep else "Other"][r["m"] - 1] += float(r["v"])
    last = date.today().month if year == date.today().year else 12
    series = {k: v[:last] for k, v in series.items() if any(v)}
    return {"labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][:last],
            "series": series}


def top_payees(cur, today, limit=12):
    """Who the money went to this year, biggest first."""
    return [dict(r, total=float(r["total"])) for r in _rows(cur, """
        SELECT COALESCE(NULLIF(TRIM(name), ''), '(no payee)') AS payee, COUNT(*) AS n, SUM(amount) AS total,
               MODE() WITHIN GROUP (ORDER BY category) AS category, MAX(txn_date) AS last
        FROM qbo_txns WHERE section = ANY(%(exp)s) AND txn_date >= %(a)s
        GROUP BY 1 HAVING SUM(amount) > 0 ORDER BY 3 DESC LIMIT %(n)s""",
        {"a": date(today.year, 1, 1), "exp": list(EXPENSE_SECTIONS), "n": limit})]


def biggest(cur, today, limit=10):
    """The largest single expenses this year."""
    return [dict(r, amount=float(r["amount"])) for r in _rows(cur, """
        SELECT txn_date, name, category, account, memo, amount FROM qbo_txns
        WHERE section = ANY(%(exp)s) AND txn_date >= %(a)s ORDER BY amount DESC LIMIT %(n)s""",
        {"a": date(today.year, 1, 1), "exp": list(EXPENSE_SECTIONS), "n": limit})]


def recurring(cur, today):
    """Payees charged in at least 3 of the last 6 months (subscriptions, rent, software): the monthly cost."""
    since = (today.replace(day=1) - timedelta(days=150)).replace(day=1)
    rows = _rows(cur, """
        SELECT COALESCE(NULLIF(TRIM(name), ''), '(no payee)') AS payee, MODE() WITHIN GROUP (ORDER BY category) AS category,
               COUNT(DISTINCT date_trunc('month', txn_date)) AS months, SUM(amount) AS total, MAX(txn_date) AS last
        FROM qbo_txns WHERE section = ANY(%(exp)s) AND txn_date >= %(a)s
        GROUP BY 1 HAVING COUNT(DISTINCT date_trunc('month', txn_date)) >= 3 AND SUM(amount) > 0""",
                 {"a": since, "exp": list(EXPENSE_SECTIONS)})
    for r in rows:
        r["monthly"] = float(r["total"]) / r["months"]
    rows.sort(key=lambda r: -r["monthly"])
    return {"rows": rows, "total": sum(r["monthly"] for r in rows), "since": since}


def income_sources(cur, today):
    """Where this year's income came from: Compass payments, escrow / title wires, checks, other."""
    rows = _rows(cur, """SELECT name, memo, account, amount FROM qbo_txns
                         WHERE section IN ('Income', 'Other income') AND txn_date >= %(a)s""", {"a": date(today.year, 1, 1)})
    out = {}
    for r in rows:
        text = f"{r['name'] or ''} {r['memo'] or ''}".lower()
        if "interest" in (r["account"] or "").lower():
            k = "Bank interest"
        elif "assist contr" in text:
            k = "Compass Assist Contr"
        elif "compass" in text:
            k = "Compass payments"
        elif "escrow" in text or "title" in text or "wire" in text:
            k = "Escrow / title wires"
        elif "mobile" in text or "deposit" in text:
            k = "Checks (mobile deposits)"
        else:
            k = "Other"
        out[k] = out.get(k, 0.0) + float(r["amount"])
    total = sum(out.values()) or 1
    return [{"source": k, "amount": v, "share": v / total * 100} for k, v in sorted(out.items(), key=lambda x: -x[1])]


def expense_caps(cur):
    """The Oct-Mar plan's monthly expense caps against the books, for the plan months reached so far."""
    caps = dict((k, v) for k, _, v, _ in G.MONTHLY)["expenses"]
    out = []
    for (y, m), cap in zip(G.PLAN_MONTHS, caps):
        if date(y, m, 1) > date.today():
            break
        r = _rows(cur, """SELECT COALESCE(cogs, 0) + COALESCE(expenses, 0) + COALESCE(other_expenses, 0) AS exp
                          FROM qbo_pnl WHERE month = %(m)s""", {"m": date(y, m, 1)})
        spent = float(r[0]["exp"]) if r else 0.0
        out.append({"month": date(y, m, 1), "cap": cap, "spent": spent, "over": spent > cap})
    return out


def page(cur, today):
    """Everything the QuickBooks page shows (None when there are no entries yet)."""
    if not _has_txns(cur):
        return None
    this, last = monthly(cur, today.year), monthly(cur, today.year - 1)
    return {"kpis": kpis(cur, today), "this": this, "last": last, "categories": by_category(cur, today),
            "cat_months": category_months(cur, today.year), "payees": top_payees(cur, today),
            "biggest": biggest(cur, today), "recurring": recurring(cur, today),
            "sources": income_sources(cur, today), "caps": expense_caps(cur)}
