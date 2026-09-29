"""One-time, read-only look through the Follow Up Boss API for anywhere a lead's
past stages might be kept (the API has no stage-history endpoint).

Runs from the cron job once (while fub_probe is empty) and saves what it finds to
the fub_probe table; the dashboard shows it at /fub-probe. Only GET requests.
Delete the fub_probe rows to run it again.
"""
import json
import re

import psycopg2

API_BASE = "https://api.followupboss.com/v1"
STAGE_WORDS = re.compile(r"stage", re.I)


def _get(session, path, params=None):
    try:
        r = session.get(f"{API_BASE}/{path}", params=params or {}, timeout=30)
        try:
            body = r.json()
        except ValueError:
            body = r.text[:500]
        return r.status_code, body
    except Exception as e:  # noqa: BLE001 - record and carry on
        return 0, str(e)


def _records(body):
    if isinstance(body, dict):
        for v in body.values():
            if isinstance(v, list):
                return v
    return []


def _clip(obj, n=3000):
    s = json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + " ..."


def _stage_bits(obj, path=""):
    """Every key/value anywhere in obj whose key or text mentions 'stage'."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if STAGE_WORDS.search(k) and not isinstance(v, (dict, list)):
                out.append((p, v))
            out += _stage_bits(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            out += _stage_bits(v, f"{path}[{i}]")
    elif isinstance(obj, str) and STAGE_WORDS.search(obj):
        out.append((path, obj[:300]))
    return out


def run(session, database_url):
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS fub_probe (
                       name TEXT PRIMARY KEY, status INT, result TEXT, probed_at TIMESTAMPTZ DEFAULT now())""")
    conn.commit()
    cur.execute("SELECT EXISTS (SELECT 1 FROM fub_probe)")
    if cur.fetchone()[0]:
        conn.close()
        return
    print("Looking through the FUB API for lead stage history (one time, read-only)...")

    # A lead with plenty of history: most appointments, then most recently updated
    cur.execute("""SELECT a.person_id FROM appointments a JOIN people p ON p.person_id = a.person_id
                   WHERE a.person_id IS NOT NULL GROUP BY 1 ORDER BY COUNT(*) DESC, MAX(p.updated_at) DESC LIMIT 1""")
    row = cur.fetchone()
    pid = row[0] if row else None

    results = {}

    def save(name, status, result):
        results[name] = (status, result)
        print(f"  {name}: HTTP {status}")

    if pid:
        st, body = _get(session, f"people/{pid}", {"fields": "allFields"})
        save("people/:id allFields - all keys", st, _clip(sorted(body.keys()) if isinstance(body, dict) else body))
        save("people/:id allFields - stage fields", st, _clip(_stage_bits(body) if isinstance(body, dict) else []))

        st, body = _get(session, "notes", {"personId": pid, "limit": 100})
        notes = _records(body)
        save("notes?personId - count / keys", st, _clip({"count": len(notes), "keys": sorted(notes[0].keys()) if notes else []}))
        save("notes?personId - mentions of stage", st, _clip(_stage_bits(notes)))

        st, body = _get(session, "events", {"personId": pid, "limit": 100})
        ev = _records(body)
        save("events?personId - types", st, _clip(sorted({e.get("type") for e in ev if isinstance(e, dict)}, key=str)))
        save("events?personId - mentions of stage", st, _clip(_stage_bits(ev)))

        for path in ("timeline", "personStages", "activities", "people/%s/timeline" % pid, "people/%s/history" % pid):
            st, body = _get(session, path, {"personId": pid, "limit": 20})
            save(f"{path} (undocumented)", st, _clip(body, 800))

        st, body = _get(session, "emails", {"personId": pid, "limit": 100})
        save("emails?personId - mentions of stage", st, _clip(_stage_bits(_records(body))))
        st, body = _get(session, "textMessages", {"personId": pid, "limit": 100})
        save("textMessages?personId - mentions of stage", st, _clip(_stage_bits(_records(body))))
        st, body = _get(session, "automationsPeople", {"personId": pid, "limit": 100})
        save("automationsPeople?personId", st, _clip(_records(body)[:20]))
        st, body = _get(session, "actionPlansPeople", {"personId": pid, "limit": 100})
        save("actionPlansPeople?personId", st, _clip(_records(body)[:20]))
    else:
        save("lead to test", 0, "no appointments with a lead yet")

    st, body = _get(session, "automations", {"limit": 100})
    autos = _records(body)
    save("automations - names", st, _clip([a.get("name") for a in autos if isinstance(a, dict)]))
    save("automations - stage triggers", st, _clip(_stage_bits(autos)))
    save("automations - first one in full", st, _clip(autos[:1]))
    st, body = _get(session, "automationsPeople", {"limit": 20})
    save("automationsPeople - sample", st, _clip(_records(body)[:5]))

    st, body = _get(session, "actionPlans", {"limit": 100})
    plans = _records(body)
    save("actionPlans - names", st, _clip([a.get("name") for a in plans if isinstance(a, dict)]))
    save("actionPlans - stage triggers", st, _clip(_stage_bits(plans)))
    st, body = _get(session, "actionPlansPeople", {"limit": 20})
    save("actionPlansPeople - sample", st, _clip(_records(body)[:5]))

    st, body = _get(session, "webhooks")
    save("webhooks - already registered", st, _clip(_records(body)))

    for name, (status, result) in results.items():
        cur.execute("""INSERT INTO fub_probe (name, status, result) VALUES (%s, %s, %s)
                       ON CONFLICT (name) DO UPDATE SET status = EXCLUDED.status, result = EXCLUDED.result,
                                                        probed_at = now()""", (name, status, result))
    conn.commit()
    conn.close()
