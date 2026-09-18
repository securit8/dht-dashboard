"""
Follow Up Boss event pull script — Render Cron Job version.

Pulls appointments, calls, and — via a bounded per-person lookup —
texts and emails, writing one row per individual event into Postgres
so the dashboard can total any period on demand.

Texts/emails work around FUB's API restriction (no team-wide listing)
by first finding people updated since the cutoff (cheap, sorted,
bounded), then checking just those people's messages individually.
This only works because most leads are inactive on any given day —
checking all 12,000+ leads every run would not be feasible.

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


def get_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.auth = (api_key, "")
    session.headers.update({"Accept": "application/json"})
    return session


def paginate(session, endpoint, params=None):
    params = dict(params or {})
    params.setdefault("limit", PAGE_SIZE)
    next_link = None
    while True:
        resp = session.get(next_link) if next_link else session.get(f"{API_BASE}/{endpoint}", params=params)
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
    events = []

    print("Pulling appointments...")
    for a in paginate(session, "appointments"):
        created = parse_dt(a.get("created"))
        if created is None or created < cutoff:
            break
        for invitee in a.get("invitees", []):
            uid = invitee.get("userId")
            if uid:
                events.append({"fub_id": a["id"], "event_type": "appt",
                                "user_id": uid, "created_at": created, "duration_min": 0})

    print("Pulling calls (attempts + conversations)...")
    for c in paginate(session, "calls"):
        created = parse_dt(c.get("created"))
        if created is None or created < cutoff:
            break
        uid = c.get("userId")
        if not uid:
            continue
        events.append({"fub_id": c["id"], "event_type": "attempt",
                        "user_id": uid, "created_at": created, "duration_min": 0})
        duration = c.get("duration") or 0
        if duration >= 120:
            events.append({"fub_id": c["id"], "event_type": "conversation",
                            "user_id": uid, "created_at": created, "duration_min": round(duration / 60)})

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
                events.append({"fub_id": t["id"], "event_type": "text",
                                "user_id": uid, "created_at": created, "duration_min": 0})

        for e in paginate(session, "emails", params={"personId": pid}):
            created = parse_dt(e.get("created") or e.get("date"))
            if created is None or created < cutoff:
                continue
            uid = e.get("userId")
            if uid:
                events.append({"fub_id": e["id"], "event_type": "email",
                                "user_id": uid, "created_at": created, "duration_min": 0})

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
            INSERT INTO agent_events (fub_id, event_type, user_id, agent_name, created_at, duration_min)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (fub_id, event_type, user_id) DO NOTHING
        """, (e["fub_id"], e["event_type"], e["user_id"], name, e["created_at"], e["duration_min"]))
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

    print("Pulling texts/emails (bounded to recently-updated people)...")
    message_events = pull_message_events(session, cutoff)
    print(f"Collected {len(message_events)} text/email events.")
    events += message_events

    written = write_events_to_db(database_url, users, events, run_started_at)
    print(f"Wrote {written} events. Next run will pull since {run_started_at.isoformat()}.")


if __name__ == "__main__":
    main()
