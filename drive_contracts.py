"""Google Drive: read-only pull of the agents' signed agreements, so a new or changed contract is used without
anyone typing it in.

Read-only by design: the only scope is drive.readonly and the only calls list files, read a folder's name and
download a PDF. Nothing in Drive is ever created, changed, moved or deleted. Each night (and on "Read Drive now")
every agreement PDF in an agent's folder that is new or changed since the last read is read: the split table
(agent/company by lead source), the cheat sheet on the last page, the $10M bonus and the signing date.

An agreement that reads cleanly (table and cheat sheet agree, and it is signed) is used by the splits right away
(splits.decided_contracts). Anything else goes on Needs attention with what was read; the owners can tell the
dashboard to use it as read. The agreements already typed into splits.CONTRACTS are not read again.

Settings: the same GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET as Gmail (the Drive API must be enabled in that
Google Cloud project). The token is encrypted in Postgres like the Gmail one.
"""
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import psycopg2

import gmail_import as G

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
API = "https://www.googleapis.com/drive/v3"
# Agreement PDFs: the name says contract or agreement; Compass's, Zillow's and referral paperwork are not ours
NAME_Q = " or ".join(f"name contains '{w}'" for w in ("Contract", "contract", "CONTRACT", "Agreement", "agreement", "AGREEMENT"))
NOT_OURS = re.compile(r"compass|zillow|referral|_ICA_|buyer|representation|listing", re.I)
USE_AS_READ = "Use it as read"


def ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drive_account (
            id INT PRIMARY KEY DEFAULT 1,
            email TEXT, access_token TEXT, access_expires_at TIMESTAMPTZ, refresh_token TEXT,
            connected_at TIMESTAMPTZ, last_pull_at TIMESTAMPTZ, needs_reconnect BOOLEAN DEFAULT false, last_error TEXT
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drive_contracts (
            file_id TEXT PRIMARY KEY,
            agent TEXT, file TEXT, folder TEXT, modified_at TIMESTAMPTZ, read_at TIMESTAMPTZ,
            signed DATE, personal INT, zillow INT, database INT, bonus INT,
            sheet_personal INT, sheet_zillow INT, sheet_database INT,
            problem TEXT
        )""")


def _db(database_url):
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    ensure_tables(cur)
    return conn, cur


def status(cur):
    ensure_tables(cur)
    cur.execute("SELECT email, connected_at, last_pull_at, needs_reconnect, last_error FROM drive_account WHERE id = 1")
    r = cur.fetchone()
    if not r or not r[0]:
        return None
    cur.execute("SELECT COUNT(*) FROM drive_contracts")
    return dict(zip(["email", "connected_at", "last_pull_at", "needs_reconnect", "last_error"], r), files=cur.fetchone()[0])


# ---------------------------------------------------------------- OAuth (same Google client as Gmail)

def authorize_url(redirect_uri, state):
    return G.AUTH_URL + "?" + urlencode({
        "client_id": G.os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent select_account", "state": state})


def connect(database_url, code, redirect_uri):
    tokens = G._token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    if not tokens.get("refresh_token"):
        raise G.GmailError("Google did not return a refresh token. Remove the app's access in your Google "
                           "account (Security > Third-party access) and connect again.")
    if SCOPE not in set((tokens.get("scope") or "").split()):
        raise G.GmailError("Read access to Google Drive was not granted. Connect again and tick the Drive box.")
    r = G._request("GET", f"{API}/about", params={"fields": "user(emailAddress)"},
                   headers={"Authorization": f"Bearer {tokens['access_token']}"})
    if r.status_code >= 400:
        raise G.GmailError(f"Could not read the Drive account (HTTP {r.status_code}). Is the Drive API enabled "
                           "in the Google Cloud project?")
    email = r.json()["user"]["emailAddress"].lower()
    conn, cur = _db(database_url)
    cur.execute("""
        INSERT INTO drive_account (id, email, access_token, access_expires_at, refresh_token, connected_at,
                                   needs_reconnect, last_error)
        VALUES (1, %s, %s, %s, %s, now(), false, NULL)
        ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, access_token = EXCLUDED.access_token,
            access_expires_at = EXCLUDED.access_expires_at, refresh_token = EXCLUDED.refresh_token,
            connected_at = now(), needs_reconnect = false, last_error = NULL""",
                (email, G._enc(tokens["access_token"]),
                 datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 120),
                 G._enc(tokens["refresh_token"])))
    conn.commit()
    conn.close()
    return email


def disconnect(database_url):
    """Forget the Drive token (what was read from the agreements stays; it's only numbers and dates)."""
    conn, cur = _db(database_url)
    cur.execute("SELECT refresh_token FROM drive_account WHERE id = 1")
    r = cur.fetchone()
    if r and r[0]:
        try:
            G._request("POST", G.REVOKE_URL, params={"token": G._dec(r[0])})
        except Exception:  # noqa: BLE001 - forget it locally either way
            pass
    cur.execute("DELETE FROM drive_account WHERE id = 1")
    conn.commit()
    conn.close()


def _access_token(database_url):
    conn, cur = _db(database_url)
    cur.execute("SELECT access_token, access_expires_at, refresh_token, needs_reconnect FROM drive_account WHERE id = 1")
    row = cur.fetchone()
    if not row or not row[2]:
        conn.close()
        raise G.NeedsReconnect("Google Drive is not connected")
    access, exp, refresh, needs = row
    if needs:
        conn.close()
        raise G.NeedsReconnect("Google Drive needs to be reconnected")
    if access and exp and exp > datetime.now(timezone.utc):
        conn.close()
        return G._dec(access)
    try:
        tokens = G._token_call({"grant_type": "refresh_token", "refresh_token": G._dec(refresh)})
    except G.NeedsReconnect as e:
        cur.execute("UPDATE drive_account SET needs_reconnect = true, last_error = %s WHERE id = 1", (str(e)[:500],))
        conn.commit()
        conn.close()
        raise
    cur.execute("UPDATE drive_account SET access_token = %s, access_expires_at = %s WHERE id = 1",
                (G._enc(tokens["access_token"]),
                 datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 120)))
    conn.commit()
    conn.close()
    return tokens["access_token"]


def _get(token, path, raw=False, **params):
    r = G._request("GET", f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {token}"},
                   timeout=60)
    if r.status_code >= 400:
        raise G.GmailError(f"Drive HTTP {r.status_code} on {path}: {r.text[:200]}")
    return r.content if raw else r.json()


# ---------------------------------------------------------------- reading an agreement

def _pct(pattern, text):
    m = re.search(pattern, text, re.I | re.S)
    return [int(x) for x in m.groups()] if m else None


def parse(text):
    """Company % by lead source from the split table and from the cheat sheet, the $10M bonus and the signing date."""
    t = re.sub(r"[ \t]+", " ", text)
    body, _, sheet = t.partition("QUICK REFERENCE")
    out = {}
    for key, pat in (("personal", r"Personal Contacts.{0,80}?(\d{1,2})%\s*(\d{1,2})%"),
                     ("zillow", r"Zillow Leads\s*(\d{1,2})%\s*(\d{1,2})%"),
                     ("database", r"Database Leads.{0,80}?(\d{1,2})%\s*(\d{1,2})%")):
        v = _pct(pat, body)
        out[key] = v[1] if v and v[0] + v[1] == 100 else None
    for key, pat in (("sheet_personal", r"personal contacts\s*(\d{1,2})%"), ("sheet_zillow", r"Zillow leads\s*(\d{1,2})%"),
                     ("sheet_database", r"database leads\s*(\d{1,2})%")):
        v = _pct(pat, sheet)
        out[key] = 100 - v[0] if v else None
    b = _pct(r"After reaching \$10M total:\s*You get (\d{1,2})%,?\s*Company gets (\d{1,2})%", t)
    out["bonus"] = b[1] if b else None
    sig = t[t.find("ACKNOWLEDGMENT"):] if "ACKNOWLEDGMENT" in t else t[-3000:]
    dates = []
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", sig):
        try:
            dates.append(date(int(m.group(3)), int(m.group(1)), int(m.group(2))))
        except ValueError:
            pass
    out["signed"] = max(dates) if dates else None
    problems = []
    if None in (out["personal"], out["zillow"], out["database"]):
        problems.append("the split table couldn't be read")
    elif any(out[f"sheet_{k}"] is not None and out[f"sheet_{k}"] != out[k] for k in ("personal", "zillow", "database")):
        problems.append("the split table and the cheat sheet on the last page don't agree")
    if not out["signed"]:
        problems.append("it isn't signed and dated")
    out["problem"] = "; ".join(problems) or None
    return out


def pull(database_url, log=print):
    """Read every agreement PDF in the agents' folders that is new or changed. Returns how many were read."""
    import splits
    token = _access_token(database_url)
    known = {c["file"].strip().lower() for c in splits.CONTRACTS}
    files, page = [], None
    while True:
        res = _get(token, "files", q=f"mimeType = 'application/pdf' and trashed = false and ({NAME_Q})",
                   fields="nextPageToken, files(id, name, modifiedTime, parents)", pageSize=200,
                   includeItemsFromAllDrives="true", supportsAllDrives="true", **({"pageToken": page} if page else {}))
        files += res.get("files", [])
        page = res.get("nextPageToken")
        if not page:
            break
    conn, cur = _db(database_url)
    cur.execute("SELECT file_id, modified_at FROM drive_contracts")
    seen = dict(cur.fetchall())
    conn.close()
    folders, read = {}, 0
    for f in files:
        name = f["name"]
        if name.strip().lower() in known or NOT_OURS.search(name) or not f.get("parents"):
            continue
        modified = datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))
        if f["id"] in seen and seen[f["id"]] and seen[f["id"]] >= modified:
            continue
        parent = f["parents"][0]
        if parent not in folders:
            try:
                folders[parent] = _get(token, f"files/{parent}", fields="name", supportsAllDrives="true")["name"]
            except G.GmailError:
                folders[parent] = ""
        agent = folders[parent].strip()
        # agent folders are named after the agent ("Jeff Iacoviello"); skip anything else
        if not re.fullmatch(r"[A-Z][\w'\-]+(\s+[A-Z][\w'\-]+){1,3}", agent) or re.search(r"folder|team|listing", agent, re.I):
            continue
        try:
            text = G.pdf_text(_get(token, f"files/{f['id']}", raw=True, alt="media", supportsAllDrives="true"))
        except Exception as e:  # noqa: BLE001 - note it and go on
            log(f"Drive: couldn't read {name}: {e}")
            continue
        if "Compass California" in text or not re.search(r"commission split", text, re.I):
            continue  # not a team agreement with a split table
        p = parse(text)
        conn, cur = _db(database_url)
        cur.execute("""
            INSERT INTO drive_contracts (file_id, agent, file, folder, modified_at, read_at, signed, personal, zillow,
                                         database, bonus, sheet_personal, sheet_zillow, sheet_database, problem)
            VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (file_id) DO UPDATE SET agent = EXCLUDED.agent, file = EXCLUDED.file, folder = EXCLUDED.folder,
                modified_at = EXCLUDED.modified_at, read_at = now(), signed = EXCLUDED.signed,
                personal = EXCLUDED.personal, zillow = EXCLUDED.zillow, database = EXCLUDED.database,
                bonus = EXCLUDED.bonus, sheet_personal = EXCLUDED.sheet_personal, sheet_zillow = EXCLUDED.sheet_zillow,
                sheet_database = EXCLUDED.sheet_database, problem = EXCLUDED.problem""",
                    (f["id"], agent, name, folders[parent], modified, p["signed"], p["personal"], p["zillow"],
                     p["database"], p["bonus"], p["sheet_personal"], p["sheet_zillow"], p["sheet_database"], p["problem"]))
        conn.commit()
        conn.close()
        read += 1
        log(f"Drive: read {agent} / {name}" + (f" ({p['problem']})" if p["problem"] else ""))
    conn, cur = _db(database_url)
    cur.execute("UPDATE drive_account SET last_pull_at = now(), last_error = NULL WHERE id = 1")
    conn.commit()
    conn.close()
    return read


def cron_pull(database_url):
    conn, cur = _db(database_url)
    cur.execute("SELECT 1 FROM drive_account WHERE id = 1 AND refresh_token IS NOT NULL AND NOT needs_reconnect")
    on = cur.fetchone()
    conn.close()
    if not on:
        print("Drive agreements skipped: Google Drive not connected (Agent Splits page).")
        return
    try:
        print(f"Drive agreements: read {pull(database_url)} new or changed file(s).")
    except Exception as e:  # noqa: BLE001 - report and carry on
        conn, cur = _db(database_url)
        cur.execute("UPDATE drive_account SET last_error = %s WHERE id = 1", (str(e)[:500],))
        conn.commit()
        conn.close()
        print(f"Drive agreements failed: {e}")


def rows(cur):
    """What was read from Drive, newest first."""
    ensure_tables(cur)
    cols = ["file_id", "agent", "file", "folder", "modified_at", "read_at", "signed", "personal", "zillow", "database",
            "bonus", "sheet_personal", "sheet_zillow", "sheet_database", "problem"]
    cur.execute(f"SELECT {', '.join(cols)} FROM drive_contracts ORDER BY agent, signed NULLS LAST")
    return [dict(zip(cols, r)) for r in cur.fetchall()]
