"""Owner decisions on the dashboard's Needs-attention items: a dropdown choice and/or a comment,
with who wrote it and when.

Decisions with a clear data meaning are used by the dashboard right away (apply_fixes): the CTE
typo fixes and agent-name merges are applied to the imported CTE rows in the dashboard's database,
never to the files in OneDrive, and only while the file still has the old value, so a corrected
file uploaded later simply takes over. Splits read the agreement choices (splits.py). Everything
else is read on the Decisions page and fixed by hand, then marked fixed with a note.
"""
import re
from datetime import datetime


STATUSES = [("new", "New"), ("needs_info", "Needs more info"), ("fixed", "Fixed")]


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS owner_decisions (
            id SERIAL PRIMARY KEY,
            item_key TEXT NOT NULL,
            item_title TEXT,
            item_detail TEXT,
            choice TEXT,
            comment TEXT,
            decided_by TEXT,
            decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status TEXT NOT NULL DEFAULT 'new',
            fix_note TEXT,
            fixed_at TIMESTAMPTZ
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS owner_decisions_key ON owner_decisions (item_key, decided_at DESC)")


_COLS = ["id", "item_key", "item_title", "item_detail", "choice", "comment", "decided_by", "decided_at",
         "status", "fix_note", "fixed_at"]


def add(cur, item_key, title, detail, choice, comment, by):
    ensure_table(cur)
    cur.execute("""INSERT INTO owner_decisions (item_key, item_title, item_detail, choice, comment, decided_by)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (item_key, title, detail, choice or None, comment or None, by or None))


def latest(cur):
    """{item_key: the newest decision}"""
    ensure_table(cur)
    cur.execute(f"""SELECT DISTINCT ON (item_key) {", ".join(_COLS)} FROM owner_decisions
                    ORDER BY item_key, decided_at DESC""")
    return {r[1]: dict(zip(_COLS, r)) for r in cur.fetchall()}


def all_(cur, status=None):
    ensure_table(cur)
    cur.execute(f"""SELECT {", ".join(_COLS)} FROM owner_decisions
                    WHERE %(s)s::text IS NULL OR status = %(s)s ORDER BY decided_at DESC""", {"s": status})
    return [dict(zip(_COLS, r)) for r in cur.fetchall()]


def set_status(cur, decision_id, status, fix_note=None):
    ensure_table(cur)
    cur.execute("""UPDATE owner_decisions SET status = %s, fix_note = COALESCE(%s, fix_note),
                       fixed_at = CASE WHEN %s = 'fixed' THEN NOW() ELSE fixed_at END
                   WHERE id = %s""", (status, fix_note or None, status, decision_id))


COMPASS_RIGHT = "Compass is right: fix the CTE file"
SAME_PERSON = "Same person: count together"
APPLY = "Apply"
# One CTE cell corrected on the dashboard: key "fix:<address>|<close date>|<field>|<old>|<new>" (old may be empty)
FIELD_FIXES = {"sale_price": float, "gci": float, "commission_pct": float, "address": str, "source": str,
               "signed_date": lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
               "close_date": lambda s: datetime.strptime(s, "%Y-%m-%d").date()}


def field_fix_key(address, close_date, field, old, new):
    return f"fix:{address}|{close_date}|{field}|{'' if old is None else old}|{new}"


def _typo_fix(dec):
    """(old address, old close date, field, new value) from a typo decision, or None."""
    m = re.match(r"typo:(.+)\|(\d{4}-\d{2}-\d{2})$", dec["item_key"] or "")
    if not m:
        return None
    address, close = m.group(1).strip(), m.group(2)
    detail = dec["item_detail"] or ""
    dm = re.search(r"close date (\d{2}/\d{2}/\d{4})", detail)
    if dm:
        return address, close, "close_date", datetime.strptime(dm.group(1), "%m/%d/%Y").date()
    am = re.search(r"has address (\d+)\b", detail)
    if am:
        parts = address.split(None, 1)
        if parts and parts[0].isdigit() and parts[0] != am.group(1):
            return address, close, "address", am.group(1) + (" " + parts[1] if len(parts) > 1 else "")
    return None


def apply_fixes(cur):
    """Apply the decided fixes to the imported CTE rows (dashboard database only). Safe to run any time:
    a fix only touches rows that still have the old value. Returns lines describing what changed."""
    ensure_table(cur)
    cur.execute("SELECT to_regclass('cte_deals') IS NOT NULL")
    if not cur.fetchone()[0]:
        return []
    done = []
    # typo and name fixes first: a field fix names the deal by its corrected address
    decided = sorted(latest(cur).values(), key=lambda d: (d["item_key"] or "").startswith("fix:"))
    for dec in decided:
        key = dec["item_key"] or ""
        if key.startswith("fix:") and dec["choice"] == APPLY:
            parts = key[len("fix:"):].split("|")
            if len(parts) != 5 or parts[2] not in FIELD_FIXES:
                continue
            address, close, field, old, new = parts
            conv = FIELD_FIXES[field]
            cur.execute(f"""UPDATE cte_deals SET {field} = %s
                            WHERE TRIM(address) = %s AND close_date = %s AND {field} IS NOT DISTINCT FROM %s""",
                        (conv(new), address, close, conv(old) if old else None))
            if cur.rowcount:
                done.append((dec["id"], f"Applied on the dashboard: {address} {field.replace('_', ' ')} "
                                        f"{old or '(empty)'} -> {new} (the CTE file itself is unchanged; "
                                        "a corrected upload takes over)."))
        elif key.startswith("typo:") and dec["choice"] == COMPASS_RIGHT:
            fix = _typo_fix(dec)
            if not fix:
                continue
            address, close, field, new = fix
            cur.execute(f"""UPDATE cte_deals SET {field} = %s
                            WHERE TRIM(address) = %s AND close_date = %s AND status = 'Closed'""", (new, address, close))
            if cur.rowcount:
                what = new.strftime("%m/%d/%Y") if field == "close_date" else new
                done.append((dec["id"], f"Applied on the dashboard: {address} {field.replace('_', ' ')} -> {what} "
                                        "(the CTE file itself is unchanged; a corrected upload takes over)."))
        elif key.startswith("same_person:") and dec["choice"] == SAME_PERSON:
            a, _, b = key[len("same_person:"):].partition("|")
            keep, alias = (a, b) if len(a) >= len(b) else (b, a)  # keep the fuller name
            n = 0
            for col in ("primary_agent", "secondary_agent", "agent3", "agent4"):
                cur.execute(f"UPDATE cte_deals SET {col} = %s WHERE LOWER(TRIM({col})) = LOWER(%s)", (keep, alias))
                n += cur.rowcount
            cur.execute("UPDATE cte_activity SET agent_name = %s WHERE LOWER(TRIM(agent_name)) = LOWER(%s)", (keep, alias))
            n += cur.rowcount
            if n:
                done.append((dec["id"], f"Applied on the dashboard: \"{alias}\" counted as \"{keep}\" ({n} CTE rows)."))
    for decision_id, note in done:
        set_status(cur, decision_id, "fixed", note)
    return [note for _, note in done]
