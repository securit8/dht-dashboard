"""Dashboard home: one screen that pulls the headline numbers out of every source the dashboard reads
(Follow Up Boss, the CTE workbooks, QuickBooks, the Compass statements in Gmail, the agent contracts)
and lists what needs someone's attention, each linking to the report with the detail.

Every section is read on its own, so one source being down or not connected yet doesn't break the page.
"""
import logging
import threading
import time
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta

import cte_reports as CTE
import decisions
import gmail_import
import goals as G
import qbo
import reports as R
import splits as SP

log = logging.getLogger(__name__)


_timing = threading.local()


def timings():
    """{section: seconds} for the sections run so far in this request (see reset_timings)."""
    return dict(getattr(_timing, "t", {}))


def reset_timings():
    _timing.t = {}


LAST_ERRORS = {}  # section -> its last error, shown in the dashboard page source


def _safe(cur, name, fn, default=None):
    """Run one section inside a savepoint; on error log it and return the default. Its time is recorded."""
    started = time.perf_counter()
    cur.execute("SAVEPOINT home_section")
    try:
        out = fn()
        cur.execute("RELEASE SAVEPOINT home_section")
        return out
    except Exception as e:
        log.exception("dashboard home: %s failed", name)
        cur.execute("ROLLBACK TO SAVEPOINT home_section")
        LAST_ERRORS[name] = f"{type(e).__name__}: {str(e)[:160]}".replace("--", "-")  # in the page source
        return default
    finally:
        t = getattr(_timing, "t", None)
        if t is not None:
            key = name.split(" ")[0] if name.startswith("details") else name
            t[key] = t.get(key, 0.0) + time.perf_counter() - started


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
    if today.day <= 7:
        # a month only a few days old compares almost nothing: show last month in full instead
        m_end = today.replace(day=1)
        m_start = (m_end - timedelta(days=1)).replace(day=1)
        m_label, m_note = f"{m_start.strftime('%B')} vs a year ago",             f"Last month in full (this month is {today.day} day{'s' if today.day != 1 else ''} old) against {m_start.strftime('%B %Y').replace(str(m_start.year), str(m_start.year - 1))}"
    else:
        m_start, m_end = today.replace(day=1), end
        m_label, m_note = "This month vs a year ago", "Against the same days of the month a year ago"
    m, m_prev = days(m_start, m_end)
    y, y_prev = days(today.replace(month=1, day=1), end)
    pending = R.one(cur, """SELECT COUNT(*) AS n, COALESCE(SUM(sale_price), 0) AS vol, COALESCE(SUM(gci), 0) AS gci
                            FROM cte_deals WHERE status = 'Pending' AND file_year = %(y)s""", {"y": today.year})
    out = {}
    for key, cur_, prev in (("month", m, m_prev), ("ytd", y, y_prev)):
        out[key] = {k: cur_[k] for k in ("written", "closed", "closed_vol", "gci", "cancelled")}
        out[key]["chg"] = {k: _chg(cur_[k], prev[k]) for k in ("written", "closed", "closed_vol", "gci")}
        out[key]["prev"] = prev
    out["pending"] = pending
    out["month"].update(label=m_label, note=m_note)
    return out


def _pipeline(cur, today, tz, funnel, deals, money):
    """The year so far from lead to money, each step from where it's recorded: Follow Up Boss (leads, talks,
    appointments), the CTE deal log (accepted, closed) and Compass / QuickBooks (paid)."""
    steps = []
    if funnel and funnel["leads"]:
        y = R.funnel_counts(cur, R.Filters(today.replace(month=1, day=1), today + timedelta(days=1), None, None, tz))
        steps += [
            {"label": "New leads", "value": funnel["leads"], "src": "FUB", "sub": "added to Follow Up Boss"},
            {"label": "Appointments set", "value": y["appts_set"], "src": "FUB", "sub": "set this year"},
            {"label": "Appointments held", "value": y["held"], "src": "FUB",
             "sub": f"{R.pct(y['held'], y['held'] + y['not_held'], 0):.0f}% of the ones with an outcome"},
        ]
    if deals:
        d = deals["ytd"]
        steps += [
            {"label": "Offers accepted", "value": d["written"], "src": "CTE",
             "sub": f"{d['cancelled']} fell through" if d["cancelled"] else "went under contract"},
            {"label": "Closed", "value": d["closed"], "src": "CTE", "sub": f"${float(d['closed_vol']) / 1e6:.1f}M volume"},
        ]
    for i, s in enumerate(steps):
        s["of_prev"] = R.pct(s["value"], steps[i - 1]["value"], 0) if i and steps[i - 1]["value"] else None
        s["bar"] = R.pct(s["value"], steps[0]["value"], 1) if steps[0]["value"] else 0
    paid = None
    if money:
        books = money.get("books") or {}
        paid = {"compass": money.get("compass"), "books": books.get("income"), "net": money.get("net"),
                "gci": float(deals["ytd"]["gci"]) if deals else None}
    return {"steps": steps, "paid": paid} if steps else None


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
            when = (s.get("bill_date") or s.get("paid_on"))
            typos.append({"address": d["address"], "close_date": d["close_date"], "what": what, "fix": fix,
                          "when": when, "anchor": f"deal-{d['file_year']}-{d['row_num']}", "agent": d["agent"],
                          "gci": d["gci"], "deal_type": d["deal_type"], "receipt": s,
                          "file_year": d["file_year"], "row_num": d["row_num"]})
    return {"total": len(deals), "missing": len(missing), "typos": len(typos), "typo_list": typos,
            "missing_list": missing, "unmatched_list": unmatched,
            "unmatched": len(unmatched), "any_receipts": any(d["receipts"] for d in deals) or bool(unmatched)}


def both_sides_rows(cur, year):
    """Sales where the team had both sides: each side is its own row in CTE."""
    return R.fetch(cur, """
        SELECT d.* FROM cte_deals d JOIN (
            SELECT LOWER(SPLIT_PART(TRIM(address), ' ', 1)) AS n, LOWER(SPLIT_PART(TRIM(address), ' ', 2)) AS w, close_date
            FROM cte_deals WHERE status = 'Closed' AND file_year = %(y)s AND close_date IS NOT NULL
            GROUP BY 1, 2, 3 HAVING COUNT(*) > 1) x
          ON LOWER(SPLIT_PART(TRIM(d.address), ' ', 1)) = x.n AND LOWER(SPLIT_PART(TRIM(d.address), ' ', 2)) = x.w
         AND d.close_date = x.close_date
        WHERE d.status = 'Closed' AND d.file_year = %(y)s ORDER BY d.close_date, d.deal_type DESC""", {"y": year})


def low_pct_rows(cur, year):
    """Agent deals (not the owners') closed under the contracts' 2% minimum commission."""
    return [r for r in R.fetch(cur, """
        SELECT d.* FROM cte_deals d WHERE status = 'Closed' AND file_year = %(y)s AND sale_price > 0
          AND gci / sale_price < 0.0199 ORDER BY primary_agent, close_date""", {"y": year})
        if (r["primary_agent"] or "").strip().lower() not in SP.OWNERS]


def no_signed_date_rows(cur, year):
    return R.fetch(cur, """SELECT d.* FROM cte_deals d WHERE file_year = %(y)s AND deal_type = 'Listing' AND signed_date IS NULL
                           AND status NOT IN ('Cancelled', 'Sale Failed', 'Expired') ORDER BY row_num""", {"y": year})


def no_split_rows(cur, year):
    return R.fetch(cur, """SELECT d.* FROM cte_deals d WHERE file_year = %(y)s AND status = 'Closed'
                           AND primary_pct IS NULL AND primary_gci IS NULL ORDER BY row_num""", {"y": year})


def missing_field_rows(cur, year):
    return R.fetch(cur, """SELECT d.* FROM cte_deals d WHERE file_year = %(y)s AND status = 'Closed'
                           AND (source IS NULL OR TRIM(source) = '' OR commission_pct IS NULL) ORDER BY row_num""", {"y": year})


# CTE "My Business" sheet: the column letter and header of each field (cte_import.DEAL_COLS)
CTE_COL = {"deal_type": ("W", "Type"), "status": ("X", "Status"), "address": ("Y", "Address"), "source": ("AA", "Lead Source"),
           "signed_date": ("AB", "Signed Date"), "list_date": ("AC", "List Date"), "close_date": ("AG", "Close Date"),
           "sale_price": ("AI", "Sale Price"), "commission_pct": ("AK", "Commission %"), "gci": ("AM", "GCI"),
           "primary_agent": ("AS", "Primary Agent"), "primary_pct": ("AT", "Primary %"), "primary_gci": ("AU", "Primary GCI")}


def cell(field, row):
    col, name = CTE_COL[field]
    return f"{name} {col}{row}"


def _fix_cells(kind, r):
    n = r["row_num"]
    if kind == "low_pct":
        return f"{cell('commission_pct', n)} / {cell('gci', n)}: approval, or correct if mistyped"
    if kind == "both_sides":
        return f"{cell('deal_type', n)}: decide 1 or 2 closings (no edit yet)"
    if kind == "no_signed_date":
        return f"{cell('signed_date', n)}: fill in"
    if kind == "no_split_pct":
        return f"{cell('primary_pct', n)} / {cell('primary_gci', n)}: fill in"
    if kind == "missing_fields":
        out = []
        if not (r.get("source") or "").strip():
            out.append(cell("source", n))
        if r.get("commission_pct") is None:
            out.append(cell("commission_pct", n))
        return ", ".join(out) + ": fill in"
    return ""


FOCUS = {"both_sides": ("Sales where we had both sides", both_sides_rows),
         "low_pct": ("Agent deals under the 2% minimum commission", low_pct_rows),
         "no_signed_date": ("Listings with no Signed Date", no_signed_date_rows),
         "no_split_pct": ("Closed deals with no agent split (Primary % / Primary GCI)", no_split_rows),
         "missing_fields": ("Closed deals missing a lead source or commission %", missing_field_rows)}


def focus_deals(cur, kind, year):
    """(title, deal rows with the cells to fix) for the CTE page when it's opened from a Needs-attention item."""
    if kind not in FOCUS:
        return None
    title, fn = FOCUS[kind]
    rows = fn(cur, year)
    for r in rows:
        r["fix"] = _fix_cells(kind, r)
    return {"kind": kind, "title": title, "rows": rows}


def _sides(rows):
    return " / ".join(f"{x['deal_type']} {x['primary_agent']}" for x in rows)


# One-time questions that the data can't answer by itself; each leaves the list once its decision is
# marked Fixed on the Decisions page. (key, title, detail, link, choices)
STANDING = [
    ("setup:open_house_leads", "Open-house leads: team lead or the agent's own?",
     "Splits count them as team/database leads (higher company share). Compass has paid most of Margaryta's open-house "
     "deals at her personal rate, so they show as under contract.",
     ("splits_page", {"_anchor": "deals"}),
     ["Team / database lead", "Agent's own (personal) lead"]),
    ("setup:team_past_client", "\"Team Past Client\" leads: team lead or the agent's own?",
     "Donna's 856 Elm Ave is marked Team Past Client and was paid at her personal rate (20%) instead of team (40%).",
     ("splits_page", {"_anchor": "deals"}),
     ["Team lead (database split)", "Agent's own (personal split)"]),
    ("setup:margaryta_agreements", "Margaryta has 3 agreements: which one is in force now?",
     "10/2025 contract (70/30 sphere, 50/50 rest), 2/23/2026 12-month amendment (75/25 all, 80/20 after $10M), "
     "4/18/2026 agreement (25/35/35, 80/20 after $10M career). Splits use the newest signed one. "
     "Since she passed $10M on 6/2, Diamond St (~$2,300) and Old Bridgeport (~$600) look owed to her.",
     ("splits_page", {"_anchor": "contract-Margaryta-Gvritishvili"}),
     ["Newest (4/18) agreement is in force", "The 2/23 amendment runs its 12 months"]),
    ("setup:compass_fee_wording", "Agreements say Compass takes 10%, but Compass keeps 7.5% + about $150",
     "Every agreement's example math nets out 10%. The real Compass statements take 7.5% plus a ~$150 fee, "
     "so actual numbers come out a little higher than the contract examples.",
     ("splits_page", {"_anchor": "contracts"}),
     ["Update the agreement wording", "Leave it as is"]),
    ("setup:escrow_statements", "Keep the Compass escrow statements in the totals?",
     "\"Agent Remittance Paid by Escrow\" emails are imported along with the Upcoming Payment ones; you said you'd "
     "decide later whether to keep them.",
     ("compass_invoices", {}),
     ["Keep them", "Remove them"]),
    ("setup:onedrive_duplicate_folder", "OneDrive has a duplicate \"CTE\" folder next to \"CTE FILES\"",
     "Only CTE FILES is imported. An edit made in the other folder (it happened once) never reaches the dashboard.",
     ("cte", {}),
     ["Rename or remove the duplicate folder", "Keep it"]),
    ("setup:onedrive_button", "The \"Update from OneDrive now\" button is hidden",
     "The Microsoft keys are only on the cron job, so CTE changes show up after the daily run. "
     "Adding the same MS_* keys to the web service in Render shows the button.",
     ("cte", {}),
     ["Add the keys to the web service", "Daily update is enough"]),
]


def _data_checks(cur, year, receipts):
    """Data problems in the CTE file and the connections, found from the data (gone once fixed)."""
    items = []
    one = lambda sql, p=None: R.fetch(cur, sql, p or {"y": year})[0]
    r = one("""SELECT COUNT(*) AS rows, COUNT(DISTINCT agent_name) AS agents FROM cte_activity
               WHERE file_year = %(y)s AND EXTRACT(YEAR FROM activity_date) = %(y)s""")
    deal_agents = one("""SELECT COUNT(DISTINCT TRIM(primary_agent)) AS n FROM cte_deals
                         WHERE file_year = %(y)s AND status = 'Closed'""")["n"]
    if r["rows"] < 5 * max(deal_agents, 1) * 4:
        items.append({"level": "warn", "key": f"lead_gen_empty:{year}",
                      "title": f"Agents barely log in the CTE Lead Gen sheet: {r['rows']} rows all year",
                      "detail": f"{r['agents']} people logged anything; {deal_agents} agents closed deals. Offers written, "
                                "open houses held and dials can't be counted until it's filled daily (the agreements require it).",
                      "link": ("cte", {}), "choices": ["Remind agents to log daily", "Stop using the Lead Gen sheet"]})
    r = one("""SELECT COUNT(*) AS n FROM cte_deals WHERE file_year = %(y)s AND deal_type = 'Listing'
               AND signed_date IS NULL AND status NOT IN ('Cancelled', 'Sale Failed', 'Expired')""")
    if r["n"]:
        items.append({"level": "info", "key": f"no_signed_date:{year}",
                      "title": f"{r['n']} listings in the CTE file have no Signed Date",
                      "detail": "Listings Taken uses the list date instead. Fill Signed Date to count listings when they're signed.",
                      "link": ("cte", {})})
    r = one("""SELECT COUNT(*) AS n FROM cte_deals WHERE file_year = %(y)s AND status = 'Closed'
               AND primary_pct IS NULL AND primary_gci IS NULL""")
    if r["n"]:
        items.append({"level": "info", "key": f"no_split_pct:{year}",
                      "title": f"{r['n']} closed deals have no agent split in CTE (Primary % / Primary GCI empty)",
                      "detail": "With the split filled in, CTE can be checked against Compass and the contracts deal by deal.",
                      "link": ("splits_page", {"_anchor": "deals"}),
                      "choices": ["Start filling the split in CTE", "Not needed: Compass receipts are enough"]})
    rows = R.fetch(cur, """SELECT address, primary_agent, source IS NULL OR TRIM(source) = '' AS no_src,
                                  commission_pct IS NULL AS no_pct
                           FROM cte_deals WHERE file_year = %(y)s AND status = 'Closed'
                             AND (source IS NULL OR TRIM(source) = '' OR commission_pct IS NULL)""", {"y": year})
    if rows:
        items.append({"level": "info", "key": f"missing_fields:{year}",
                      "title": f"{len(rows)} closed deals are missing a lead source or commission % in CTE",
                      "detail": "; ".join(f"{x['address']} ({x['primary_agent']}: "
                                          + ", ".join(w for w, on in (("no source", x["no_src"]), ("no %", x["no_pct"])) if on) + ")"
                                          for x in rows),
                      "link": ("cte", {})})
    if R.tables_ready(cur, "agents"):
        cte_names = CTE.agent_options(cur)
        fub = {(CTE.name_for(cur, n, cte_names) or "").lower() for n in R.agent_names(cur).values()}
        missing = [x["name"] for x in R.fetch(cur, """SELECT DISTINCT TRIM(primary_agent) AS name FROM cte_deals
                                                      WHERE file_year = %(y)s AND status IN ('Closed', 'Pending')
                                                        AND COALESCE(TRIM(primary_agent), '') <> '' ORDER BY 1""", {"y": year})
                   if x["name"].lower() not in fub]
        if missing:
            items.append({"level": "info", "key": "cte_not_in_fub:" + ",".join(missing),
                          "title": f"{len(missing)} agent{'s' if len(missing) != 1 else ''} with deals in CTE not found in Follow Up Boss",
                          "detail": "Their appointments and calls can't be shown next to their deals: " + ", ".join(missing)
                                    + ". Different spelling, or not set up in FUB?",
                          "link": ("cte", {"_anchor": "agents"}),
                          "choices": ["Spelled differently: I'll say how in the comment", "Not in FUB: that's fine"]})
    if receipts and receipts.get("unmatched"):
        items.append({"level": "info", "key": f"unmatched_receipts:{year}",
                      "title": f"{receipts['unmatched']} Compass receipt{'s' if receipts['unmatched'] != 1 else ''} this year match no deal in CTE",
                      "detail": "Referral or other income, or a deal missing from the CTE file?",
                      "link": ("compass_invoices", {"_anchor": "unmatched"}),
                      "choices": ["Not deals (referrals etc.): OK", "Deals missing from CTE: add them"]})
    # last year's closings with no receipt (the mailbox has no escrow emails before 08/2025)
    try:
        deals, _ = gmail_import.deal_receipts(cur, date(year - 1, 1, 1), date(year, 1, 1))
    except Exception:
        deals = []
    if deals and any(d["receipts"] for d in deals):
        miss = [d for d in deals if not d["receipts"]]
        if miss:
            items.append({"level": "info", "key": f"missing_receipts:{year - 1}",
                          "title": f"{year - 1}: {len(miss)} of {len(deals)} closed deals have no Compass receipt",
                          "detail": "The mailbox has no \"Paid by Escrow\" emails before August 2025, so most of these are "
                                    "from the first half. Forward the old statements to the connected Gmail, or accept the gap.",
                          "link": ("compass_invoices", {"year": year - 1, "deals": "missing"}),
                          "choices": ["Find and forward the old statements", "OK: not needed for that year"]})
    return items


def _books_items(cur, year):
    """One item per QuickBooks income entry with no Compass payment, and per Compass payment that isn't in
    QuickBooks (from gmail_import.books_match). Bank interest is grouped into one line."""
    m = gmail_import.books_match(cur, year)
    if not m:
        return []
    items = []
    interest = [t for t in m["qb_only"] if "interest" in (t["account"] or "").lower()]
    for t in m["qb_only"]:
        if t in interest:
            continue
        memo = " ".join((t["memo"] or "").split())
        items.append({"level": "ask", "key": f"qb_only:{t['date']}|{t['amount']:.2f}",
                      "title": f"QuickBooks {t['type'] or 'entry'} {t['date'].strftime('%m/%d/%Y')} ${t['amount']:,.2f}: no Compass payment matches",
                      "detail": f"Booked to {t['account'] or 'income'}" + (f" from {t['name']}" if t["name"] else "")
                                + (f". Bank memo: {memo[:140]}" if memo else "") + ". What is it?",
                      "link": ("compass_invoices", {"year": year, "_anchor": "books-match"}),
                      "choices": ["Not from Compass: real income, OK (say what in the comment)",
                                  "Should match a Compass payment: look into it"],
                      "_row": t})
    if interest:
        items.append({"level": "info", "key": f"qb_interest:{year}",
                      "title": f"QuickBooks bank interest {year}: ${sum(t['amount'] for t in interest):,.2f}",
                      "detail": "Counted as income in QuickBooks but isn't a Compass payment, so it's part of the difference.",
                      "link": ("compass_invoices", {"year": year, "_anchor": "books-match"})})
    for p in m["compass_only"]:
        what = "Paid by escrow" if p["kind"] == "escrow" else "Compass payment"
        items.append({"level": "ask", "key": f"compass_only:{p['id']}",
                      "title": f"{what} {p['date'].strftime('%m/%d/%Y')} ${p['amount']:,.2f} ({(p['property'] or '').split(',')[0]}): not found in QuickBooks",
                      "detail": "No QuickBooks income entry with this amount within 3 weeks. Check the bank deposits around this date.",
                      "link": ("compass_invoices", {"year": year, "_anchor": "books-match"}),
                      "choices": ["Found: deposited as a different amount or combined", "Missing: never deposited, follow up"],
                      "_row": p})
    return items


def _goal_items(cur, year):
    """The year's GCI and net goals, when nobody has typed them in on Business Overview."""
    goals = CTE.year_goals(cur, year)
    items = []
    if goals["gross"] is None:
        items.append({"level": "ask", "key": f"goal_gross:{year}",
                      "title": f"No {year} gross (GCI) goal is set",
                      "detail": "The Oct-Mar plan only has closings per month (10, 10, 12 for Oct-Dec), no GCI dollar goal. "
                                "Business Overview shows the plan's closings until a goal is typed in under \"Set goals\".",
                      "link": ("business_overview", {"_anchor": "year-totals"}),
                      "choices": ["Set a GCI goal (amount in the comment)", "Closings from the plan are enough"]})
    if goals["net"] is None:
        items.append({"level": "ask", "key": f"goal_net:{year}",
                      "title": f"No {year} net income goal is set",
                      "detail": "Business Overview shows the plan's $1M a year main goal against the run rate. "
                                "For a calendar-year net goal, type it under \"Set goals\".",
                      "link": ("business_overview", {"_anchor": "year-totals"}),
                      "choices": ["Set a net goal for the year (amount in the comment)", "The $1M run-rate goal is enough"]})
    return items


def _spend_items(cur, today):
    """The Oct-Mar plan's expense cap: the last 3 months' average spend, and the 'over the cap 2 months in a row' gate."""
    import qbo_reports as QR
    caps = QR.expense_caps(cur)
    cap = next((c["cap"] for c in reversed(caps)), None) or dict((k, v) for k, _, v, _ in G.MONTHLY)["expenses"][0]
    k = QR.kpis(cur, today.date()) if QR._has_txns(cur) else None
    items = []
    if k and k["avg_spend_3m"] and k["avg_spend_3m"] > cap:
        items.append({"level": "warn", "key": f"spend_over_cap:{today.strftime('%Y-%m')}",
                      "title": f"Spending ${k['avg_spend_3m']:,.0f} a month, over the plan's ${cap:,.0f} cap",
                      "detail": "Average of the last 3 full months in QuickBooks. The plan's gate: over the cap 2 months in a row means review.",
                      "link": ("quickbooks", {"_anchor": "spend"}),
                      "where": "QuickBooks page › Expenses by category and Recurring charges: what to cut, or raise the cap in the plan",
                      "choices": ["Cut spending (say what in the comment)", "Raise the cap (new amount in the comment)"]})
    over = [c for c in caps if c["over"] and c["month"] < today.date().replace(day=1)]
    if len(over) >= 2:
        items.append({"level": "bad", "key": f"cap_gate:{over[-1]['month']:%Y-%m}",
                      "title": "Gate: spending over the cap 2 months in a row: review",
                      "detail": ", ".join(f"{c['month']:%b} ${c['spent']:,.0f} vs ${c['cap']:,.0f}" for c in over[-2:]),
                      "link": ("quickbooks", {"_anchor": "spend"})})
    return items


def _standing(latest):
    """The one-time questions still open (not marked Fixed)."""
    out = []
    for key, title, detail, link, choices in STANDING:
        d = latest.get(key)
        if d and d["status"] == "fixed":
            continue
        out.append({"level": "ask", "key": key, "title": title, "detail": detail, "link": link, "choices": choices})
    return out


def _questions(cur, year):
    """Open questions found in the data for the owners to decide. Each disappears once it's fixed.
    choices: the two answers offered in the dropdown (none = comment only)."""
    items = []
    owners = SP.OWNERS
    rows = both_sides_rows(cur, year)
    if rows:
        sales = {}
        for r in rows:
            sales.setdefault((r["address"].split()[0].lower(), r["close_date"]), []).append(r)
        extra = sum(float(v[0]["sale_price"] or 0) * (len(v) - 1) for v in sales.values())
        items.append({"level": "ask", "key": f"both_sides:{year}",
                      "title": f"{len(sales)} sales where we had both sides: count as 1 closing or 2?",
                      "detail": f"Each side is a closing now, so ${extra:,.0f} of volume is counted twice. "
                                + "; ".join(f"{v[0]['address']} ({_sides(v)})" for v in sales.values()),
                      "link": ("cte", {"focus": "both_sides", "_anchor": "focus"}),
                      "choices": ["Count as 2 closings (one per side)", "Count as 1 closing (volume once)"]})
    low = low_pct_rows(cur, year)
    if low:
        items.append({"level": "ask", "key": f"low_pct:{year}",
                      "title": f"{len(low)} agent deal{'s' if len(low) != 1 else ''} under the 2% minimum commission: approved?",
                      "detail": "Contracts need written approval below 2% or $1,500. "
                                + "; ".join(f"{r['address']} ({r['primary_agent']}, {float(r['gci']) / float(r['sale_price']) * 100:.2f}%)" for r in low),
                      "link": ("cte", {"focus": "low_pct", "_anchor": "focus"}),
                      "choices": ["All approved", "Not approved: talk to the agents"]})
    names = CTE.agent_options(cur)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if a.lower() in owners or b.lower() in owners:
                continue
            fa, fb = a.lower().split(), b.lower().split()
            first = SequenceMatcher(None, fa[0], fb[0]).ratio() >= 0.8
            last = len(fa) > 1 and len(fb) > 1 and SequenceMatcher(None, fa[-1], fb[-1]).ratio() >= 0.8
            if first and (last or len(fa) == 1 or len(fb) == 1 or fa[0] == fb[0]):
                items.append({"level": "ask", "key": f"same_person:{a}|{b}",
                              "title": f"\"{a}\" and \"{b}\" in CTE: same person?",
                              "detail": "If they are, their deals and activity get counted together.",
                              "link": ("cte", {"cte_agent": b}),
                              "choices": ["Same person: count together", "Different people"]})
    answered = {k[len("contract:"):] for k, d in (_safe(cur, "decisions", lambda: decisions.latest(cur), {}) or {}).items()
                if k.startswith("contract:") and d["choice"] and (d["choice"] in SP.READINGS.get(k[len("contract:"):], {})
                                                                  or (d["choice"].startswith("Signed") and d["comment"]))}
    import drive_contracts
    for d in _safe(cur, "drive contracts", lambda: drive_contracts.rows(cur), []) or []:
        if d["problem"]:
            read = (f"Read: company {d['personal']}% personal / {d['zillow']}% Zillow / {d['database']}% team"
                    + (f", {d['bonus']}% after $10M" if d["bonus"] else "")
                    + (f", signed {d['signed'].strftime('%m/%d/%Y')}" if d["signed"] else "") + ".")
            items.append({"level": "ask", "key": f"drive_contract:{d['file_id']}",
                          "title": f"New agreement in Drive for {d['agent']}: {d['problem']}",
                          "detail": f"{d['file']}. {read} Until it's decided, the splits don't use it.",
                          "link": ("splits_page", {"_anchor": "drive"}),
                          "choices": [drive_contracts.USE_AS_READ, "Ignore this file"]})
    for c in SP.CONTRACTS:
        if c["agent"] in answered:
            continue
        if c["since"] is None:
            items.append({"level": "ask", "key": f"contract:{c['agent']}",
                          "title": f"{c['agent']}'s agreement isn't signed or dated",
                          "detail": f"{c['file']} in Drive. Her deals can't be checked against a split until it's in force.",
                          "link": ("splits_page", {"_anchor": "contract-" + c["agent"].replace(" ", "-")}),
                          "choices": ["Signed: date in the comment", "Not on the team / no agreement"]})
        elif " but " in c["note"]:
            split_says, sheet_says = c["note"].split(" but ", 1)
            items.append({"level": "ask", "key": f"contract:{c['agent']}",
                          "title": f"{c['agent']}'s agreement contradicts itself: which split is right?",
                          "detail": f"{split_says.strip().rstrip(',')}, but {sheet_says.strip()}.",
                          "link": ("splits_page", {"_anchor": "contract-" + c["agent"].replace(" ", "-")}),
                          "choices": [f"Split table is right", f"Cheat sheet is right"]})
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
                items.append({"level": "ask", "key": f"books_vs_compass:{y}",
                              "title": f"{y}: QuickBooks is ${books - compass:+,.0f} off from Compass",
                              "detail": f"QuickBooks income ${books:,.0f} vs Compass YTD ${compass:,.0f}.",
                              "link": ("compass_invoices", {"year": y, "_anchor": "books-match"}),
                              "choices": ["Income from outside Compass: OK", "Something is missing: look into it"]})
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
            items.append({"level": "bad" if gap < -1000 else "warn", "key": f"off_contract:{now.year}",
                          "title": f"{len(off)} deal{'s' if len(off) != 1 else ''} paid off the agent's contract split",
                          "detail": f"Company got ${gap:+,.0f} against the contracts this year.",
                          "link": ("splits_page", {"_anchor": "deals"}),
                          "choices": ["Paid right: update the contracts", "Paid wrong: fix with the agents"]})
        pending = sorted({r["agent"] for r in checks if r["status"] == "contract_pending" and r["agent"]})
        for a in pending:
            items.append({"level": "info", "key": f"contract_pending:{a}",
                          "title": f"{a}'s new agreement: put it in Drive once signed",
                          "detail": f"{SP.CONTRACT_PENDING.get(a.lower(), '')}. Until then his deals aren't checked against a split.",
                          "link": ("splits_page", {"_anchor": "contracts"})})
        no_contract = sorted({r["agent"] for r in checks if r["status"] == "no_contract" and r["agent"]})
        if no_contract:
            items.append({"level": "warn", "key": "no_contract:" + ",".join(no_contract),
                          "title": "No agreement in Drive for " + ", ".join(no_contract),
                          "detail": "Their deals can't be checked against a split.", "link": ("splits_page", {"_anchor": "contracts"})})
    for t in (receipts or {}).get("typo_list", []):
        items.append({"level": "ask", "key": f"typo:{t['address']}|{t['close_date']}",
                      "title": f"CTE typo? {t['address']} ({t['close_date'].strftime('%m/%d/%Y')}, {t['agent'] or 'no agent'})",
                      "detail": f"The Compass receipt has {t['what']} " + (f"{t['when'].strftime('%m/%d/%Y')}" if t["what"] == "close date" and t["when"] else t["fix"]) + ".",
                      "link": ("compass_invoices", {"deals": "missing", "_anchor": t["anchor"]}),
                      "choices": ["Compass is right: fix the CTE file", "CTE is right"]})
    if receipts and receipts["missing"] and receipts["any_receipts"]:
        items.append({"level": "bad" if receipts["missing"] - receipts["typos"] > 2 else "warn", "key": f"missing_receipts:{now.year}",
                      "title": f"{receipts['missing']} closed deal{'s' if receipts['missing'] != 1 else ''} with no Compass receipt",
                      "detail": (f"{receipts['typos']} look like a typo in the CTE address or date. " if receipts["typos"] else "")
                      + f"{receipts['total'] - receipts['missing']} of {receipts['total']} closings this year are matched.",
                      "link": ("compass_invoices", {"deals": "missing"})})
    if money and money.get("compass") is not None and money.get("books") is not None:
        diff = money["books"] - money["compass"]
        if abs(diff) > max(1000, 0.01 * money["compass"]):
            items.append({"level": "warn", "key": f"books_vs_compass:{now.year}",
                          "choices": ["Income from outside Compass: OK", "Something is missing: look into it"],
                          "title": f"QuickBooks and Compass differ by ${diff:+,.0f} this year",
                          "detail": f"QuickBooks income ${money['books']:,.0f} vs Compass YTD ${money['compass']:,.0f}.",
                          "link": ("compass_invoices", {"_anchor": "books-match"})})
    if money and money.get("gross_vs") and money["gross_vs"]["pace_diff"] < 0:
        g = money["gross_vs"]
        items.append({"level": "warn", "key": f"gci_pace:{now.year}", "title": f"GCI is ${-g['pace_diff']:,.0f} behind the pace for the year's goal",
                      "detail": f"${money['gross']:,.0f} of ${g['goal']:,.0f} ({g['pct']:.0f}%), "
                                f"{money['pace_pct']:.0f}% of the year gone.", "link": ("business_overview", {})})
    if leads:
        if leads["stale_new_pct"] > 10:
            items.append({"level": "warn", "key": "stale_new", "title": f"{leads['stale_new_pct']}% of last 30 days' new leads still in \"New\" after 48h",
                          "detail": "Get them contacted and staged.", "link": ("dashboard", {"tab": "pipeline", "_anchor": "lead-health"})})
        if leads["held_rate"] is not None and leads["held"] + leads["not_held"] and leads["held_rate"] < 50:
            items.append({"level": "warn", "key": "held_rate", "title": f"Only {leads['held_rate']}% of appointments held in the last 30 days",
                          "detail": f"{leads['held']} held, {leads['not_held']} not held.", "link": ("appointments", {"period": "last30"})})
    if deals and deals["ytd"]["cancelled"]:
        items.append({"level": "info", "key": f"fell_through:{now.year}", "title": f"{deals['ytd']['cancelled']} deal{'s' if deals['ytd']['cancelled'] != 1 else ''} fell through this year",
                      "detail": "Went under contract and then cancelled.", "link": ("business_overview", {})})
    for s in fresh or []:
        if s["error"]:
            items.append({"level": "bad", "key": f"source:{s['name']}", "title": f"{s['name']} needs attention", "detail": s["error"], "link": (s["link"], {})})
        elif s["at"] and now - s["at"] > timedelta(hours=36):
            items.append({"level": "warn", "key": f"source:{s['name']}", "title": f"{s['name']} hasn't updated since {s['at'].strftime('%m/%d %I:%M %p')}",
                          "detail": "Check the daily cron job on Render.", "link": (s["link"], {})})
    items += questions or []
    order = {"bad": 0, "warn": 1, "ask": 2, "info": 3}
    for i in items:
        i.setdefault("choices", [])
    return sorted(items, key=lambda i: order[i["level"]])


_cache = {"at": 0.0, "key": None, "data": None}
CACHE_SECONDS = 120


def clear_cache():
    """Mark the cached dashboard numbers stale (after a decision is saved): the next visit rebuilds them."""
    _cache["at"] = 0.0


def overview(cur, today, tz, year_totals):
    """The dashboard home, reused for CACHE_SECONDS so reloads are instant."""
    key = today.date()
    if _cache["data"] is not None and _cache["key"] == key and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    return refresh(cur, today, tz, year_totals)


def refresh(cur, today, tz, year_totals):
    """Rebuild the dashboard numbers now and keep them for the next visits."""
    gmail_import.memo_start()
    try:
        data = _overview(cur, today, tz, year_totals)
    finally:
        gmail_import.memo_stop()
    _cache.update(at=time.monotonic(), key=today.date(), data=data)
    return data


def _overview(cur, today, tz, year_totals):
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
    # leads created this year: how far each has got in Follow Up Boss today (same as the Lead Source Report)
    funnel = _safe(cur, "funnel", lambda: R.lead_funnel(cur, R.Filters(today.replace(month=1, day=1), today + timedelta(days=1),
                                                                        None, None, tz))) if has_fub else None
    checks = _safe(cur, "splits", lambda: SP.deal_check(cur, date(year, 1, 1), date(year + 1, 1, 1))) if has_cte else None
    receipts = _safe(cur, "receipts", lambda: _receipts(cur, year)) if has_cte else None
    agents = _safe(cur, "agents", lambda: _agents(cur, today, checks), []) if has_cte else []
    fresh = _safe(cur, "freshness", lambda: _freshness(cur), [])
    questions = _safe(cur, "questions", lambda: _questions(cur, year), []) if has_cte else []
    questions += _safe(cur, "data checks", lambda: _data_checks(cur, year, receipts), []) if has_cte else []
    questions += _safe(cur, "books items", lambda: _books_items(cur, year), []) or []
    questions += _safe(cur, "goal items", lambda: _goal_items(cur, year), []) if has_cte else []
    questions += _safe(cur, "spend items", lambda: _spend_items(cur, today), []) or []
    questions += _standing(_safe(cur, "decisions", lambda: decisions.latest(cur), {}) or {})
    company = None
    if checks:
        paid = [r["company"] for r in checks if r["company"] is not None]
        company = {"total": sum(paid), "deals": len(paid)}
    pipeline = _safe(cur, "pipeline", lambda: _pipeline(cur, today, tz, funnel, deals, money))
    return {"year": year, "money": money, "deals": deals, "leads": leads, "funnel": funnel, "pipeline": pipeline,
            "agents": agents, "company": company,
            "receipts": receipts, "fresh": fresh,
            "attention": _with_decisions(cur, _attention(money, deals, leads, receipts, checks, fresh, now, questions),
                                         year, checks, receipts, money)}


def _with_decisions(cur, items, year=None, checks=None, receipts=None, money=None):
    """Attach the latest owner decision (dropdown choice + comment), the detail table and where to fix it."""
    _safe(cur, "where", lambda: add_where(cur, items, year, receipts))
    latest = _safe(cur, "decisions", lambda: decisions.latest(cur), {}) or {}
    # an item whose decision is marked Fixed is done, even when the data behind it stays the same
    items = [i for i in items if not (latest.get(i["key"]) and latest[i["key"]]["status"] == "fixed")]
    for i in items:
        i["decision"] = latest.get(i["key"])
        i["table"] = _safe(cur, f"details {i['key']}", lambda: details(cur, i, year, checks, receipts, money or {}))
    return items


# ---------------------------------------------------------------- detail tables for the Needs-attention items

def _m(v):
    return "" if v is None else f"${float(v):,.0f}"


def _d(v):
    return v.strftime("%m/%d/%Y") if v else ""


def _p(v):
    return "" if v is None else f"{float(v):.1f}%"


def _tbl(cols, rows, num=()):
    """A small table for an item's details: column names, rows of strings, and which columns are numbers."""
    return {"cols": cols, "rows": rows, "num": set(num)} if rows else None


def _check_rows(rows):
    return _tbl(["Closed", "Address", "Agent", "Lead (CTE source)", "GCI", "Company got", "Actual %", "Contract %", "Difference"],
                [[_d(r["close_date"]), r["address"], r["agent"] or "", r["source"] or "", _m(r["gci"]), _m(r["company"]),
                  _p(r["actual_pct"]), _p(r["expected_pct"]) + (" (after $10M)" if r.get("bonus") else ""),
                  (f"{r['gap']:+,.0f}" if r.get("gap") is not None else "")] for r in rows],
                num=(4, 5, 6, 7, 8))


def _what_if(rows, readings):
    """Company share on an agent's deals under each reading of an unclear agreement."""
    out = []
    for r in rows:
        if not r["gci"]:
            continue
        b = SP.base(r["gci"])
        line = [_d(r["close_date"]), r["address"], r["source"] or "",
                _m(r["company"]) if r["company"] is not None else "no receipt", _p(r["actual_pct"])]
        for label, pers, zil, db in readings:
            pct = {"personal": pers, "zillow": zil, "database": db}[r["lead"]]
            line.append(f"{pct}% = {_m(b * pct / 100)}")
        out.append(line)
    return _tbl(["Closed", "Address", "Lead", "Company got", "Actual %"] + [x[0] for x in readings], out, num=(3, 4, 5, 6))


def details(cur, item, year, checks, receipts, money):
    """Rows behind an item, to compare side by side (None when there's nothing to list)."""
    key = item["key"]
    kind, _, arg = key.partition(":")
    checks = checks or []
    if kind == "off_contract":
        return _check_rows([r for r in checks if r["status"] in ("under", "over")])
    if kind == "no_contract":
        return _check_rows([r for r in checks if r["status"] == "no_contract"])
    if kind == "typo":
        t = next((t for t in (receipts or {}).get("typo_list", []) if f"{t['address']}|{t['close_date']}" == arg), None)
        if t:
            s = t["receipt"]
            return _tbl(["", "CTE file", "Compass receipt"], [
                ["Address", t["address"], s.get("property") or s.get("description") or ""],
                ["Close / bill date", _d(t["close_date"]), _d(s.get("bill_date") or s.get("paid_on"))],
                ["Agent / side", f"{t['agent'] or ''} · {t['deal_type'] or ''}", ""],
                ["GCI / commission", _m(t["gci"]), _m(s.get("close_price")) and f"close price {_m(s.get('close_price'))}"],
                ["Company got", "", f"{_m(s.get('gross'))} gross · {_m(s.get('amount'))} paid"]])
    if kind == "missing_receipts":
        y = int(arg)
        miss = (receipts or {}).get("missing_list", []) if y == year else \
            [d for d in gmail_import.deal_receipts(cur, date(y, 1, 1), date(y + 1, 1, 1))[0] if not d["receipts"]]
        return _tbl(["Closed", "Address", "Agent", "Side", "GCI", "Possible match"],
                    [[_d(d["close_date"]), d["address"], d["agent"] or "", d["deal_type"] or "", _m(d["gci"]),
                      "; ".join(f"{s.get('property') or s.get('description')} ({s['why']})" for s in d["suggestions"][:1])]
                     for d in miss], num=(4,))
    if kind == "books_vs_compass":
        rows = gmail_import.monthly_vs_books(cur, int(arg))
        return _tbl(["Month", "Compass this month", "QuickBooks this month", "Compass YTD", "QuickBooks YTD", "Difference"],
                    [[r["month"].strftime("%b %Y"), _m(r["compass_month"]), _m(r["books_month"]), _m(r["compass_ytd"]),
                      _m(r["books_ytd"]),
                      (f"{r['books_ytd'] - r['compass_ytd']:+,.0f}" if r["books_ytd"] is not None and r["compass_ytd"] is not None else "")]
                     for r in rows], num=(1, 2, 3, 4, 5))
    if kind == "gci_pace" and money.get("gross_vs"):
        g = money["gross_vs"]
        return _tbl(["Goal", "GCI so far", "Should be by today", "Behind", "Year gone"],
                    [[_m(g["goal"]), _m(money["gross"]), _m(g["pace_target"]), _m(-g["pace_diff"]), f"{money['pace_pct']:.0f}%"]],
                    num=(0, 1, 2, 3, 4))
    if kind == "fell_through":
        rows = R.fetch(cur, """SELECT under_contract_date, address, primary_agent, deal_type, sale_price, source, status
                               FROM cte_deals WHERE file_year = %(y)s AND status IN ('Cancelled', 'Sale Failed')
                                 AND EXTRACT(YEAR FROM under_contract_date) = %(y)s ORDER BY under_contract_date""", {"y": int(arg)})
        return _tbl(["Under contract", "Address", "Agent", "Side", "Price", "Lead source", "Status"],
                    [[_d(r["under_contract_date"]), r["address"], r["primary_agent"] or "", r["deal_type"] or "",
                      _m(r["sale_price"]), r["source"] or "", r["status"]] for r in rows], num=(4,))
    if kind == "both_sides":
        return _tbl(["Closed", "Address", "Side", "Agent", "Price", "GCI", "Lead source"],
                    [[_d(r["close_date"]), r["address"], r["deal_type"], r["primary_agent"] or "", _m(r["sale_price"]),
                      _m(r["gci"]), r["source"] or ""] for r in both_sides_rows(cur, int(arg))], num=(4, 5))
    if kind == "low_pct":
        out = []
        for r in low_pct_rows(cur, int(arg)):
            price, gci = float(r["sale_price"]), float(r["gci"] or 0)
            minimum = max(0.02 * price, 1500)
            out.append([_d(r["close_date"]), r["address"], r["primary_agent"] or "", r["deal_type"] or "", _m(price),
                        _m(gci), f"{gci / price * 100:.2f}%", _m(minimum), _m(minimum - gci), r["source"] or ""])
        return _tbl(["Closed", "Address", "Agent", "Side", "Price", "GCI", "Commission", "2% minimum", "Short by", "Lead source"],
                    out, num=(4, 5, 6, 7, 8))
    if kind == "same_person":
        names = arg.split("|")
        out = []
        for n in names:
            r = R.fetch(cur, """SELECT COUNT(*) FILTER (WHERE status = 'Closed') AS closed, COALESCE(SUM(gci) FILTER (WHERE status = 'Closed'), 0) AS gci,
                                       MIN(file_year) AS first, MAX(file_year) AS last
                                FROM cte_deals WHERE LOWER(TRIM(primary_agent)) = LOWER(%(n)s)""", {"n": n})[0]
            a = R.fetch(cur, """SELECT COUNT(*) AS rows, MIN(file_year) AS first, MAX(file_year) AS last
                                FROM cte_activity WHERE LOWER(TRIM(agent_name)) = LOWER(%(n)s)""", {"n": n})[0]
            years = sorted({y for y in (r["first"], r["last"], a["first"], a["last"]) if y})
            out.append([n, str(r["closed"]), _m(r["gci"]), str(a["rows"]),
                        f"{years[0]}–{years[-1]}" if len(years) > 1 else (str(years[0]) if years else "")])
        return _tbl(["Name in CTE", "Closed deals", "GCI", "Lead Gen rows", "Years"], out, num=(1, 2, 3))
    if kind == "contract":
        rows = [r for r in checks if (r["agent"] or "").lower() == arg.lower()]
        c = next((c for c in SP.CONTRACTS if c["agent"] == arg), None)
        readings = {"Ahtziri Duran": [("Split table (80/20)", 20, 35, 40), ("Cheat sheet (75/25)", 25, 35, 40)],
                    "Darrion Jackson": [("Split table (70/60/60)", 30, 40, 40), ("Cheat sheet (80/65/60)", 20, 35, 40)]}.get(arg)
        if readings:
            return _what_if(rows, readings)
        if c:
            return _what_if(rows, [("If signed as written", c["personal"], c["zillow"], c["database"])])
    if key == "setup:open_house_leads":
        rows = [r for r in checks if "open house" in (r["source"] or "").lower()]
        out = []
        for r in rows:
            cs = SP.contract_for(r["agent"], r["close_date"], r["address"])
            if not cs:
                continue
            b = SP.base(r["gci"])
            out.append([_d(r["close_date"]), r["address"], r["agent"],
                        _m(r["company"]) if r["company"] is not None else "no receipt", _p(r["actual_pct"]),
                        f"{cs['database']}% = {_m(b * cs['database'] / 100)}", f"{cs['personal']}% = {_m(b * cs['personal'] / 100)}"])
        return _tbl(["Closed", "Address", "Agent", "Company got", "Actual %", "As team lead", "As agent's own"], out, num=(3, 4, 5, 6))
    if key == "setup:team_past_client":
        rows = [r for r in checks if "team past client" in (r["source"] or "").lower()]
        return _check_rows(rows)
    if key == "setup:margaryta_agreements":
        return _check_rows([r for r in checks if (r["agent"] or "").lower().startswith("margaryta")])
    if key == "setup:escrow_statements":
        cur.execute("""SELECT EXTRACT(YEAR FROM p.paid_on)::int, p.kind, COUNT(DISTINCT p.message_id), COALESCE(SUM(i.amount), 0)
                       FROM compass_payments p JOIN compass_payment_items i ON i.message_id = p.message_id
                       WHERE NOT i.is_assist GROUP BY 1, 2 ORDER BY 1 DESC, 2""")
        by = {}
        for y, k, n, amt in cur.fetchall():
            by.setdefault(y, {})[k] = (n, float(amt))
        return _tbl(["Year", "Upcoming Payment emails", "Paid", "Escrow statements", "Paid"],
                    [[str(y), str(v.get("payment", (0, 0))[0]), _m(v.get("payment", (0, 0))[1]),
                      str(v.get("escrow", (0, 0))[0]), _m(v.get("escrow", (0, 0))[1])] for y, v in by.items()], num=(1, 2, 3, 4))
    if kind == "lead_gen_empty":
        rows = R.fetch(cur, """
            SELECT COALESCE(a.name, d.name) AS name, COALESCE(a.rows, 0) AS rows, COALESCE(a.dials, 0) AS dials,
                   COALESCE(a.offers, 0) AS offers, COALESCE(d.closed, 0) AS closed
            FROM (SELECT TRIM(agent_name) AS name, COUNT(*) AS rows, SUM(dials) AS dials, SUM(written_offers) AS offers
                  FROM cte_activity WHERE file_year = %(y)s GROUP BY 1) a
            FULL JOIN (SELECT TRIM(primary_agent) AS name, COUNT(*) AS closed FROM cte_deals
                       WHERE file_year = %(y)s AND status = 'Closed' GROUP BY 1) d ON LOWER(a.name) = LOWER(d.name)
            ORDER BY 5 DESC, 2 DESC""", {"y": int(arg)})
        return _tbl(["Agent", "Lead Gen rows", "Dials logged", "Offers logged", "Deals closed"],
                    [[r["name"], str(r["rows"]), f"{float(r['dials']):,.0f}", f"{float(r['offers']):,.0f}", str(r["closed"])] for r in rows],
                    num=(1, 2, 3, 4))
    if kind == "no_signed_date":
        rows = R.fetch(cur, """SELECT address, primary_agent, status, list_date, under_contract_date FROM cte_deals
                               WHERE file_year = %(y)s AND deal_type = 'Listing' AND signed_date IS NULL
                                 AND status NOT IN ('Cancelled', 'Sale Failed', 'Expired') ORDER BY list_date""", {"y": int(arg)})
        return _tbl(["Address", "Agent", "Status", "List date", "Under contract"],
                    [[r["address"], r["primary_agent"] or "", r["status"], _d(r["list_date"]), _d(r["under_contract_date"])] for r in rows])
    if kind == "no_split_pct":
        rows = R.fetch(cur, """SELECT close_date, address, primary_agent, gci FROM cte_deals WHERE file_year = %(y)s
                               AND status = 'Closed' AND primary_pct IS NULL AND primary_gci IS NULL ORDER BY close_date""", {"y": int(arg)})
        return _tbl(["Closed", "Address", "Agent", "GCI"], [[_d(r["close_date"]), r["address"], r["primary_agent"] or "", _m(r["gci"])] for r in rows], num=(3,))
    if kind == "missing_fields":
        rows = R.fetch(cur, """SELECT close_date, address, primary_agent, source, commission_pct, sale_price, gci FROM cte_deals
                               WHERE file_year = %(y)s AND status = 'Closed'
                                 AND (source IS NULL OR TRIM(source) = '' OR commission_pct IS NULL) ORDER BY close_date""", {"y": int(arg)})
        return _tbl(["Closed", "Address", "Agent", "Lead source", "Commission %", "GCI ÷ price"],
                    [[_d(r["close_date"]), r["address"], r["primary_agent"] or "", r["source"] or "(blank)",
                      f"{float(r['commission_pct']) * 100:.2f}%" if r["commission_pct"] is not None else "(blank)",
                      f"{float(r['gci']) / float(r['sale_price']) * 100:.2f}%" if r["sale_price"] and r["gci"] else ""] for r in rows])
    if kind == "cte_not_in_fub":
        fub_names = list(R.agent_names(cur).values())
        out = []
        for n in arg.split(","):
            best = max(fub_names, key=lambda f: SequenceMatcher(None, n.lower(), f.lower()).ratio(), default="")
            r = R.fetch(cur, """SELECT COUNT(*) FILTER (WHERE status = 'Closed') AS closed, COUNT(*) FILTER (WHERE status = 'Pending') AS pending
                                FROM cte_deals WHERE file_year = %(y)s AND LOWER(TRIM(primary_agent)) = LOWER(%(n)s)""", {"y": year, "n": n})[0]
            out.append([n, str(r["closed"]), str(r["pending"]), best])
        return _tbl(["Name in CTE", "Closed", "Pending", "Closest name in Follow Up Boss"], out, num=(1, 2))
    if kind == "compass_only":
        p = item.get("_row")
        if p:
            cur.execute("""SELECT txn_date, txn_type, name, memo, account, amount FROM qbo_income_txns
                           WHERE txn_date BETWEEN %s AND %s ORDER BY txn_date""",
                        (p["date"] - timedelta(days=14), p["date"] + timedelta(days=14)))
            near = cur.fetchall()
            rows = [["Compass", _d(p["date"]), "Paid by escrow" if p["kind"] == "escrow" else "Payment",
                     (p["property"] or "")[:60], f"${p['amount']:,.2f}"]]
            rows += [["QuickBooks", _d(r[0]), f"{r[1] or ''} · {r[4] or ''}", " ".join((r[3] or r[2] or "").split())[:60], f"${float(r[5]):,.2f}"]
                     for r in near]
            return _tbl(["", "Date", "Type", "Property / memo", "Amount"], rows, num=(4,))
    if kind == "qb_only":
        t = item.get("_row")
        if t:
            cur.execute("""SELECT COALESCE(paid_on, (received_at AT TIME ZONE 'America/Los_Angeles')::date) AS d, kind, property, total
                           FROM compass_payments WHERE total IS NOT NULL
                             AND COALESCE(paid_on, (received_at AT TIME ZONE 'America/Los_Angeles')::date) BETWEEN %s AND %s ORDER BY 1""",
                        (t["date"] - timedelta(days=21), t["date"] + timedelta(days=21)))
            near = cur.fetchall()
            rows = [["QuickBooks", _d(t["date"]), f"{t['type'] or ''} · {t['account'] or ''}",
                     " ".join((t["memo"] or t["name"] or "").split())[:60], f"${t['amount']:,.2f}"]]
            rows += [["Compass", _d(r[0]), "Paid by escrow" if r[1] == "escrow" else "Payment", (r[2] or "")[:60], f"${float(r[3]):,.2f}"]
                     for r in near]
            return _tbl(["", "Date", "Type", "Property / memo", "Amount"], rows, num=(4,))
    if kind == "unmatched_receipts":
        return _tbl(["Date", "Compass says", "Statement", "Gross", "Paid"],
                    [[_d(r.get("bill_date") or r.get("paid_on")), r.get("property") or r.get("description") or "",
                      "Escrow" if r.get("kind") == "escrow" else "Upcoming Payment", _m(r.get("gross")), _m(r.get("amount"))]
                     for r in (receipts or {}).get("unmatched_list", [])], num=(3, 4))
    return None



# ---------------------------------------------------------------- where each item is fixed

def _rows_text(rows, limit=12):
    nums = [str(r["row_num"]) for r in rows]
    return ", ".join(nums[:limit]) + (f" and {len(nums) - limit} more" if len(nums) > limit else "")


def drive_contracts_rows(cur):
    import drive_contracts
    return drive_contracts.rows(cur)


def add_where(cur, items, year, receipts):
    """Give every item a "Fix in" line (system > file/screen > sheet > row/column or field) and point its
    link at that exact spot."""
    files = {fy: sf for fy, sf in R.fetch_rows(cur, "SELECT DISTINCT ON (file_year) file_year, source_file FROM cte_deals ORDER BY file_year, source_file")} \
        if CTE.ready(cur) else {}
    cte_file = files.get(year, f"CTE {year} workbook")
    sheet = f"OneDrive › CTE FILES › {cte_file} › My Business"
    for i in items:
        kind, _, arg = i["key"].partition(":")
        w = None
        if kind == "typo":
            t = next((t for t in (receipts or {}).get("typo_list", []) if f"{t['address']}|{t['close_date']}" == arg), None)
            if t:
                f = files.get(t["file_year"], cte_file)
                if t["what"] == "close date":
                    w = (f"OneDrive › CTE FILES › {f} › My Business › {cell('close_date', t['row_num'])}: "
                         f"change {t['close_date'].strftime('%m/%d/%Y')} to {t['when'].strftime('%m/%d/%Y') if t['when'] else 'the Compass date'}")
                else:
                    num = (t["fix"].split() or [""])[0]
                    parts = t["address"].split(None, 1)
                    new = num + (" " + parts[1] if len(parts) > 1 else "")
                    w = f"OneDrive › CTE FILES › {f} › My Business › {cell('address', t['row_num'])}: change “{t['address']}” to “{new}”"
        elif kind == "both_sides":
            w = f"{sheet} › {CTE_COL['deal_type'][1]} column {CTE_COL['deal_type'][0]}, rows {_rows_text(both_sides_rows(cur, year))}: a decision, no edit yet"
        elif kind == "low_pct":
            rows = low_pct_rows(cur, year)
            w = f"{sheet} › Commission % column AK and GCI column AM, rows {_rows_text(rows)}: written approval, or correct if mistyped"
        elif kind == "no_signed_date":
            w = f"{sheet} › Signed Date column AB, rows {_rows_text(no_signed_date_rows(cur, year))}"
            i["link"] = ("cte", {"focus": "no_signed_date", "_anchor": "focus"})
        elif kind == "no_split_pct":
            w = f"{sheet} › Primary % column AT and Primary GCI column AU, rows {_rows_text(no_split_rows(cur, year))}"
            i["link"] = ("cte", {"focus": "no_split_pct", "_anchor": "focus"})
        elif kind == "missing_fields":
            w = f"{sheet} › Lead Source column AA / Commission % column AK, rows {_rows_text(missing_field_rows(cur, year))}"
            i["link"] = ("cte", {"focus": "missing_fields", "_anchor": "focus"})
        elif kind == "fell_through":
            w = f"{sheet} › Status column X = Cancelled (nothing to fix; review why they fell through)"
            i["link"] = ("business_overview", {"status": "Cancelled", "_anchor": "status-deals"})
        elif kind == "lead_gen_empty":
            w = f"OneDrive › CTE FILES › {cte_file} › Lead Gen sheet: one row per agent per day (Dials, Contacts, Written Offers, Open Houses Held…)"
        elif kind == "same_person":
            a, _, b = arg.partition("|")
            where = R.fetch(cur, """SELECT source_file, MIN(row_num) AS first, COUNT(*) AS n FROM cte_deals
                                    WHERE LOWER(TRIM(primary_agent)) = LOWER(%(n)s) GROUP BY 1 ORDER BY 1""", {"n": a})
            w = (f"OneDrive › CTE FILES › My Business › Primary Agent column AS (and the Lead Gen sheet's agent name): "
                 f"“{a}” appears in " + (", ".join(f"{x['source_file']} ({x['n']} row{'s' if x['n'] != 1 else ''}, first is row {x['first']})" for x in where) or "the Lead Gen sheet only")
                 + f". If it's the same person, decide here (the dashboard counts them together) or retype it as “{b}”.")
        elif kind == "drive_contract":
            d = next((d for d in drive_contracts_rows(cur) if d["file_id"] == arg), None)
            if d:
                w = (f"Google Drive › {d['folder']} folder › {d['file']}: section 3 split table, the cheat sheet on "
                     "the last page and the signatures (fix the file, or decide here to use what was read)")
        elif kind == "contract":
            c = next((c for c in SP.CONTRACTS if c["agent"] == arg), None)
            if c:
                w = (f"Google Drive › {c['agent']} folder › {c['file']}: "
                     + ("needs the agent's signature and date" if c["since"] is None
                        else "section 3 split table vs the Quick Reference cheat sheet on the last page"))
        elif kind == "contract_pending":
            w = f"Google Drive › {arg} folder (shared by sandiegospecialist619): add the signed agreement PDF; then tell me and I'll add his splits"
        elif kind == "no_contract":
            w = "Google Drive › each agent's folder: add the signed agreement (a PDF with “contract” or “agreement” in the name)"
        elif kind == "off_contract":
            w = "Agent Splits › Every closed deal › Off contract: each deal's Compass receipt vs the agent's agreement; fix the pay with Compass, or the agreement in Drive"
            i["link"] = ("splits_page", {"_anchor": "deals"})
        elif kind == "missing_receipts":
            y = int(arg) if arg.isdigit() else year
            w = (f"Gmail joesellssandiego@gmail.com › forward the missing Compass statements (the list shows each deal), "
                 f"or the deal's row in OneDrive › CTE FILES › {files.get(y, f'CTE {y} workbook')} › My Business if its address or date is wrong")
        elif kind == "unmatched_receipts":
            w = f"Compass Invoices › Receipts that match no closed deal: if it's a sale, add it to {sheet}"
        elif kind == "books_vs_compass":
            w = "QuickBooks › Banking › deposits vs the Compass statements: see the entry-by-entry list"
        elif kind == "qb_only":
            t = i.get("_row")
            if t:
                w = (f"QuickBooks › {t['type'] or 'Deposit'} on {t['date'].strftime('%m/%d/%Y')} for ${t['amount']:,.2f}, "
                     f"income account “{t['account'] or ''}”: say what it is, or fix the account")
                i["link"] = ("compass_invoices", {"year": year, "_anchor": f"qb-{t['id']}"})
        elif kind == "compass_only":
            p = i.get("_row")
            if p:
                w = (f"Bank of America business checking (BofA Bus Chk 9123): look for ${p['amount']:,.2f} around "
                     f"{p['date'].strftime('%m/%d/%Y')}; then QuickBooks › Banking › that deposit")
                i["link"] = ("compass_invoices", {"year": year, "_anchor": f"cp-{p['id']}"})
        elif kind == "qb_interest":
            w = "QuickBooks › Interest Earned account (nothing to fix)"
        elif kind in ("goal_gross", "goal_net"):
            w = f"Business Overview › Set {year} goals › " + ("Gross (GCI) goal" if kind == "goal_gross" else "Net income goal")
        elif kind == "gci_pace":
            w = f"Business Overview › {year} year totals (goal set under Set {year} goals)"
        elif kind == "stale_new":
            w = "Follow Up Boss › People › stage “New”, created over 48 hours ago: contact them and change the stage"
        elif kind == "held_rate":
            w = "Follow Up Boss › each appointment's outcome (Held / No show / Canceled) and the lead's stage after it"
            i["link"] = ("appointments", {"period": "last30", "view_by": "start", "status": "not_held"})
        elif kind == "source":
            w = {"QuickBooks": "Dashboard › QuickBooks P&L › Reconnect",
                 "CTE workbooks (OneDrive)": "Render › cron job dht-dashboard-1 › Logs (the daily import)",
                 "Follow Up Boss": "Render › cron job dht-dashboard-1 › Logs (the FUB pull)"}.get(arg, "Dashboard › Compass Invoices › Gmail account › Reconnect")
        elif kind == "cte_not_in_fub":
            w = "Follow Up Boss › Admin › Users (the user's name) vs the Primary Agent column AS in CTE: say here which FUB user each one is"
        elif i["key"] == "setup:open_house_leads":
            w = f"{sheet} › Lead Source column AA = “open house”: a rule for how those leads are split"
        elif i["key"] == "setup:team_past_client":
            w = f"{sheet} › Lead Source column AA = “Team Past Client”: a rule for how those leads are split"
        elif i["key"] == "setup:margaryta_agreements":
            w = "Google Drive › Margaryta Gvritishvili folder › DHT_Contract.pdf, COMMISSION_MODIFICATION_AGREEMENTdocx.pdf, INDEPENDENT CONTRACTOR AGREEMENT DHT"
            i["link"] = ("splits_page", {"_anchor": "contract-Margaryta-Gvritishvili"})
        elif i["key"] == "setup:compass_fee_wording":
            w = "Google Drive › each agent's agreement › section 3 “How commissions are calculated” (Step 2: Compass fee 10%)"
        elif i["key"] == "setup:escrow_statements":
            w = "Compass Invoices › payments list (Escrow statement rows): a decision, no edit"
        elif i["key"] == "setup:onedrive_duplicate_folder":
            w = "OneDrive (joecorbisiero@dreamhomesteam) › the “CTE” folder next to “CTE FILES”"
        elif i["key"] == "setup:onedrive_button":
            w = "Render › dht-dashboard web service › Environment: the MS_* keys"
        i["where"] = w or i.get("where")
    return items
