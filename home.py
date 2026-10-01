"""Dashboard home: one screen that pulls the headline numbers out of every source the dashboard reads
(Follow Up Boss, the CTE workbooks, QuickBooks, the Compass statements in Gmail, the agent contracts)
and lists what needs someone's attention, each linking to the report with the detail.

Every section is read on its own, so one source being down or not connected yet doesn't break the page.
"""
import logging
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta

import cte_reports as CTE
import gmail_import
import qbo
import reports as R
import splits as SP

log = logging.getLogger(__name__)


def _safe(cur, name, fn, default=None):
    """Run one section inside a savepoint; on error log it and return the default."""
    cur.execute("SAVEPOINT home_section")
    try:
        out = fn()
        cur.execute("RELEASE SAVEPOINT home_section")
        return out
    except Exception:
        log.exception("dashboard home: %s failed", name)
        cur.execute("ROLLBACK TO SAVEPOINT home_section")
        return default


def _chg(now, before):
    if not before:
        return None
    return (float(now) - float(before)) / float(before) * 100


def _compass_ytd(cur, year):
    """Highest YTD Income on a Compass statement sent in the year (see compass_invoices)."""
    gmail_import.ensure_tables(cur)
    cur.execute("""SELECT MAX(ytd_income) FROM compass_payments
                   WHERE EXTRACT(YEAR FROM received_at AT TIME ZONE 'America/Los_Angeles') = %s""", (year,))
    v = cur.fetchone()[0]
    return float(v) if v is not None else None


def _deals(cur, today, tz):
    """CTE deal log: this month so far and the year so far, each against the same days a year earlier."""
    def days(start, end):
        return (CTE.deal_counts(cur, start, end), CTE.deal_counts(cur, start.replace(year=start.year - 1),
                                                                   end.replace(year=end.year - 1)))
    end = today + timedelta(days=1)
    m, m_prev = days(today.replace(day=1), end)
    y, y_prev = days(today.replace(month=1, day=1), end)
    pending = R.one(cur, """SELECT COUNT(*) AS n, COALESCE(SUM(sale_price), 0) AS vol, COALESCE(SUM(gci), 0) AS gci
                            FROM cte_deals WHERE status = 'Pending' AND file_year = %(y)s""", {"y": today.year})
    out = {}
    for key, cur_, prev in (("month", m, m_prev), ("ytd", y, y_prev)):
        out[key] = {k: cur_[k] for k in ("written", "closed", "closed_vol", "gci", "cancelled")}
        out[key]["chg"] = {k: _chg(cur_[k], prev[k]) for k in ("written", "closed", "closed_vol", "gci")}
        out[key]["prev"] = prev
    out["pending"] = pending
    return out


def _leads(cur, today, tz):
    """Follow Up Boss: last 30 days against the 30 days before."""
    f = R.Filters(today - timedelta(days=29), today + timedelta(days=1), None, None, tz)
    now, prev = R.funnel_counts(cur, f), R.funnel_counts(cur, f.previous())
    keys = ("new_leads", "appts_set", "appts_sched", "held", "not_held")
    out = {k: now[k] for k in keys}
    out["chg"] = {k: _chg(now[k], prev[k]) for k in keys}
    out["held_rate"] = R.pct(now["held"], now["held"] + now["not_held"], 0)
    out["set_rate"] = R.pct(now["appts_set"], now["new_leads"], 1)
    health = R.pipeline_health(cur, f)
    out["stale_new_pct"] = health["stale_new_pct"]
    return out


def _agents(cur, today, checks):
    """CTE closings this year per agent, with the company's share from the Compass receipts."""
    rows = CTE.by_agent(cur, today.replace(month=1, day=1), today + timedelta(days=1))
    company = {}
    for r in checks or []:
        if r["company"] is not None and r["agent"]:
            c = company.setdefault(r["agent"].lower(), {"company": 0.0, "off": 0})
            c["company"] += r["company"]
            c["off"] += r["status"] in ("under", "over")
    out = []
    for a in rows:
        if not a["closed_deals"] and not a["accepted"]:
            continue
        c = company.get(a["name"].lower(), {})
        out.append({"name": a["name"], "closed": a["closed_deals"], "accepted": a["accepted"],
                    "volume": float(a["volume"]), "gci": float(a["gci"]),
                    "company": c.get("company"), "off": c.get("off", 0),
                    "appts": (a.get("buyer_appts_held") or 0) + (a.get("listing_appts_held") or 0),
                    "owner": a["name"].lower() in SP.OWNERS})
    return out


def _receipts(cur, year):
    deals, unmatched = gmail_import.deal_receipts(cur, date(year, 1, 1), date(year + 1, 1, 1))
    missing = [d for d in deals if not d["receipts"]]
    typos = []
    for d in missing:
        for s in d["suggestions"][:1]:
            fix = (s.get("property") or s.get("description") or "").split(",")[0].strip()
            what = "close date" if s.get("why") == "close date differs" else "address"
            typos.append(f"{d['address']} ({d['close_date'].strftime('%m/%d')}) → {what}: Compass has {fix}"
                         + (f" on {(s.get('bill_date') or s.get('paid_on')).strftime('%m/%d')}" if what == "close date" else ""))
    return {"total": len(deals), "missing": len(missing), "typos": len(typos), "typo_list": typos,
            "unmatched": len(unmatched), "any_receipts": any(d["receipts"] for d in deals) or bool(unmatched)}


def _questions(cur, year):
    """Open questions found in the data for the owners to decide. Each disappears once it's fixed."""
    items = []
    owners = SP.OWNERS
    # both sides of one sale in CTE: counted as 2 closings and the price twice in volume
    rows = R.fetch(cur, """
        SELECT MIN(address) AS address, close_date, COUNT(*) AS sides, MAX(sale_price) AS price,
               STRING_AGG(deal_type || ' ' || COALESCE(primary_agent, '?'), ' / ' ORDER BY deal_type DESC) AS who
        FROM cte_deals WHERE status = 'Closed' AND file_year = %(y)s AND close_date IS NOT NULL
        GROUP BY LOWER(SPLIT_PART(TRIM(address), ' ', 1)), LOWER(SPLIT_PART(TRIM(address), ' ', 2)), close_date
        HAVING COUNT(*) > 1 ORDER BY close_date""", {"y": year})
    if rows:
        extra = sum(float(r["price"] or 0) * (r["sides"] - 1) for r in rows)
        items.append({"level": "ask", "title": f"{len(rows)} sales where we had both sides: count as 1 closing or 2?",
                      "detail": f"Each side is a closing now, so ${extra:,.0f} of volume is counted twice. "
                                + "; ".join(f"{r['address']} ({r['who']})" for r in rows),
                      "link": ("cte", {})})
    # under the 2% minimum commission the agent contracts allow without written approval
    low = [r for r in R.fetch(cur, """
        SELECT address, primary_agent, gci, sale_price, gci / NULLIF(sale_price, 0) * 100 AS pct
        FROM cte_deals WHERE status = 'Closed' AND file_year = %(y)s AND sale_price > 0
          AND gci / sale_price < 0.0199 ORDER BY primary_agent, close_date""", {"y": year})
        if (r["primary_agent"] or "").strip().lower() not in owners]
    if low:
        items.append({"level": "ask", "title": f"{len(low)} agent deal{'s' if len(low) != 1 else ''} under the 2% minimum commission: approved?",
                      "detail": "Contracts need written approval below 2% or $1,500. "
                                + "; ".join(f"{r['address']} ({r['primary_agent']}, {float(r['pct']):.2f}%)" for r in low),
                      "link": ("cte", {})})
    # the same agent typed two ways in CTE
    names = CTE.agent_options(cur)
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if a.lower() in owners or b.lower() in owners:
                continue
            fa, fb = a.lower().split(), b.lower().split()
            first = SequenceMatcher(None, fa[0], fb[0]).ratio() >= 0.8
            last = len(fa) > 1 and len(fb) > 1 and SequenceMatcher(None, fa[-1], fb[-1]).ratio() >= 0.8
            if first and (last or len(fa) == 1 or len(fb) == 1 or fa[0] == fb[0]):
                pairs.append(f"{a} / {b}")
    if pairs:
        items.append({"level": "ask", "title": f"{len(pairs)} agent name{'s' if len(pairs) != 1 else ''} in CTE that may be the same person",
                      "detail": "Same person, or different? " + "; ".join(pairs), "link": ("cte", {})})
    # agreements that contradict themselves or aren't signed
    unclear = [c for c in SP.CONTRACTS if "but" in c["note"] or c["since"] is None]
    if unclear:
        items.append({"level": "ask", "title": f"{len(unclear)} agent agreement{'s' if len(unclear) != 1 else ''} to clear up",
                      "detail": "; ".join(f"{c['agent']}: {c['note'].split(';')[0]}" for c in unclear),
                      "link": ("splits_page", {})})
    # QuickBooks vs Compass for earlier full years (this year is its own item)
    qm = {}
    q = qbo.status(cur)
    if q and q["env"] == "production" and not q["needs_reconnect"]:
        cur.execute("SELECT month, income FROM qbo_pnl")
        qm = {m: float(v or 0) for m, v in cur.fetchall()}
    if qm:
        for y in range(min(qm).year, year):
            if min(qm) > date(y, 1, 1):
                continue
            compass = _compass_ytd(cur, y)
            books = sum(v for m, v in qm.items() if m.year == y)
            if compass is not None and abs(books - compass) > max(1000, 0.01 * compass):
                items.append({"level": "ask", "title": f"{y}: QuickBooks is ${books - compass:+,.0f} off from Compass",
                              "detail": f"QuickBooks income ${books:,.0f} vs Compass YTD ${compass:,.0f}. "
                                        "Income from outside Compass (referrals?) or a missing statement?",
                              "link": ("compass_invoices", {"year": y})})
    return items


def _freshness(cur):
    """When each source last brought in new data."""
    out = []
    if R.table_exists(cur, "pull_state"):
        r = R.fetch(cur, "SELECT last_pulled_at AS at FROM pull_state WHERE id = 1", {})
        out.append({"name": "Follow Up Boss", "at": r[0]["at"] if r else None, "error": None, "link": "sales_manager"})
    if CTE.ready(cur):
        out.append({"name": "CTE workbooks (OneDrive)", "at": CTE.last_import(cur), "error": None, "link": "cte"})
    q = qbo.status(cur)
    out.append({"name": "QuickBooks", "at": q["last_pull_at"] if q else None,
                "error": (q["last_error"] if q and q["needs_reconnect"] else None) if q else "Not connected",
                "link": "quickbooks"})
    for a in gmail_import.accounts(cur):
        out.append({"name": f"Gmail · {a['email']}", "at": a["last_pull_at"],
                    "error": a["last_error"] if a["needs_reconnect"] else None, "link": "compass_invoices"})
    return out


def _attention(money, deals, leads, receipts, checks, fresh, now, questions=None):
    """Things that need a decision or a fix, most important first. Each: level, title, detail, link."""
    items = []
    if checks:
        off = [r for r in checks if r["status"] in ("under", "over")]
        if off:
            gap = sum(r["gap"] for r in off)
            items.append({"level": "bad" if gap < -1000 else "warn",
                          "title": f"{len(off)} deal{'s' if len(off) != 1 else ''} paid off the agent's contract split",
                          "detail": f"Company got ${gap:+,.0f} against the contracts this year.",
                          "link": ("splits_page", {})})
        no_contract = sorted({r["agent"] for r in checks if r["status"] == "no_contract" and r["agent"]})
        if no_contract:
            items.append({"level": "warn", "title": "No agreement in Drive for " + ", ".join(no_contract),
                          "detail": "Their deals can't be checked against a split.", "link": ("splits_page", {})})
    if receipts and receipts["typo_list"]:
        items.append({"level": "ask", "title": f"{receipts['typos']} CTE deal{'s' if receipts['typos'] != 1 else ''} with a likely typo: fix in the CTE file",
                      "detail": "; ".join(receipts["typo_list"]), "link": ("compass_invoices", {"deals": "missing"})})
    if receipts and receipts["missing"] and receipts["any_receipts"]:
        items.append({"level": "bad" if receipts["missing"] - receipts["typos"] > 2 else "warn",
                      "title": f"{receipts['missing']} closed deal{'s' if receipts['missing'] != 1 else ''} with no Compass receipt",
                      "detail": (f"{receipts['typos']} look like a typo in the CTE address or date. " if receipts["typos"] else "")
                      + f"{receipts['total'] - receipts['missing']} of {receipts['total']} closings this year are matched.",
                      "link": ("compass_invoices", {"deals": "missing"})})
    if money and money.get("compass") is not None and money.get("books") is not None:
        diff = money["books"] - money["compass"]
        if abs(diff) > max(1000, 0.01 * money["compass"]):
            items.append({"level": "warn", "title": f"QuickBooks and Compass differ by ${diff:+,.0f} this year",
                          "detail": f"QuickBooks income ${money['books']:,.0f} vs Compass YTD ${money['compass']:,.0f}.",
                          "link": ("compass_invoices", {})})
    if money and money.get("gross_vs") and money["gross_vs"]["pace_diff"] < 0:
        g = money["gross_vs"]
        items.append({"level": "warn", "title": f"GCI is ${-g['pace_diff']:,.0f} behind the pace for the year's goal",
                      "detail": f"${money['gross']:,.0f} of ${g['goal']:,.0f} ({g['pct']:.0f}%), "
                                f"{money['pace_pct']:.0f}% of the year gone.", "link": ("business_overview", {})})
    if leads:
        if leads["stale_new_pct"] > 10:
            items.append({"level": "warn", "title": f"{leads['stale_new_pct']}% of last 30 days' new leads still in \"New\" after 48h",
                          "detail": "Get them contacted and staged.", "link": ("dashboard", {"tab": "pipeline", "_anchor": "lead-health"})})
        if leads["held_rate"] is not None and leads["held"] + leads["not_held"] and leads["held_rate"] < 50:
            items.append({"level": "warn", "title": f"Only {leads['held_rate']}% of appointments held in the last 30 days",
                          "detail": f"{leads['held']} held, {leads['not_held']} not held.", "link": ("appointments", {"period": "last30"})})
    if deals and deals["ytd"]["cancelled"]:
        items.append({"level": "info", "title": f"{deals['ytd']['cancelled']} deal{'s' if deals['ytd']['cancelled'] != 1 else ''} fell through this year",
                      "detail": "Went under contract and then cancelled.", "link": ("business_overview", {})})
    for s in fresh or []:
        if s["error"]:
            items.append({"level": "bad", "title": f"{s['name']} needs attention", "detail": s["error"], "link": (s["link"], {})})
        elif s["at"] and now - s["at"] > timedelta(hours=36):
            items.append({"level": "warn", "title": f"{s['name']} hasn't updated since {s['at'].strftime('%m/%d %I:%M %p')}",
                          "detail": "Check the daily cron job on Render.", "link": (s["link"], {})})
    items += questions or []
    order = {"bad": 0, "warn": 1, "ask": 2, "info": 3}
    return sorted(items, key=lambda i: order[i["level"]])


def overview(cur, today, tz, year_totals):
    """Everything on the dashboard home. `today` is midnight today in the team's timezone,
    `year_totals` is app._year_totals for this year."""
    year = today.year
    now = datetime.now(today.tzinfo)
    money = _safe(cur, "money", lambda: dict(year_totals(cur, year, today)), {}) or {}
    money["compass"] = _safe(cur, "compass ytd", lambda: _compass_ytd(cur, year))
    money["books"] = (money.get("books") or {}).get("income") if isinstance(money.get("books"), dict) else None
    has_cte = _safe(cur, "cte ready", lambda: CTE.ready(cur), False)
    has_fub = _safe(cur, "fub ready", lambda: R.tables_ready(cur, "people"), False)
    deals = _safe(cur, "deals", lambda: _deals(cur, today, tz)) if has_cte else None
    leads = _safe(cur, "leads", lambda: _leads(cur, today, tz)) if has_fub else None
    checks = _safe(cur, "splits", lambda: SP.deal_check(cur, date(year, 1, 1), date(year + 1, 1, 1))) if has_cte else None
    receipts = _safe(cur, "receipts", lambda: _receipts(cur, year)) if has_cte else None
    agents = _safe(cur, "agents", lambda: _agents(cur, today, checks), []) if has_cte else []
    fresh = _safe(cur, "freshness", lambda: _freshness(cur), [])
    questions = _safe(cur, "questions", lambda: _questions(cur, year), []) if has_cte else []
    company = None
    if checks:
        paid = [r["company"] for r in checks if r["company"] is not None]
        company = {"total": sum(paid), "deals": len(paid)}
    return {"year": year, "money": money, "deals": deals, "leads": leads, "agents": agents, "company": company,
            "receipts": receipts, "fresh": fresh,
            "attention": _attention(money, deals, leads, receipts, checks, fresh, now, questions)}
