"""Agent splits: each agent's contract terms and a check of every closed deal against them.

The contracts are the Dream Homes Team SD independent contractor agreements (and amendments) in the
agents' Google Drive folders (read on 2026-09-30). The company share is by lead source: the agent's
personal contacts (sphere, past clients, family), Zillow, and every other team/database lead.
An agent can have several agreements over time; a deal is checked against the latest one signed on or
before its close date. Some agreements drop the company share once the agent has closed $10M in sales
with the team (career, or within the amendment's own period); the deal that crosses $10M is still on
the standard split and every later deal is on the bonus split.

What the company actually got comes from the Compass remittance statements (gmail_import):
Compass keeps 7.5% of the commission plus a ~$150 transaction fee, and pays the team its share
of the rest, so the actual company % = company commission / (GCI x 0.925 - 150).
"""
import re
from datetime import date

import gmail_import

COMPASS_KEEP = 0.075     # Compass's split, seen on every 2026 statement
COMPASS_FEE = 150.0      # flat fee shared in proportion to the split
TOLERANCE = 2.0          # points of difference before a deal is flagged
ZILLOW_FEE = (0.25, 0.45)  # Zillow referral fee range (share of the commission) treated as on contract
BONUS_VOLUME = 10_000_000  # closed sales volume that unlocks the bonus split


def _c(agent, since, personal, zillow, database, file, note="", bonus=None, bonus_from=None, keep_old=(),
       bonus_skip=(), special=None):
    """bonus: company % on every deal after the agent passes BONUS_VOLUME; bonus_from: count volume from
    this date (None = all the agent's closed deals with the team); keep_old: addresses that stay on the
    agent's previous agreement; bonus_skip: lead types the bonus doesn't change ("team generated leads stay
    as originally structured"); special: {CTE lead-source pattern: company %} for clients with their own
    rate in the agreement (e.g. prior clients disclosed before signing, immediate family)."""
    return {"agent": agent, "since": since, "personal": personal, "zillow": zillow, "database": database,
            "file": file, "note": note, "bonus": bonus, "bonus_from": bonus_from, "keep_old": keep_old,
            "bonus_skip": bonus_skip, "special": special or {}}


# Company share (%) by lead type, from each agent's signed agreement, oldest first per agent.
CONTRACTS = [
    _c("Carolyn Naples", date(2026, 1, 19), 30, 35, 50, "INDEPENDENT_CONTRACTOR_AGREEMENT__(1).pdf",
       "Older agreement: 70/30 sphere & past clients, 50/50 standard, team and ISA leads; Zillow per the Zillow Flex agreement (65/35)"),
    _c("Ahtziri Duran", date(2026, 3, 5), 20, 35, 40, "Independent Contractor Agreement Ahtziri Duran .pdf",
       "Split table says personal 80/20, but the cheat sheet and 'prior clients' say 75/25"),
    _c("Jeff Iacoviello", date(2026, 3, 6), 25, 35, 40, "Independent Contractor Agreement Jeff Iacoviello.pdf",
       "80/20 on everything after $10M career sales", bonus=20),
    _c("Sandy Osuna", date(2026, 4, 15), 20, 35, 40, "Independent_Contractor_Agreement-_Sandy_Osuna.pdf"),
    _c("Margaryta Gvritishvili", date(2025, 10, 22), 30, 50, 50, "DHT_Contract.pdf",
       "Older agreement: 70/30 sphere & past clients, 50/50 standard, team, ISA and database leads (Zillow not listed)"),
    _c("Margaryta Gvritishvili", date(2026, 2, 23), 25, 25, 25, "COMMISSION_MODIFICATION_AGREEMENTdocx.pdf",
       "12-month amendment: 75/25 on every deal (after Zillow/referral fees); 80/20 after $10M closed from 2/23/2026; "
       "6484 Belle Glade stays on the older agreement",
       bonus=20, bonus_from=date(2026, 2, 23), keep_old=("6484 belle glade",)),
    _c("Margaryta Gvritishvili", date(2026, 4, 18), 25, 35, 35, "INDEPENDENT CONTRACTOR AGREEMENT DHT",
       "TC fee 50/50; 80/20 on everything after $10M career sales", bonus=20),
    _c("Darrion Jackson", date(2026, 5, 26), 30, 40, 40, "Darrion_Jacksondocx.pdf",
       "Section 3 table 70/30 personal, 60/40 Zillow and team; prior clients disclosed before signing 80/20, "
       "immediate family 75/25 (section 7); 75/25 after $10M, team leads stay 60/40. The cheat sheet's "
       "80/65/60 and the Zillow example's 65/35 math are leftovers from the standard template",
       bonus=25, bonus_skip=("database",), special={r"prior client|disclosed": 20, r"family|relative": 25}),
    _c("Sam Foote", date(2026, 6, 25), 25, 35, 40, "DHT Independent Agent Contract Sam.docx.pdf",
       "TC fee by split; 80/20 on everything after $10M career sales", bonus=20),
    _c("Donna Karen Ray", date(2026, 7, 24), 20, 35, 40, "Independent_Contractor_Agreement- Donna Karen Ray 2026.pdf",
       "Zillow 65/35 after Zillow's referral fee; 36-month lead ownership"),
    _c("Haley Lower", date(2026, 8, 26), 25, 35, 45, "Haley Lower DHT Contract.docx.pdf",
       "80/20 on everything after $10M career sales", bonus=20),
    _c("Tristen Campanella", date(2026, 8, 28), 20, 35, 40, "Tristen_DHT_Contract_to_sign.pdf",
       "Zillow Flex and any other referring-partner leads 65/35"),
    _c("Glennis Dawson", date(2026, 9, 1), 20, 40, 45, "Glennis_Dawson_DHT_Contract.pdf"),
    _c("Gina Story", date(2026, 9, 2), 25, 40, 45, "Gina_DHT_Contract.pdf"),
    _c("Dan DeBacco", date(2026, 9, 14), 20, 35, 40, "DHT_Contract_Dan_DeBaccodocx.pdf"),
    _c("Katrina DeBacco", date(2026, 9, 14), 20, 35, 40, "DHT_Contract_Katrina_DeBaccodocx.pdf"),
    _c("Cortney Vaughan", date(2026, 9, 18), 30, 45, 50, "DHT Contract Cortney Vaughan.pdf",
       "75/25 on everything after $10M career sales", bonus=25),
    _c("Jexsi Grey", None, 25, 35, 45, "Jexsi Grey.pdf", "Not signed or dated; 36-month lead ownership"),
]
# The two readings of an agreement that contradicts itself, as offered on the dashboard (company %:
# personal, zillow, database). The owners' choice on the Needs-attention item decides which is used.
READINGS = {
    "Ahtziri Duran": {"Split table is right": (20, 35, 40), "Cheat sheet is right": (25, 35, 40)},
    "Darrion Jackson": {"Split table is right": (30, 40, 40), "Cheat sheet is right": (20, 35, 40)},
}
OWNERS = {"joe corbisiero", "maria corbisiero"}  # team owners: no agent split
NO_CONTRACT = ["Jason Patel"]  # agents with deals but no agreement in Drive
# owners' answers on the dashboard (10/01/2026)
FORMER_AGENTS = {"miguel aguirre": "No longer with the team"}
CONTRACT_PENDING = {"jason patel": "New agreement being made"}

LEAD_TYPES = [("personal", "Personal / sphere"), ("zillow", "Zillow"), ("database", "Team / database")]


# Lead sources the owners decided on Needs attention: decision key -> (source pattern, the choice meaning "personal")
LEAD_TYPE_DECISIONS = {"setup:open_house_leads": (r"open\s*house", "Agent's own (personal) lead"),
                       "setup:team_past_client": (r"team\s*past\s*client", "Agent's own (personal split)")}


def own_lead_patterns(cur):
    """Source patterns the owners said count as the agent's own leads."""
    try:
        import decisions
        made = decisions.latest(cur)
    except Exception:
        return []
    return [pat for key, (pat, own) in LEAD_TYPE_DECISIONS.items() if (made.get(key) or {}).get("choice") == own]


def lead_type(source, own=()):
    """CTE lead source -> the contract's lead type. `own`: extra source patterns that count as personal."""
    s = (source or "").lower()
    if "zillow" in s:
        return "zillow"
    if any(re.search(p, s) for p in own):
        return "personal"
    if re.search(r"sphere|personal|client referral|^past client|family|friend", s) and "team" not in s:
        return "personal"
    return "database"


AMENDMENT_RUNS = "The 2/23 amendment runs its 12 months"


def _drive_contracts(cur, rows, latest):
    """Agreements read from the agents' Drive folders (drive_contracts.py) that aren't typed in already:
    the clean ones, and the unclear ones the owners said to use as read."""
    try:
        import drive_contracts
        found = drive_contracts.rows(cur)
    except Exception:
        return []
    have = {(c["agent"].lower(), c["since"]) for c in rows}
    out = []
    for d in found:
        use = not d["problem"] or (latest.get(f"drive_contract:{d['file_id']}") or {}).get("choice") == drive_contracts.USE_AS_READ
        if not use or None in (d["personal"], d["zillow"], d["database"]) or (d["agent"].lower(), d["signed"]) in have:
            continue
        out.append(_c(d["agent"], d["signed"], d["personal"], d["zillow"], d["database"], d["file"],
                      "Read from Google Drive" + (f" ({d['problem']}; used as read)" if d["problem"] else ""),
                      bonus=d["bonus"]))
        have.add((d["agent"].lower(), d["signed"]))
    return out


def decided_contracts(cur):
    """CONTRACTS with the owners' answers on the dashboard applied: which reading of a contradicting
    agreement is right, and the signing date of an unsigned one."""
    rows = [dict(c) for c in CONTRACTS]
    try:
        import decisions
        latest = decisions.latest(cur)
        made = {k[len("contract:"):]: d for k, d in latest.items() if k.startswith("contract:")}
    except Exception:
        return rows
    rows += _drive_contracts(cur, rows, latest)
    rows.sort(key=lambda c: c["since"] or date.max)  # contract_for takes the latest one per agent
    # Margaryta's 2/23/2026 Commission Modification runs 12 months (75/25 on every deal, 80/20 after $10M
    # closed in the period): when the owners say so, her 4/18 agreement only starts after it ends
    if (latest.get("setup:margaryta_agreements") or {}).get("choice") == AMENDMENT_RUNS:
        for c in rows:
            if c["agent"] == "Margaryta Gvritishvili" and c["since"] == date(2026, 4, 18):
                c["since"] = date(2027, 2, 23)
                c["note"] = "Starts after the 2/23/2026 amendment's 12 months (owners' decision). " + c["note"]
    for c in rows:
        d = made.get(c["agent"])
        if not d or not d["choice"]:
            continue
        reading = READINGS.get(c["agent"], {}).get(d["choice"])
        if reading:
            c["personal"], c["zillow"], c["database"] = reading
            c["note"] = f"Owners' decision: {d['choice'].lower()} ({100 - reading[0]}/{reading[0]} personal). " + c["note"]
        elif c["since"] is None and d["choice"].startswith("Signed"):
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", d["comment"] or "")
            if m:
                y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
                c["since"] = date(y, int(m.group(1)), int(m.group(2)))
                c["note"] = f"Signed {c['since'].strftime('%m/%d/%Y')} per the owners. " + c["note"]
    return rows


def _agent_contracts(agent, rows=None):
    return [c for c in (rows if rows is not None else CONTRACTS) if c["agent"].lower() == (agent or "").strip().lower()]


def contract_for(agent, when, address=None, rows=None):
    """The agent's agreement in force on `when` (None if none, or the deal closed before the first one)."""
    rows = _agent_contracts(agent, rows)
    if rows and rows[-1]["since"] is None:
        return rows[-1]
    rows = [c for c in rows if when and c["since"] <= when]
    while rows and address and any(address.lower().startswith(k) for k in rows[-1]["keep_old"]):
        rows = rows[:-1]
    return rows[-1] if rows else None


def contracts_table(cur=None):
    return decided_contracts(cur) if cur is not None else [dict(c) for c in CONTRACTS]


def base(gci):
    return float(gci) * (1 - COMPASS_KEEP) - COMPASS_FEE


def milestones(cur):
    """For every agreement with a $10M bonus: the deal that took the agent past BONUS_VOLUME
    (counting closed CTE deals where the agent is primary or secondary, from bonus_from or the start)."""
    out = {}
    for c in decided_contracts(cur):
        if not c["bonus"]:
            continue
        cur.execute("""SELECT file_year, row_num, close_date, address, sale_price FROM cte_deals
                       WHERE status = 'Closed' AND close_date IS NOT NULL AND close_date >= %s
                         AND (lower(primary_agent) = lower(%s) OR lower(secondary_agent) = lower(%s))
                       ORDER BY close_date, file_year, row_num""",
                    (c["bonus_from"] or date(1900, 1, 1), c["agent"], c["agent"]))
        total, crossed = 0.0, None
        order = {}
        for i, (fy, rn, cd, addr, price) in enumerate(cur.fetchall()):
            order[(fy, rn)] = i
            total += float(price or 0)
            if crossed is None and total >= BONUS_VOLUME:
                crossed = {"index": i, "close_date": cd, "address": addr}
        out[(c["agent"], c["since"])] = {"volume": total, "crossed": crossed, "order": order}
    return out


def deal_check(cur, start, end):
    """Closed deals in [start, end) with their contract company % and the company % Compass actually paid."""
    deals, _ = gmail_import.deal_receipts(cur, start, end)
    cur.execute("""SELECT address, close_date, primary_agent, source FROM cte_deals
                   WHERE status = 'Closed' AND close_date >= %s AND close_date < %s""", (start, end))
    sources = {(a, d, g): s for a, d, g, s in cur.fetchall()}
    miles = milestones(cur)
    rows_c = decided_contracts(cur)
    own = own_lead_patterns(cur)
    out = []
    for d in deals:
        agent = d["agent"] or ""
        src = sources.get((d["address"], d["close_date"], d["agent"]))
        row = {**d, "source": src, "lead": lead_type(src, own), "owner": agent.lower() in OWNERS,
               "contract": None, "contract_file": None, "bonus": False, "expected_pct": None, "actual_pct": None,
               "company": None, "expected": None, "gap": None, "status": "no_receipt"}
        company = sum(float(r["gross"] or r["amount"] or 0) for r in d["receipts"]) if d["receipts"] else None
        row["company"] = company
        c = None if row["owner"] else contract_for(agent, d["close_date"], d["address"], rows_c)
        if c:
            row["contract"], row["contract_file"] = c["agent"], c["file"]
            row["expected_pct"] = c[row["lead"]]
            for pat, pct_ in (c.get("special") or {}).items():  # clients with their own rate in the agreement
                if row["lead"] == "personal" and re.search(pat, src or "", re.I):
                    row["expected_pct"] = pct_
                    break
            m = miles.get((c["agent"], c["since"]))
            if m and m["crossed"] and m["order"].get((d["file_year"], d["row_num"]), -1) > m["crossed"]["index"]                     and row["lead"] not in c.get("bonus_skip", ()):
                row["bonus"], row["expected_pct"] = True, min(c["bonus"], row["expected_pct"])
        if company is not None and d["gci"] and base(d["gci"]) > 0:
            row["actual_pct"] = company / base(d["gci"]) * 100
        if row["owner"]:
            row["status"] = "owner"
        elif company is None:
            row["status"] = "no_receipt"
        elif not c:
            key = agent.strip().lower()
            row["status"] = ("before_contract" if _agent_contracts(agent, rows_c)
                             else "former_agent" if key in FORMER_AGENTS
                             else "contract_pending" if key in CONTRACT_PENDING else "no_contract")
        else:
            row["expected"] = row["expected_pct"] / 100 * base(d["gci"])
            row["gap"] = company - row["expected"]
            diff = row["actual_pct"] - row["expected_pct"]
            row["status"] = "ok" if abs(diff) <= TOLERANCE else ("under" if diff < 0 else "over")
            # Zillow's / Realtor.com's referral fee (usually 35-40% of the commission) comes off the top before
            # the split, so a deal paid at the contract % of what was left is on contract
            if (row["lead"] == "zillow" or "realtor.com" in (src or "").lower()) and row["status"] == "under":
                fee = 1 - row["actual_pct"] / row["expected_pct"]
                if ZILLOW_FEE[0] <= fee <= ZILLOW_FEE[1]:
                    row.update(status="ok", zillow_fee=fee * 100, expected=company, gap=0.0)
            # every agreement says "The Company decides which category a lead falls into": a deal paid at
            # another of the agent's own rates was classified that way by the company (before the $10M bonus)
            if row["status"] != "ok" and not row["bonus"]:
                for kind in ("personal", "zillow", "database"):
                    if kind != row["lead"] and abs(row["actual_pct"] - c[kind]) <= TOLERANCE:
                        row.update(status="ok", paid_as=kind, expected=company, gap=0.0)
                        break
        out.append(row)
    return out
