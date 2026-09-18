"""
Follow Up Boss agent-activity pull script — Render Cron Job version.

Pulls calls, appointments, and (where the API allows) text messages,
emails, and Zillow messages for every agent on the team, and writes a
per-agent summary into Postgres (Render's managed database). The
dashboard's web service reads from that same table.

IMPORTANT: this needs open internet access to api.followupboss.com.
It runs fine as a Render Cron Job; it will NOT work inside Claude's
sandboxed chat/artifact environment (a platform restriction, not a
bug).

KNOWN LIMITATION: FUB's /v1/textMessages and /v1/emails both require
personId, threadId, or similar — there is no "list every message for
the team" mode. Both are skipped gracefully rather than crashing the
run. Appointments, Call Attempts, and Conversations are solid.

Setup (local test):
    pip install requests psycopg2-binary
    export FUB_API_KEY="your_api_key_here"      # Admin > More > API in FUB
    export DATABASE_URL="postgres://..."         # from Render's DB page
    python fub_agent_activity_pull.py --days 1

On Render, FUB_API_KEY and DATABASE_URL are set as environment
variables in the dashboard — you don't pass them on the command line
there.

A broker/admin API key sees every agent's activity. An individual
agent's key only sees their own.
"""

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

API_BASE = "https://api.followupboss.com/v1"
PAGE_SIZE = 100  # FUB's max per page


def get_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.auth = (api_key, "")  # API key as username, blank password
    session.headers.update({"Accept": "application/json"})
    return session


def paginate(session, endpoint, params=None):
    """Yield every record from a FUB list endpoint, handling pagination
    and rate limits. Finds the list field in the response defensively
    since the wrapping key's casing varies by resource."""
    params = dict(params or {})
    params.setdefault("limit", PAGE_SIZE)
    offset = 0
    while True:
        params["offset"] = offset
        resp = session.get(f"{API_BASE}/{endpoint}", params=params)
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

        total = data.get("_metadata", {}).get("total", len(records))
        offset += len(records)
        if offset >= total or len(records) < params["limit"]:
            break


def fetch_users(session):
    """Return {user_id: display_name} for every agent on the team."""
    users = {}
    for u in paginate(session, "users"):
        name = u.get("name") or f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
        users[u["id"]] = name or f"User {u['id']}"
    return users


def within_window(created_str, cutoff):
    if not created_str:
        return False
    try:
        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    return created >= cutoff


def pull_activity(session, days):
  now = datetime.now(timezone.utc)
    cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    activity = defaultdict(lambda: {
        "appts": 0, "conversations": 0, "conversations_dur_min": 0,
        "attempts": 0, "texts": 0, "zillow": 0, "emails": 0,
    })

    print("Pulling appointments...")
    for a in paginate(session, "appointments"):
        if not within_window(a.get("created"), cutoff):
            break
        for invitee in a.get("invitees", []):
            uid = invitee.get("userId")
            if uid:
                activity[uid]["appts"] += 1

    print("Pulling calls (attempts + conversations)...")
    for c in paginate(session, "calls"):
        if not within_window(c.get("created"), cutoff):
            break
        uid = c.get("userId")
        if not uid:
            continue
        activity[uid]["attempts"] += 1
        duration = c.get("duration") or 0
        if duration >= 120:
            activity[uid]["conversations"] += 1
            activity[uid]["conversations_dur_min"] += round(duration / 60)

    print("Pulling text messages...")
    try:
        for t in paginate(session, "textMessages"):
            if not within_window(t.get("created"), cutoff):
                break
            uid = t.get("userId")
            if uid:
                activity[uid]["texts"] += 1
    except requests.HTTPError as err:
        print(f"  (skipped — {err.response.status_code}: {err.response.text.strip()})")

    print("Pulling emails...")
    try:
        for e in paginate(session, "emails"):
            if not within_window(e.get("created"), cutoff):
                break
            uid = e.get("userId")
            if uid:
                activity[uid]["emails"] += 1
    except requests.HTTPError as err:
        print(f"  (skipped — {err.response.status_code}: {err.response.text.strip()})")

    print("Pulling Zillow messages...")
    try:
        for t in paginate(session, "textMessages", params={"source": "Zillow"}):
            if not within_window(t.get("created"), cutoff):
                break
            uid = t.get("userId")
            if uid:
                activity[uid]["zillow"] += 1
    except requests.HTTPError as err:
        print(f"  (skipped — {err.response.status_code}: {err.response.text.strip()})")

    return activity


def write_to_db(database_url, users, activity, days):
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_activity (
            agent_name TEXT PRIMARY KEY,
            appts INT, conversations INT, conversations_dur_min INT,
            attempts INT, texts INT, zillow INT, emails INT,
            window_days INT, pulled_at TIMESTAMPTZ
        )
    """)
    pulled_at = datetime.now(timezone.utc)
    for uid, name in users.items():
        a = activity.get(uid, {"appts": 0, "conversations": 0, "conversations_dur_min": 0,
                                "attempts": 0, "texts": 0, "zillow": 0, "emails": 0})
        cur.execute("""
            INSERT INTO agent_activity (agent_name, appts, conversations, conversations_dur_min,
                                         attempts, texts, zillow, emails, window_days, pulled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_name) DO UPDATE SET
                appts = EXCLUDED.appts, conversations = EXCLUDED.conversations,
                conversations_dur_min = EXCLUDED.conversations_dur_min,
                attempts = EXCLUDED.attempts, texts = EXCLUDED.texts,
                zillow = EXCLUDED.zillow, emails = EXCLUDED.emails,
                window_days = EXCLUDED.window_days, pulled_at = EXCLUDED.pulled_at
        """, (name, a["appts"], a["conversations"], a["conversations_dur_min"],
              a["attempts"], a["texts"], a["zillow"], a["emails"], days, pulled_at))
    conn.commit()
    cur.close()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Pull FUB agent activity into Postgres.")
    parser.add_argument("--days", type=int, default=1, help="Lookback window in days")
    args = parser.parse_args()

    api_key = os.environ.get("FUB_API_KEY")
    database_url = os.environ.get("DATABASE_URL")
    if not api_key:
        sys.exit("Set FUB_API_KEY in your environment first.")
    if not database_url:
        sys.exit("Set DATABASE_URL in your environment first.")

    session = get_session(api_key)

    print("Pulling agent roster...")
    users = fetch_users(session)

    activity = pull_activity(session, args.days)

    write_to_db(database_url, users, activity, args.days)
    print(f"Wrote {len(users)} agents to the database.")


if __name__ == "__main__":
    main()
