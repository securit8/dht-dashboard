"""Agent splits: each agent's contract terms and a check of every closed deal against them.

The contracts are the Dream Homes Team SD independent contractor agreements in the agents'
Google Drive folders (read on 2026-09-30). The company share is by lead source: the agent's
personal contacts (sphere, past clients, family), Zillow, and every other team/database lead.

What the company actually got comes from the Compass remittance statements (gmail_import):
Compass keeps 7.5% of the commission plus a ~$150 transaction fee, and pays the team its share
of the rest, so the actual company % = company commission / (GCI x 0.925 - 150).
"""
import re
from datetime import date, timedelta

import gmail_import

COMPASS_KEEP = 0.075     # Compass's split, seen on every 2026 statement
COMPASS_FEE = 150.0      # flat fee shared in proportion to the split
TOLERANCE = 2.0          # points of difference before a deal is flagged

# Company share (%) by lead type, from each agent's signed agreement.
# (agent as named in CTE, effective date, personal, zillow, database, file, note)
CONTRACTS = [
    ("Carolyn Naples", date(2026, 1, 19), 30, 35, 50, "INDEPENDENT_CONTRACTOR_AGREEMENT__(1).pdf",
     "Older agreement: 70/30 sphere & past clients, 50/50 standard, team and ISA leads; Zillow per the Zillow Flex agreement (65/35)"),
    ("Ahtziri Duran", date(2026, 3, 5), 20, 35, 40, "Independent Contractor Agreement Ahtziri Duran .pdf",
     "Split table says personal 80/20, but the cheat sheet and 'prior clients' say 75/25"),
    ("Jeff Iacoviello", date(2026, 3, 6), 25, 35, 40, "Independent Contractor Agreement Jeff Iacoviello.pdf",
     "80/20 on everything after $10M career sales"),
    ("Sandy Osuna", date(2026, 4, 15), 20, 35, 40, "Independent_Contractor_Agreement-_Sandy_Osuna.pdf", ""),
    ("Margaryta Gvritishvili", date(2026, 4, 18), 25, 35, 35, "INDEPENDENT CONTRACTOR AGREEMENT DHT",
     "TC fee 50/50; 80/20 on everything after $10M career sales"),
    ("Darrion Jackson", date(2026, 5, 26), 30, 40, 40, "Darrion_Jacksondocx.pdf",
     "Split table says 70/60/60, but the cheat sheet says 80/65/60"),
    ("Sam Foote", date(2026, 6, 25), 25, 35, 40, "DHT Independent Agent Contract Sam.docx.pdf",
     "TC fee by split; 80/20 on everything after $10M career sales"),
    ("Donna Karen Ray", date(2026, 7, 24), 20, 35, 40, "Independent_Contractor_Agreement- Donna Karen Ray 2026.pdf",
     "Zillow 65/35 after Zillow's referral fee; 36-month lead ownership"),
    ("Haley Lower", date(2026, 8, 26), 25, 35, 45, "Haley Lower DHT Contract.docx.pdf",
     "80/20 on everything after $10M career sales"),
    ("Tristen Campanella", date(2026, 8, 28), 20, 35, 40, "Tristen_DHT_Contract_to_sign.pdf",
     "Zillow Flex and any other referring-partner leads 65/35"),
    ("Glennis Dawson", date(2026, 9, 1), 20, 40, 45, "Glennis_Dawson_DHT_Contract.pdf", ""),
    ("Gina Story", date(2026, 9, 2), 25, 40, 45, "Gina_DHT_Contract.pdf", ""),
    ("Dan DeBacco", date(2026, 9, 14), 20, 35, 40, "DHT_Contract_Dan_DeBaccodocx.pdf", ""),
    ("Katrina DeBacco", date(2026, 9, 14), 20, 35, 40, "DHT_Contract_Katrina_DeBaccodocx.pdf", ""),
    ("Cortney Vaughan", date(2026, 9, 18), 30, 45, 50, "DHT Contract Cortney Vaughan.pdf",
     "75/25 on everything after $10M career sales"),
    ("Jexsi Grey", None, 25, 35, 45, "Jexsi Grey.pdf", "Not signed or dated; 36-month lead ownership"),
]
OWNERS = {"joe corbisiero", "maria corbisiero"}  # team owners: no agent split
NO_CONTRACT = ["Jason Patel", "Miguel Aguirre"]  # agents with deals but no agreement in Drive

LEAD_TYPES = [("personal", "Personal / sphere"), ("zillow", "Zillow"), ("database", "Team / database")]


def lead_type(source):
    """CTE lead source -> the contract's lead type."""
    s = (source or "").lower()
    if "zillow" in s:
        return "zillow"
    if re.search(r"sphere|personal|client referral|^past client|family|friend", s) and "team" not in s:
        return "personal"
    return "database"


def contract_for(agent, when):
    """The agent's agreement in force on `when` (None if none, or the deal closed before it)."""
    rows = [c for c in CONTRACTS if c[0].lower() == (agent or "").strip().lower()]
    if not rows:
        return None
    c = rows[-1]
    return c if c[1] is None or (when and when >= c[1]) else None


def contracts_table():
    return [{"agent": a, "since": d, "personal": p, "zillow": z, "database": db, "file": f, "note": n}
            for a, d, p, z, db, f, n in CONTRACTS]


def base(gci):
    return float(gci) * (1 - COMPASS_KEEP) - COMPASS_FEE


def deal_check(cur, start, end):
    """Closed deals in [start, end) with their contract company % and the company % Compass actually paid."""
    deals, _ = gmail_import.deal_receipts(cur, start, end)
    cur.execute("""SELECT address, close_date, primary_agent, source FROM cte_deals
                   WHERE status = 'Closed' AND close_date >= %s AND close_date < %s""", (start, end))
    sources = {(a, d, g): s for a, d, g, s in cur.fetchall()}
    out = []
    for d in deals:
        agent = d["agent"] or ""
        src = sources.get((d["address"], d["close_date"], d["agent"]))
        row = {**d, "source": src, "lead": lead_type(src), "owner": agent.lower() in OWNERS,
               "contract": None, "expected_pct": None, "actual_pct": None, "company": None,
               "expected": None, "gap": None, "status": "no_receipt"}
        company = sum(float(r["gross"] or r["amount"] or 0) for r in d["receipts"]) if d["receipts"] else None
        row["company"] = company
        c = None if row["owner"] else contract_for(agent, d["close_date"])
        if c:
            row["contract"] = c[0]
            row["expected_pct"] = {"personal": c[2], "zillow": c[3], "database": c[4]}[row["lead"]]
        if company is not None and d["gci"] and base(d["gci"]) > 0:
            row["actual_pct"] = company / base(d["gci"]) * 100
        if row["owner"]:
            row["status"] = "owner"
        elif company is None:
            row["status"] = "no_receipt"
        elif not c:
            has_any = any(x[0].lower() == agent.lower() for x in CONTRACTS)
            row["status"] = "before_contract" if has_any else "no_contract"
        else:
            row["expected"] = row["expected_pct"] / 100 * base(d["gci"])
            row["gap"] = company - row["expected"]
            diff = row["actual_pct"] - row["expected_pct"]
            row["status"] = "ok" if abs(diff) <= TOLERANCE else ("under" if diff < 0 else "over")
        out.append(row)
    return out
