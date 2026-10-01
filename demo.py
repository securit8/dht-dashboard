"""Demo mode: the same app, filled with random numbers, for people to try without signing in.

Runs only as its own separate service (demo_wsgi.py) with its own throwaway database and no keys: it can't
reach Follow Up Boss, the CTE files, QuickBooks, Gmail or the real database. In demo mode:
- nobody needs to sign in, and every form that changes something is turned off;
- the agreements, names and account details written into the code are swapped for made-up ones;
- every page is checked on the way out for the real names and accounts, as a last safety net.
"""
import random
import uuid
from datetime import date, datetime, timedelta, timezone

import cte_import
import fub_agent_activity_pull as fub
import gmail_import
import qbo

COMPANY = "Demo Realty Group"
DEMO_EMAIL = "demo@example.com"
OWNERS = [(201, "Alex Rivera"), (202, "Jordan Rivera")]
AGENTS = [(203, "Casey Brooks"), (204, "Morgan Lee"), (205, "Taylor Quinn"), (206, "Riley Park"),
          (207, "Sam Ortiz"), (208, "Devon Hayes"), (209, "Jamie Fox")]
WEIGHT = {201: 9, 202: 8, 203: 10, 204: 6, 205: 4, 206: 3, 207: 3, 208: 2, 209: 2}
STREETS = ["Ocean View Dr", "Palm Ave", "Sunset Blvd", "Harbor Way", "Canyon Rd", "Mesa Ct", "Bayside Ln", "Cedar St",
           "Vista Del Mar", "Pacific Hwy", "Juniper St", "Del Rio Pl", "Coronado Ave", "Torrey Pines Rd", "Laurel St"]
CTE_SOURCES = ["Zillow.com", "Sphere", "open house", "Realtor.com", "Client Referral", "PAST CLIENTS", "Sphere Referral", "Google PPC"]
FUB_SOURCES = ["Zillow", "Realtor.com", "Open House", "Website", "Referral", "Homes.com", "Google PPC"]
STAGES = ["Lead", "Attempted contact", "Spoke with customer", "Appointment set", "Met with customer", "Showing homes",
          "Submitting offers", "Under contract", "Closed", "Nurture", "Trash"]
STAGE_W = [8, 28, 6, 3, 3, 3, 2, 1, 2, 22, 22]
FIRST = ["Avery", "Blake", "Cameron", "Dana", "Elliot", "Frankie", "Harper", "Jules", "Kendall", "Logan", "Micah", "Noel",
         "Parker", "Reese", "Shay", "Toby"]
LAST = ["Adams", "Bennett", "Carter", "Diaz", "Ellis", "Flores", "Grant", "Hughes", "Ivers", "James", "Kim", "Lopez",
        "Moore", "Nash", "Owens", "Patel"]
EXPENSES = [("Advertising & Marketing", ["Zillow Premier", "Facebook", "Google Ads", "Postcard Co"], 12000),
            ("Salaries & Wages", ["Payroll Service"], 11000), ("Office Supplies & Software", ["CRM Software", "Adobe", "Canva", "DocuSign"], 3200),
            ("Rent & Lease", ["Office Landlord"], 2400), ("Commission & Fees", ["Referral Partner"], 2600),
            ("Legal & Professional Services", ["CPA Firm", "Law Office"], 1600), ("Meals & Entertainment", ["Cafe", "Restaurant"], 900),
            ("Car & Truck", ["Gas Station", "Car Wash"], 700), ("Insurance", ["E&O Insurer"], 650), ("Utilities", ["Phone Co", "Internet Co"], 450)]
# company share (%) by lead type for each demo agent's agreement
CONTRACT_PCT = {"Casey Brooks": (25, 35, 40), "Morgan Lee": (20, 35, 40), "Taylor Quinn": (25, 35, 45), "Riley Park": (20, 35, 40),
                "Sam Ortiz": (30, 40, 50), "Devon Hayes": (20, 35, 40), "Jamie Fox": (25, 40, 45)}


def _lead_type(src):
    s = src.lower()
    return "zillow" if "zillow" in s else "personal" if any(w in s for w in ("sphere", "client", "past")) else "database"


def _pick(r, items, weights):
    return r.choices(items, weights=weights, k=1)[0]


def seed(cur, today=None):
    """Replace everything with fresh random data ending today (about 2.5 years of history)."""
    today = today or date.today()
    r = random.Random(today.toordinal())
    tz = timezone(timedelta(hours=-7))
    for ensure in (fub.ensure_tables, cte_import.ensure_tables, qbo.ensure_tables, gmail_import.ensure_tables):
        ensure(cur)
    cur.execute("CREATE TABLE IF NOT EXISTS business_goals (year INT PRIMARY KEY, gross_goal NUMERIC, net_goal NUMERIC)")
    for t in ("agent_events", "people", "people_stage_history", "action_plan_people", "agents", "appointments", "deals",
              "pull_state", "cte_activity", "cte_deals", "cte_financials", "cte_import_log", "qbo_connection", "qbo_pnl",
              "qbo_txns", "qbo_income_txns", "compass_payment_items", "compass_payments", "gmail_accounts", "business_goals"):
        cur.execute(f"DELETE FROM {t}")
    cur.execute("SELECT to_regclass('owner_decisions') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("DELETE FROM owner_decisions")

    people_all = OWNERS + AGENTS
    names = dict(people_all)
    ids = [u for u, _ in people_all]
    for uid, name in people_all:
        cur.execute("INSERT INTO agents (user_id, name, email, role, status) VALUES (%s, %s, %s, %s, 'active')",
                    (uid, name, f"{name.split()[0].lower()}@example.com", "Owner" if uid in (201, 202) else "Agent"))

    # ---------------- CTE deal log: three years, growing each year
    first_year = today.year - 2
    company_by_month = {}
    deals = []
    for y in range(first_year, today.year + 1):
        row = 6
        per_month = 4 + (y - first_year) * 2
        last_month = today.month if y == today.year else 12
        for m in range(1, last_month + 1):
            for _ in range(max(1, int(r.gauss(per_month, 1.6)))):
                day = r.randint(1, 28)
                close = date(y, m, day)
                if close > today:
                    continue
                uid = _pick(r, ids, [WEIGHT[i] for i in ids])
                kind = "Listing" if r.random() < 0.32 else "Buyer"
                price = round(r.uniform(380, 2600)) * 1000
                pct = r.choice([0.02, 0.025, 0.025, 0.025, 0.0275, 0.03])
                src = _pick(r, CTE_SOURCES, [8, 7, 4, 3, 3, 2, 2, 1])
                uc = close - timedelta(days=r.randint(25, 48))
                status = "Closed"
                if r.random() < 0.13:
                    status = "Cancelled"
                deals.append(dict(file_year=y, row_num=row, status=status, status_raw=status, deal_type=kind,
                                  address=f"{r.randint(100, 9999)} {r.choice(STREETS)}", clients=f"{r.choice(FIRST)} {r.choice(LAST)}",
                                  source=src, signed_date=uc - timedelta(days=r.randint(10, 60)) if kind == "Listing" else None,
                                  list_date=uc - timedelta(days=r.randint(5, 50)) if kind == "Listing" else None,
                                  under_contract_date=uc, proj_close_date=close, close_date=close if status == "Closed" else None,
                                  sale_price=price, commission_pct=pct, gci=round(price * pct, 2), primary_agent=names[uid],
                                  primary_pct=None, primary_gci=None))
                row += 1
        if y == today.year:  # pipeline: pending and active
            for _ in range(10):
                uid = _pick(r, ids, [WEIGHT[i] for i in ids])
                price = round(r.uniform(400, 2200)) * 1000
                pct = r.choice([0.025, 0.025, 0.03])
                uc = today - timedelta(days=r.randint(3, 35))
                deals.append(dict(file_year=y, row_num=row, status="Pending", status_raw="Pending", deal_type=r.choice(["Buyer", "Listing"]),
                                  address=f"{r.randint(100, 9999)} {r.choice(STREETS)}", clients=f"{r.choice(FIRST)} {r.choice(LAST)}",
                                  source=r.choice(CTE_SOURCES), signed_date=None, list_date=None, under_contract_date=uc,
                                  proj_close_date=uc + timedelta(days=r.randint(28, 45)), close_date=None, sale_price=price,
                                  commission_pct=pct, gci=round(price * pct, 2), primary_agent=names[uid], primary_pct=None, primary_gci=None))
                row += 1
            for st in ("Active", "Active", "Active", "Coming Soon", "Signed"):
                uid = _pick(r, ids, [WEIGHT[i] for i in ids])
                price = round(r.uniform(500, 1800)) * 1000
                deals.append(dict(file_year=y, row_num=row, status=st, status_raw=st, deal_type="Listing",
                                  address=f"{r.randint(100, 9999)} {r.choice(STREETS)}", clients=f"{r.choice(FIRST)} {r.choice(LAST)}",
                                  source=r.choice(CTE_SOURCES), signed_date=today - timedelta(days=r.randint(5, 60)),
                                  list_date=today - timedelta(days=r.randint(1, 40)), under_contract_date=None, proj_close_date=None,
                                  close_date=None, sale_price=price, commission_pct=0.025, gci=round(price * 0.025, 2),
                                  primary_agent=names[uid], primary_pct=None, primary_gci=None))
                row += 1
    for d in deals:
        cur.execute("""INSERT INTO cte_deals (source_file, file_year, row_num, status, status_raw, deal_type, address, clients, source,
                           signed_date, list_date, under_contract_date, proj_close_date, close_date, sale_price, commission_pct, gci,
                           primary_agent, primary_pct, primary_gci, raw)
                       VALUES (%(f)s, %(file_year)s, %(row_num)s, %(status)s, %(status_raw)s, %(deal_type)s, %(address)s, %(clients)s,
                           %(source)s, %(signed_date)s, %(list_date)s, %(under_contract_date)s, %(proj_close_date)s, %(close_date)s,
                           %(sale_price)s, %(commission_pct)s, %(gci)s, %(primary_agent)s, %(primary_pct)s, %(primary_gci)s, '{}')""",
                    dict(d, f=f"CTE {d['file_year']} Year.xlsx"))
    for y in range(first_year, today.year + 1):
        n = sum(1 for d in deals if d["file_year"] == y)
        cur.execute("INSERT INTO cte_import_log (source_file, file_year, imported_at, activity_rows, deal_rows) VALUES (%s, %s, now(), 0, %s)",
                    (f"CTE {y} Year.xlsx", y, n))
        for m in range(1, 13):
            gci = sum(d["gci"] for d in deals if d["status"] == "Closed" and d["close_date"].year == y and d["close_date"].month == m)
            pend = sum(d["gci"] for d in deals if d["status"] == "Pending" and d["file_year"] == y and d["proj_close_date"].month == m)
            for rn, label, amt in ((10, "Total Income", gci), (11, "Pending to Closed Income", pend), (30, "Net Profit", gci * 0.17)):
                cur.execute("""INSERT INTO cte_financials (source_file, file_year, row_num, section, label, month, amount)
                               VALUES (%s, %s, %s, 'Income', %s, %s, %s)""", (f"CTE {y} Year.xlsx", y, rn, label, m, round(amt, 2)))
    # a little Lead Gen activity (like the real sheet: mostly empty)
    rn = 6
    for _ in range(40):
        uid = r.choice(ids)
        d = today - timedelta(days=r.randint(1, 250))
        cur.execute("""INSERT INTO cte_activity (source_file, file_year, row_num, activity_date, agent_name, dials, contacts, open_houses)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (f"CTE {d.year} Year.xlsx", d.year, rn, d, names[uid], r.randint(0, 40), r.randint(0, 10), r.choice([0, 0, 1])))
        rn += 1
    last_gross = sum(d["gci"] for d in deals if d["status"] == "Closed" and d["close_date"].year == today.year - 1)
    cur.execute("INSERT INTO business_goals (year, gross_goal, net_goal) VALUES (%s, %s, %s)",
                (today.year, round(last_gross * 1.3, -4), 450000))

    # ---------------- Compass statements and QuickBooks: the company's share of every closing
    statement_from = date(today.year - 1, 8, 1)
    cur.execute("""INSERT INTO gmail_accounts (email, connected_at, last_pull_at, needs_reconnect) VALUES (%s, now(), now(), false)""",
                (DEMO_EMAIL,))
    cur.execute("""INSERT INTO qbo_connection (id, env, realm_id, company_name, connected_at, last_pull_at, needs_reconnect)
                   VALUES (1, 'production', 'demo', %s, now() - interval '60 days', now(), false)""", (COMPANY,))
    income_month, ytd = {}, {}
    closed = sorted((d for d in deals if d["status"] == "Closed"), key=lambda d: d["close_date"])
    skip = set(r.sample(range(len(closed)), 2)) if len(closed) > 4 else set()
    for i, d in enumerate(closed):
        base = d["gci"] * 0.925 - 150
        name = d["primary_agent"]
        if name in dict((n, u) for u, n in OWNERS):
            pct = 1.0
        else:
            pers, zil, db = CONTRACT_PCT[name]
            pct = {"personal": pers, "zillow": zil, "database": db}[_lead_type(d["source"])] / 100
            if _lead_type(d["source"]) == "zillow":
                pct *= 0.65  # Zillow's referral fee comes off the top
            if r.random() < 0.08:
                pct *= 0.8  # a few deals paid under the agreement
        company = round(base * pct, 2)
        paid = d["close_date"] + timedelta(days=r.randint(1, 6))
        if paid > today:
            continue
        key = (paid.year, paid.month)
        income_month[key] = income_month.get(key, 0) + company
        ytd[paid.year] = ytd.get(paid.year, 0) + company
        if d["close_date"] >= statement_from and i not in skip:
            mid = uuid.uuid4().hex[:16]
            kind = r.choice(["escrow", "escrow", "payment"])
            cur.execute("""INSERT INTO compass_payments (message_id, account, payment_no, paid_on, received_at, total, subject, kind, property, ytd_income)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (mid, DEMO_EMAIL, str(r.randint(100000, 999999)), paid, datetime.combine(paid, datetime.min.time(), tz), company,
                         "Agent Remittance Paid by Escrow" if kind == "escrow" else "Upcoming Payment Compass Agent Remittance",
                         kind, f"{d['address']}, San Diego, CA", round(ytd[paid.year], 2)))
            cur.execute("""INSERT INTO compass_payment_items (message_id, line_no, bill_no, description, amount, is_assist, source, bill_date, close_price, gross)
                           VALUES (%s, 1, %s, %s, %s, false, 'pdf', %s, %s, %s)""",
                        (mid, str(r.randint(1000000, 9999999)), f"{d['address']} San Diego, CA", round(company - 100, 2), d["close_date"],
                         d["sale_price"], company))
        cur.execute("""INSERT INTO qbo_txns (section, category, account, txn_type, txn_date, name, memo, amount)
                       VALUES ('Income', 'Sales', 'Sales', 'Deposit', %s, %s, %s, %s)""",
                    (paid, "Compass" if r.random() < 0.6 else "Escrow Company", f"DEPOSIT {d['address']}", company))
        cur.execute("""INSERT INTO qbo_income_txns (txn_type, txn_date, name, memo, account, amount)
                       VALUES ('Deposit', %s, %s, %s, 'Sales', %s)""", (paid, "Compass", f"DEPOSIT {d['address']}", company))
    # two deposits that aren't from Compass (to show the matching)
    for k in range(2):
        when = today - timedelta(days=r.randint(60, 200))
        amt = round(r.uniform(1500, 6000), 2)
        cur.execute("""INSERT INTO qbo_txns (section, category, account, txn_type, txn_date, name, memo, amount)
                       VALUES ('Income', 'Sales', 'Sales', 'Deposit', %s, NULL, 'MOBILE DEPOSIT', %s)""", (when, amt))
        cur.execute("INSERT INTO qbo_income_txns (txn_type, txn_date, memo, account, amount) VALUES ('Deposit', %s, 'MOBILE DEPOSIT', 'Sales', %s)", (when, amt))
        income_month[(when.year, when.month)] = income_month.get((when.year, when.month), 0) + amt
    # expenses by category and payee, growing over time
    m0 = date(first_year, 1, 1)
    while m0 <= today:
        growth = 1 + (m0.year - first_year) * 0.18
        exp = 0.0
        for cat, payees, avg in EXPENSES:
            for _ in range(r.randint(1, 3)):
                amt = round(r.uniform(0.25, 0.7) * avg * growth, 2)
                day = min(r.randint(1, 28), today.day if (m0.year, m0.month) == (today.year, today.month) else 28)
                cur.execute("""INSERT INTO qbo_txns (section, category, account, txn_type, txn_date, name, memo, amount)
                               VALUES ('Expenses', %s, %s, 'Expense', %s, %s, %s, %s)""",
                            (cat, cat, date(m0.year, m0.month, day), r.choice(payees), f"{r.choice(payees).upper()} PAYMENT", amt))
                exp += amt
        inc = round(income_month.get((m0.year, m0.month), 0), 2)
        cur.execute("""INSERT INTO qbo_pnl (month, income, cogs, gross_profit, expenses, net_operating_income, other_income, other_expenses, net_income)
                       VALUES (%s, %s, 0, %s, %s, %s, 0, 0, %s)""", (m0, inc, inc, round(exp, 2), round(inc - exp, 2), round(inc - exp, 2)))
        m0 = (m0.replace(day=28) + timedelta(days=4)).replace(day=1)

    # ---------------- Follow Up Boss: leads, activity, appointments
    now = datetime.now(timezone.utc)
    pid = 5000
    people = []
    for _ in range(1400):
        created = now - timedelta(days=r.randint(0, 900), hours=r.randint(0, 23))
        uid = _pick(r, ids, [WEIGHT[i] for i in ids])
        stage = _pick(r, STAGES, STAGE_W)
        people.append((pid, created, uid, stage))
        cur.execute("""INSERT INTO people (person_id, created_at, updated_at, stage, source, assigned_user_id, agent_name, name)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (pid, created, created + timedelta(days=r.randint(0, 30)), stage, r.choice(FUB_SOURCES), uid, names[uid],
                     f"{r.choice(FIRST)} {r.choice(LAST)}"))
        pid += 1
    fid = 1
    for p, created, uid, stage in people:
        for _ in range(r.randint(0, 6)):
            when = created + timedelta(days=r.randint(0, 40), hours=r.randint(8, 19))
            if when > now:
                continue
            ev = _pick(r, ["attempt", "conversation", "text", "email"], [5, 2, 4, 3])
            cur.execute("""INSERT INTO agent_events (fub_id, event_type, user_id, agent_name, created_at, duration_min, person_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""", (fid, ev, uid, names[uid], when, r.randint(1, 18) if ev == "conversation" else 0, p))
            fid += 1
    aid = 1
    met = ("Met with customer", "Showing homes", "Submitting offers", "Under contract", "Closed")
    for p, created, uid, stage in r.sample(people, 420):
        start = created + timedelta(days=r.randint(1, 20), hours=r.randint(9, 17))
        if start > now + timedelta(days=14):
            continue
        outcome = "" if start > now else ("Met" if stage in met else r.choice(["No show", "Canceled", "", "Held"]))
        cur.execute("""INSERT INTO appointments (appt_id, created_at, start_at, title, type, outcome, created_by_id, created_by_name,
                           person_id, lead_name, agent_ids, agent_names)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (aid, start - timedelta(days=r.randint(1, 6)), start, "Consultation", r.choice(["Buyer consultation", "Listing appointment", "Showing"]),
                     outcome, uid, names[uid], p, "Lead", [uid], names[uid]))
        cur.execute(f"INSERT INTO agent_events (fub_id, event_type, user_id, agent_name, created_at, person_id) VALUES (%s, 'appt', %s, %s, %s, %s)",
                    (fid, uid, names[uid], start - timedelta(days=1), p))
        fid += 1
        aid += 1
    cur.execute("INSERT INTO pull_state (id, last_pulled_at) VALUES (1, now())")


# ---------------------------------------------------------------- safety: no real names or accounts in the demo

def apply(app):
    """Swap the real agreements, names and accounts written into the code for made-up ones, and check every page
    on the way out for anything real."""
    import cte_reports
    import goals
    import home
    import splits
    real = set()
    for c in splits.CONTRACTS:
        real.add(c["agent"])
    real.update(splits.NO_CONTRACT)
    real.update(n.title() for n in list(splits.OWNERS) + list(splits.FORMER_AGENTS) + list(splits.CONTRACT_PENDING))
    real.update(n.title() for n in cte_reports.FUB_TO_CTE)
    real.update(["Margarita", "Margaryta", "Gvritishvili", "Corbisiero", "Ahtziri", "Iacoviello", "DeBacco", "Naples"])
    secrets = ["joesellssandiego@gmail.com", "sandiegospecialist619", "joecorbisiero@dreamhomesteam.onmicrosoft.com",
               "dreamhomesteam", "BofA Bus Chk 9123", "joe@dhtsandiego.com"]

    splits.CONTRACTS = [splits._c(n, date(date.today().year - 2, 1, 1), *pct, f"{n} agreement.pdf", "Demo agreement")
                        for n, pct in CONTRACT_PCT.items()]
    splits.OWNERS = {n.lower() for _, n in OWNERS}
    splits.NO_CONTRACT, splits.FORMER_AGENTS, splits.CONTRACT_PENDING, splits.READINGS = [], {}, {}, {}
    cte_reports.FUB_TO_CTE = {}
    home.STANDING = [
        ("setup:open_house_leads", "Open-house leads: team lead or the agent's own?",
         "Decides which split applies to deals from open houses.", ("splits_page", {"_anchor": "deals"}),
         ["Team / database lead", "Agent's own (personal) lead"]),
        ("setup:escrow_statements", "Keep the escrow statements in the totals?",
         "Escrow-paid statements are counted with the regular payments.", ("compass_invoices", {}), ["Keep them", "Remove them"]),
    ]
    goals.MONTHLY = [(k, "Owner" if label == "Joe" else label, v, hb) for k, label, v, hb in goals.MONTHLY]

    replace = sorted(real, key=len, reverse=True)

    @app.after_request
    def _scrub(resp):
        if resp.mimetype in ("text/html", "application/json") and not resp.direct_passthrough:
            body = resp.get_data(as_text=True)
            for s in secrets:
                body = body.replace(s, "demo")
            for n in replace:
                if n and n in body:
                    body = body.replace(n, "Demo Agent")
            resp.set_data(body)
        return resp
