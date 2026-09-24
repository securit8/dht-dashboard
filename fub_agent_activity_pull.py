"""
Follow Up Boss event pull script — Render Cron Job version.

Pulls appointments, calls, and (bounded, per recently-updated person)
texts and emails, writing one row per individual event into Postgres.
Also now pulls new leads (/v1/people) and tags events with person_id
so the dashboard can build a Sales Funnel (distinct leads per stage),
not just raw activity counts.

Uses cursor-based pagination throughout — no offset depth limit.
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

import cte_import

API_BASE = "https://api.followupboss.com/v1"
PAGE_SIZE = 100
OVERLAP_MINUTES = 15
MIN_REQUEST_INTERVAL = 0.75  # seconds between requests — keeps us safely under FUB's 1,000/10min cap
_last_request_time = 0.0


def get_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.auth = (api_key, "")
    session.headers.update({"Accept": "application/json"})
    return session


def paginate(session, endpoint, params=None):
    global _last_request_time
    params = dict(params or {})
    params.setdefault("limit", PAGE_SIZE)
    next_link = None
    while True:
        elapsed = time.time() - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)

        resp = session.get(next_link) if next_link else session.get(f"{API_BASE}/{endpoint}", params=params)
        _last_request_time = time.time()

        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 10)))
            continue
        resp.raise_for_status()
        data = resp.json()

        records = None
        for value in data.values():
            if isinstance(value, list):
                records = value
                break
        if not records:
            break
        for r in records:
            yield r

        next_link = data.get("_metadata", {}).get("nextLink")
        if not next_link:
            break


def best_picture(picture):
    """FUB's user picture comes as {size: url} (or a list of those); pick the
    largest URL available, or "" when the user has no photo."""
    if isinstance(picture, list):
        picture = picture[0] if picture else None
    if isinstance(picture, str):
        return picture
    if not isinstance(picture, dict):
        return ""
    if picture.get("original"):
        return picture["original"]

    def size(key):
        try:
            return int(str(key).split("x")[0])
        except ValueError:
            return 0
    urls = [(size(k), v) for k, v in picture.items() if isinstance(v, str) and v.startswith("http")]
    return max(urls)[1] if urls else ""


def fetch_users(session, database_url=None):
    """Agent roster as {id: name}. When database_url is given, also saves
    name/email/phone to the agents table for the Agent Snapshot page."""
    users, rows = {}, []
    for u in paginate(session, "users"):
        name = u.get("name") or f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
        users[u["id"]] = name or f"User {u['id']}"
        rows.append((u["id"], users[u["id"]], u.get("email") or "", u.get("phone") or "",
                     u.get("role") or "", u.get("status") or "", best_picture(u.get("picture"))))
    if database_url:
        conn = psycopg2.connect(database_url)
        cur = conn.cursor()
        for row in rows:
            cur.execute("""
                INSERT INTO agents (user_id, name, email, phone, role, status, picture_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email,
                    phone = EXCLUDED.phone, role = EXCLUDED.role, status = EXCLUDED.status,
                    picture_url = EXCLUDED.picture_url
            """, row)
        conn.commit()
        cur.close()
        conn.close()
    return users


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _name_of(value):
    """FUB returns some lookups (type, outcome) as a name string, others as {id, name}."""
    if isinstance(value, dict):
        return value.get("name") or ""
    return value or ""


def appointment_record(a):
    invitees = a.get("invitees", [])
    lead = next((inv for inv in invitees if inv.get("personId")), {})
    return {"appt_id": a["id"], "created_at": parse_dt(a.get("created")),
            "start_at": parse_dt(a.get("start")), "title": a.get("title") or "",
            "type": _name_of(a.get("type")), "outcome": _name_of(a.get("outcome")),
            "created_by_id": a.get("createdById"), "person_id": lead.get("personId"),
            "lead_name": lead.get("name") or "",
            "agent_ids": [inv["userId"] for inv in invitees if inv.get("userId")]}


def pull_appointment_updates(session, days=90):
    """Outcomes (held / not held) are set after an appointment happens, so
    re-read every appointment scheduled in the last/next `days` days."""
    now = datetime.now(timezone.utc)
    params = {"start": (now - timedelta(days=days)).strftime("%Y-%m-%d"),
              "end": (now + timedelta(days=days)).strftime("%Y-%m-%d")}
    return [appointment_record(a) for a in paginate(session, "appointments", params=params)]


def pull_activity(session, cutoff, appts_out):
    """Appointments and calls, tagged with person_id where available so
    the funnel can count distinct leads, not just event counts. Full
    appointment records are appended to appts_out."""
    events = []

    print("Pulling appointments...")
    for a in paginate(session, "appointments"):
        created = parse_dt(a.get("created"))
        if created is None or created < cutoff:
            break
        appts_out.append(appointment_record(a))
        invitees = a.get("invitees", [])
        # first lead-side invitee (personId set) represents the lead this appointment is with
        lead_person_id = next((inv.get("personId") for inv in invitees if inv.get("personId")), None)
        for invitee in invitees:
            uid = invitee.get("userId")
            if uid:
                events.append({"fub_id": a["id"], "event_type": "appt", "user_id": uid,
                                "created_at": created, "duration_min": 0, "person_id": lead_person_id})

    print("Pulling calls (attempts + conversations)...")
    for c in paginate(session, "calls"):
        created = parse_dt(c.get("created"))
        if created is None or created < cutoff:
            break
        uid = c.get("userId")
        if not uid:
            continue
        pid = c.get("personId")
        events.append({"fub_id": c["id"], "event_type": "attempt", "user_id": uid,
                        "created_at": created, "duration_min": 0, "person_id": pid})
        duration = c.get("duration") or 0
        if duration >= 120:
            events.append({"fub_id": c["id"], "event_type": "conversation", "user_id": uid,
                            "created_at": created, "duration_min": round(duration / 60), "person_id": pid})

    return events


def pull_new_leads(session, cutoff):
    """New leads assigned to the team — the top of the Sales Funnel.
    Stops as soon as we hit a person outside the window (people list
    defaults to newest-first)."""
    events = []
    for p in paginate(session, "people"):
        created = parse_dt(p.get("created"))
        if created is None or created < cutoff:
            break
        uid = p.get("assignedUserId")
        if uid:
            events.append({"fub_id": p["id"], "event_type": "lead", "user_id": uid,
                            "created_at": created, "duration_min": 0, "person_id": p["id"]})
    return events


def pull_people(session, cutoff):
    """Current stage + lead source for every person updated since cutoff.
    Powers the Dashboard's pipeline, response and lead-source tabs."""
    people = []
    for p in paginate(session, "people", params={"sort": "-updated"}):
        updated = parse_dt(p.get("updated"))
        if updated is None or updated < cutoff:
            break
        people.append({"person_id": p["id"], "name": p.get("name") or "",
                       "created_at": parse_dt(p.get("created")),
                       "updated_at": updated, "stage": p.get("stage") or "",
                       "source": p.get("source") or "", "assigned_user_id": p.get("assignedUserId")})
    return people


def pull_deals(session):
    """Every deal (active and archived) with its pipeline stage name. Deal
    counts are small, so this re-reads all of them each run."""
    stage_names, pipeline_names = {}, {}
    for pl in paginate(session, "pipelines"):
        pipeline_names[pl["id"]] = pl.get("name") or ""
        for st in pl.get("stages") or []:
            stage_names[st["id"]] = st.get("name") or ""

    deals = []
    for d in paginate(session, "deals", params={"includeArchived": 1}):
        deals.append({
            "deal_id": d["id"], "name": d.get("name") or "", "type": d.get("type") or "",
            "status": d.get("status") or "", "price": d.get("price") or 0,
            "pipeline_name": pipeline_names.get(d.get("pipelineId"), ""),
            "stage_name": stage_names.get(d.get("stageId"), ""),
            "created_at": parse_dt(d.get("createdAt")),
            "entered_stage_at": parse_dt(d.get("enteredStageAt")),
            "projected_close": parse_dt(d.get("projectedCloseDate")),
            "agent_commission": d.get("agentCommission") or 0,
            "team_commission": d.get("teamCommission") or 0,
            "user_ids": [u["id"] for u in d.get("users") or [] if u.get("id")],
            "user_names": ", ".join(u.get("name", "") for u in d.get("users") or []),
            "person_ids": [p["id"] for p in d.get("people") or [] if p.get("id")],
        })
    return deals


def pull_message_events(session, cutoff):
    """Bounded per-person loop for texts/emails — only checks people
    updated since cutoff, not the whole lead database."""
    events = []
    checked = 0

    for person in paginate(session, "people", params={"sort": "-updated"}):
        updated = parse_dt(person.get("updated"))
        if updated is None or updated < cutoff:
            break
        pid = person["id"]
        checked += 1

        for t in paginate(session, "textMessages", params={"personId": pid}):
            created = parse_dt(t.get("created"))
            if created is None or created < cutoff or t.get("isIncoming"):
                continue
            uid = t.get("userId")
            if uid:
                events.append({"fub_id": t["id"], "event_type": "text", "user_id": uid,
                                "created_at": created, "duration_min": 0, "person_id": pid})

        for e in paginate(session, "emails", params={"personId": pid}):
            created = parse_dt(e.get("created") or e.get("date"))
            if created is None or created < cutoff:
                continue
            uid = e.get("userId")
            if uid:
                events.append({"fub_id": e["id"], "event_type": "email", "user_id": uid,
                                "created_at": created, "duration_min": 0, "person_id": pid})

    print(f"  Checked {checked} recently-updated people for texts/emails.")
    return events


def ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_events (
            id SERIAL PRIMARY KEY,
            fub_id BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            agent_name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            duration_min INT DEFAULT 0,
            UNIQUE (fub_id, event_type, user_id)
        )
    """)
    # Safe to run every time — no-op if the column already exists
    cur.execute("ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS person_id BIGINT")
    cur.execute("CREATE INDEX IF NOT EXISTS agent_events_person_idx ON agent_events (person_id, created_at)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS people (
            person_id BIGINT PRIMARY KEY,
            created_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ,
            stage TEXT,
            source TEXT,
            assigned_user_id BIGINT,
            agent_name TEXT
        )
    """)
    cur.execute("ALTER TABLE people ADD COLUMN IF NOT EXISTS name TEXT")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            email TEXT,
            phone TEXT
        )
    """)
    for col in ("role", "status", "picture_url"):
        cur.execute(f"ALTER TABLE agents ADD COLUMN IF NOT EXISTS {col} TEXT")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            appt_id BIGINT PRIMARY KEY,
            created_at TIMESTAMPTZ,
            start_at TIMESTAMPTZ,
            title TEXT,
            type TEXT,
            outcome TEXT,
            created_by_id BIGINT,
            created_by_name TEXT,
            person_id BIGINT,
            lead_name TEXT,
            agent_ids BIGINT[],
            agent_names TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            deal_id BIGINT PRIMARY KEY,
            name TEXT,
            type TEXT,
            status TEXT,
            price NUMERIC,
            pipeline_name TEXT,
            stage_name TEXT,
            created_at TIMESTAMPTZ,
            entered_stage_at TIMESTAMPTZ,
            projected_close TIMESTAMPTZ,
            agent_commission NUMERIC,
            team_commission NUMERIC,
            user_ids BIGINT[],
            user_names TEXT,
            person_ids BIGINT[]
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pull_state (
            id INT PRIMARY KEY DEFAULT 1,
            last_pulled_at TIMESTAMPTZ,
            CHECK (id = 1)
        )
    """)


def get_last_pulled_at(cur):
    cur.execute("SELECT last_pulled_at FROM pull_state WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row else None


def set_last_pulled_at(cur, when):
    cur.execute("""
        INSERT INTO pull_state (id, last_pulled_at) VALUES (1, %s)
        ON CONFLICT (id) DO UPDATE SET last_pulled_at = EXCLUDED.last_pulled_at
    """, (when,))


def write_events_to_db(database_url, users, events, run_started_at):
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    ensure_tables(cur)

    written = 0
    for e in events:
        name = users.get(e["user_id"])
        if not name:
            continue
        cur.execute("""
            INSERT INTO agent_events (fub_id, event_type, user_id, agent_name, created_at, duration_min, person_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fub_id, event_type, user_id) DO UPDATE SET person_id = EXCLUDED.person_id
        """, (e["fub_id"], e["event_type"], e["user_id"], name, e["created_at"], e["duration_min"], e.get("person_id")))
        written += 1

    set_last_pulled_at(cur, run_started_at)
    conn.commit()
    cur.close()
    conn.close()
    return written


def write_people_to_db(database_url, users, people):
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    ensure_tables(cur)
    for p in people:
        cur.execute("""
            INSERT INTO people (person_id, name, created_at, updated_at, stage, source, assigned_user_id, agent_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (person_id) DO UPDATE SET
                name = EXCLUDED.name, created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at,
                stage = EXCLUDED.stage, source = EXCLUDED.source,
                assigned_user_id = EXCLUDED.assigned_user_id, agent_name = EXCLUDED.agent_name
        """, (p["person_id"], p["name"], p["created_at"], p["updated_at"], p["stage"], p["source"],
              p["assigned_user_id"], users.get(p["assigned_user_id"])))
    conn.commit()
    cur.close()
    conn.close()
    return len(people)


def write_appointments_to_db(database_url, users, appts):
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    for a in appts:
        cur.execute("""
            INSERT INTO appointments (appt_id, created_at, start_at, title, type, outcome, created_by_id,
                                      created_by_name, person_id, lead_name, agent_ids, agent_names)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (appt_id) DO UPDATE SET
                start_at = EXCLUDED.start_at, title = EXCLUDED.title, type = EXCLUDED.type,
                outcome = EXCLUDED.outcome, person_id = EXCLUDED.person_id, lead_name = EXCLUDED.lead_name,
                agent_ids = EXCLUDED.agent_ids, agent_names = EXCLUDED.agent_names
        """, (a["appt_id"], a["created_at"], a["start_at"], a["title"], a["type"], a["outcome"],
              a["created_by_id"], users.get(a["created_by_id"], ""), a["person_id"], a["lead_name"],
              a["agent_ids"], ", ".join(users.get(u, f"User {u}") for u in a["agent_ids"])))
    conn.commit()
    cur.close()
    conn.close()
    return len(appts)


def write_deals_to_db(database_url, deals):
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    for d in deals:
        cur.execute("""
            INSERT INTO deals (deal_id, name, type, status, price, pipeline_name, stage_name, created_at,
                               entered_stage_at, projected_close, agent_commission, team_commission,
                               user_ids, user_names, person_ids)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (deal_id) DO UPDATE SET
                name = EXCLUDED.name, type = EXCLUDED.type, status = EXCLUDED.status, price = EXCLUDED.price,
                pipeline_name = EXCLUDED.pipeline_name, stage_name = EXCLUDED.stage_name,
                created_at = EXCLUDED.created_at, entered_stage_at = EXCLUDED.entered_stage_at,
                projected_close = EXCLUDED.projected_close, agent_commission = EXCLUDED.agent_commission,
                team_commission = EXCLUDED.team_commission, user_ids = EXCLUDED.user_ids,
                user_names = EXCLUDED.user_names, person_ids = EXCLUDED.person_ids
        """, (d["deal_id"], d["name"], d["type"], d["status"], d["price"], d["pipeline_name"],
              d["stage_name"], d["created_at"], d["entered_stage_at"], d["projected_close"],
              d["agent_commission"], d["team_commission"], d["user_ids"], d["user_names"], d["person_ids"]))
    conn.commit()
    cur.close()
    conn.close()
    return len(deals)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--people-only", action="store_true",
                        help="Backfill stage/source for people updated in the last --days, then exit")
    args = parser.parse_args()

    api_key = os.environ.get("FUB_API_KEY")
    database_url = os.environ.get("DATABASE_URL")
    if not api_key:
        sys.exit("Set FUB_API_KEY first.")
    if not database_url:
        sys.exit("Set DATABASE_URL first.")

    run_started_at = datetime.now(timezone.utc)
    session = get_session(api_key)

    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    ensure_tables(cur)
    conn.commit()
    last_pulled_at = None if (args.full or args.people_only) else get_last_pulled_at(cur)
    cur.execute("SELECT EXISTS (SELECT 1 FROM people)")
    people_empty = not cur.fetchone()[0]
    cur.execute("SELECT EXISTS (SELECT 1 FROM appointments)")
    appts_empty = not cur.fetchone()[0]
    cur.close()
    conn.close()

    if args.people_only:
        cutoff = run_started_at - timedelta(days=args.days)
        print(f"People-only backfill since {cutoff.isoformat()}")
        users = fetch_users(session, database_url)
        people = pull_people(session, cutoff)
        print(f"Wrote {write_people_to_db(database_url, users, people)} people.")
        return

    if last_pulled_at:
        cutoff = last_pulled_at - timedelta(minutes=OVERLAP_MINUTES)
        print(f"Incremental pull since {cutoff.isoformat()}")
    else:
        cutoff = run_started_at - timedelta(days=args.days)
        print(f"First run (or --full) — backfilling since {cutoff.isoformat()}")

    print("Pulling agent roster...")
    users = fetch_users(session, database_url)

    backfill_cutoff = run_started_at - timedelta(days=args.days)

    # Deals and appointment details are saved right away so a later failure doesn't lose them
    print("Pulling deals...")
    print(f"Wrote {write_deals_to_db(database_url, pull_deals(session))} deals.")

    appts = []
    if appts_empty and cutoff > backfill_cutoff:
        # First run after adding the appointments table: backfill appointment details
        print(f"Backfilling appointment details since {backfill_cutoff.isoformat()}...")
        for a in paginate(session, "appointments"):
            created = parse_dt(a.get("created"))
            if created is None or created < backfill_cutoff:
                break
            appts.append(appointment_record(a))

    events = pull_activity(session, cutoff, appts)
    print(f"Collected {len(events)} appointment/call events.")
    print("Refreshing appointment outcomes...")
    appts += pull_appointment_updates(session)
    print(f"Wrote {write_appointments_to_db(database_url, users, appts)} appointment records.")

    print("Pulling new leads...")
    lead_events = pull_new_leads(session, cutoff)
    print(f"Collected {len(lead_events)} new lead events.")
    events += lead_events

    # Saved right away so a later failure doesn't lose stage/source data
    # First run after adding the people table: backfill it fully, not just since last pull
    people_cutoff = backfill_cutoff if people_empty else cutoff
    print(f"Pulling people (stage + source) since {people_cutoff.isoformat()}...")
    people = pull_people(session, people_cutoff)
    print(f"Wrote {write_people_to_db(database_url, users, people)} people.")

    print("Pulling texts/emails (bounded to recently-updated people)...")
    message_events = pull_message_events(session, cutoff)
    print(f"Collected {len(message_events)} text/email events.")
    events += message_events

    written = write_events_to_db(database_url, users, events, run_started_at)
    print(f"Wrote {written} events. Next run will pull since {run_started_at.isoformat()}.")

    # CTE workbooks from OneDrive (read-only). Kept separate so a CTE problem
    # never fails the Follow Up Boss pull above, which is already saved.
    if cte_import.graph_configured():
        try:
            cte_import.import_from_onedrive(database_url)
        except Exception as e:  # noqa: BLE001 - report and carry on
            print(f"CTE import failed: {e}")
    else:
        print("CTE import skipped: MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET not set.")


if __name__ == "__main__":
    main()
