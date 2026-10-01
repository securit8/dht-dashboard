"""Dashboard home: one screen that pulls the headline numbers out of every source the dashboard reads
(Follow Up Boss, the CTE workbooks, QuickBooks, the Compass statements in Gmail, the agent contracts)
and lists what needs someone's attention, each linking to the report with the detail.

Every section is read on its own, so one source being down or not connected yet doesn't break the page.
"""
import logging
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta

import cte_reports as CTE
import decisions
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
            when = (s.get("bill_date") or s.get("paid_on"))
            typos.append({"address": d["address"], "close_date": d["close_date"], "what": what, "fix": fix,
                          "when": when, "anchor": f"deal-{d['file_year']}-{d['row_num']}", "agent": d["agent"]})
    return {"total": len(deals), "missing": len(missing), "typos": len(typos), "typo_list": typos,
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


FOCUS = {"both_sides": ("Sales where we had both sides", both_sides_rows),
         "low_pct": ("Agent deals under the 2% minimum commission", low_pct_rows)}


def focus_deals(cur, kind, year):
    """(title, deal rows) for the CTE page when it's opened from a Needs-attention item."""
    if kind not in FOCUS:
        return None
    title, fn = FOCUS[kind]
    return {"kind": kind, "title": title, "rows": fn(cur, year)}


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
        fub = {(CTE.name_for(cur, n) or "").lower() for n in R.agent_names(cur).values()}
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
    for c in SP.CONTRACTS:
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
                              "link": ("compass_invoices", {"year": y, "_anchor": "monthly"}),
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
                          "link": ("compass_invoices", {"_anchor": "monthly"})})
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
    questions += _safe(cur, "data checks", lambda: _data_checks(cur, year, receipts), []) if has_cte else []
    questions += _standing(_safe(cur, "decisions", lambda: decisions.latest(cur), {}) or {})
    company = None
    if checks:
        paid = [r["company"] for r in checks if r["company"] is not None]
        company = {"total": sum(paid), "deals": len(paid)}
    return {"year": year, "money": money, "deals": deals, "leads": leads, "agents": agents, "company": company,
            "receipts": receipts, "fresh": fresh,
            "attention": _with_decisions(cur, _attention(money, deals, leads, receipts, checks, fresh, now, questions))}


def _with_decisions(cur, items):
    """Attach the latest owner decision (dropdown choice + comment) to each item."""
    latest = _safe(cur, "decisions", lambda: decisions.latest(cur), {}) or {}
    for i in items:
        i["decision"] = latest.get(i["key"])
    return items
