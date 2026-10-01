"""Owner decisions on the dashboard's Needs-attention items: a dropdown choice and/or a comment,
with who wrote it and when. Nothing here changes any data: the decisions are read (on the Decisions
page) and the fixes are made by hand, then the decision is marked fixed with a note on what was done.
"""


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
