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


def fetch_users(session):
    users = {}
    for u in paginate(session, "users"):
        name = u.get("name") or f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
        users[u["id"]] = name or f"User {u['id']}"
    return users


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def pull_activity(session, cutoff):
    """Appointments and calls, tagged with person_id where available so
    the funnel can count distinct leads, not just event counts."""
    events = []

    print("Pulling appointments...")
    for a in paginate(session, "appointments"):
        created = parse_dt(a.get("created"))
        if created is None or created < cutoff:
            break
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--full", action="store_true")
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
    last_pulled_at = None if args.full else get_last_pulled_at(cur)
    cur.close()
    conn.close()

    if last_pulled_at:
        cutoff = last_pulled_at - timedelta(minutes=OVERLAP_MINUTES)
        print(f"Incremental pull since {cutoff.isoformat()}")
    else:
        cutoff = run_started_at - timedelta(days=args.days)
        print(f"First run (or --full) — backfilling since {cutoff.isoformat()}")

    print("Pulling agent roster...")
    users = fetch_users(session)

    events = pull_activity(session, cutoff)
    print(f"Collected {len(events)} appointment/call events.")

    print("Pulling new leads...")
    lead_events = pull_new_leads(session, cutoff)
    print(f"Collected {len(lead_events)} new lead events.")
    events += lead_events

    print("Pulling texts/emails (bounded to recently-updated people)...")
    message_events = pull_message_events(session, cutoff)
    print(f"Collected {len(message_events)} text/email events.")
    events += message_events

    written = write_events_to_db(database_url, users, events, run_started_at)
    print(f"Wrote {written} events. Next run will pull since {run_started_at.isoformat()}.")


if __name__ == "__main__":
    main()
