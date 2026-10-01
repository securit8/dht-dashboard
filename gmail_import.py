"""Gmail: read-only import of Compass agent remittance emails and their PDF statements.

Read-only by design: the only scope is gmail.readonly and the only calls are the profile,
a message search, reading the matching messages and downloading their PDF attachments.
Only emails that match REMITTANCE_QUERY are ever opened. From each one the dashboard keeps
the payment line items (bill number, description, amount) and the statement's text; the PDF
itself is not stored. Tokens are encrypted in Postgres. Disconnecting revokes the token at
Google and deletes the account's tokens and everything imported from it.

Settings (Render environment):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  OAuth client from Google Cloud (Web application)
  GMAIL_TOKEN_KEY                         optional; encrypts the stored tokens (else QBO_TOKEN_KEY)
"""
import base64
import hashlib
import io
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import psycopg2
import requests
from cryptography.fernet import Fernet, InvalidToken

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
# The only emails the dashboard opens
# Compass remittances: "Upcoming Payment ..." (Compass pays the team directly) and
# "Agent Remittance Paid by Escrow/Title #address - date" (escrow paid at closing; one per deal)
REMITTANCE_QUERY = ('from:DoNotReply@compass.com (subject:"Upcoming Payment Compass Agent Remittance" '
                    'OR subject:"Agent Remittance Paid by Escrow")')
# Assistant contribution payments, not invoices: "Assist Contr Sep 30", "Jul 25 Asst Cont 1 of 2"
ASSIST_RE = re.compile(r"\b(assist\s*contr?|asst\.?\s*cont)", re.I)
AMOUNT_RE = re.compile(r"\(?-?\$\s?[\d,]+\.\d{2}\)?")


class NeedsReconnect(Exception):
    """The refresh token is expired or revoked; someone has to click Connect again."""


class GmailError(Exception):
    pass


def _token_key():
    return os.environ.get("GMAIL_TOKEN_KEY") or os.environ.get("QBO_TOKEN_KEY")


def configured():
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET") and _token_key())


def _fernet():
    key = hashlib.sha256(("gmail:" + _token_key()).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _enc(s):
    return _fernet().encrypt(s.encode()).decode()


def _dec(s):
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken:
        raise NeedsReconnect("Stored Gmail token can't be decrypted (token key changed?)")


def _request(method, url, retries=3, **kw):
    """HTTP with a few retries for network errors, 429 and 5xx."""
    kw.setdefault("timeout", 30)
    r = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, **kw)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
        else:
            if r.status_code != 429 and r.status_code < 500:
                return r
            if attempt == retries - 1:
                return r
        time.sleep(2 ** attempt)
    return r


# ---------------------------------------------------------------- storage

def ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gmail_accounts (
            email TEXT PRIMARY KEY,
            access_token TEXT, access_expires_at TIMESTAMPTZ, refresh_token TEXT,
            connected_at TIMESTAMPTZ, last_pull_at TIMESTAMPTZ,
            needs_reconnect BOOLEAN DEFAULT false, last_error TEXT
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS compass_payments (
            message_id TEXT PRIMARY KEY,
            account TEXT NOT NULL,
            payment_no TEXT,
            paid_on DATE,
            received_at TIMESTAMPTZ,
            total NUMERIC,
            subject TEXT,
            pdf_name TEXT,
            pdf_text TEXT,
            parse_note TEXT,
            pulled_at TIMESTAMPTZ DEFAULT now()
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS compass_payment_items (
            message_id TEXT NOT NULL REFERENCES compass_payments(message_id) ON DELETE CASCADE,
            line_no INT NOT NULL,
            bill_no TEXT,
            description TEXT,
            amount NUMERIC,
            is_assist BOOLEAN,
            source TEXT,
            PRIMARY KEY (message_id, line_no)
        )""")
    cur.execute("ALTER TABLE compass_payments ADD COLUMN IF NOT EXISTS ytd_income NUMERIC")
    cur.execute("ALTER TABLE compass_payments ADD COLUMN IF NOT EXISTS kind TEXT")  # payment | escrow
    cur.execute("ALTER TABLE compass_payments ADD COLUMN IF NOT EXISTS property TEXT")
    for col, kind in (("bill_date", "DATE"), ("close_price", "NUMERIC"), ("gross", "NUMERIC"), ("components", "TEXT")):
        cur.execute(f"ALTER TABLE compass_payment_items ADD COLUMN IF NOT EXISTS {col} {kind}")
    cur.execute("ALTER TABLE gmail_accounts ADD COLUMN IF NOT EXISTS import_started_at TIMESTAMPTZ")


def _db(database_url):
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    ensure_tables(cur)
    return conn, cur


def accounts(cur):
    """Connected accounts for the page (no tokens)."""
    ensure_tables(cur)
    cur.execute("""SELECT a.email, a.connected_at, a.last_pull_at, a.needs_reconnect, a.last_error,
                          (SELECT COUNT(*) FROM compass_payments p WHERE p.account = a.email),
                          a.import_started_at > now() - interval '15 minutes'
                   FROM gmail_accounts a ORDER BY a.connected_at""")
    keys = ["email", "connected_at", "last_pull_at", "needs_reconnect", "last_error", "payments", "importing"]
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def _mark_error(database_url, email, message, reconnect=False):
    conn, cur = _db(database_url)
    cur.execute("""UPDATE gmail_accounts SET last_error = %s, needs_reconnect = needs_reconnect OR %s
                   WHERE email = %s""", (message[:500], reconnect, email))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- OAuth

def authorize_url(redirect_uri, state):
    return AUTH_URL + "?" + urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent select_account", "state": state})


def _token_call(data):
    r = _request("POST", TOKEN_URL, data={**data, "client_id": os.environ["GOOGLE_CLIENT_ID"],
                                          "client_secret": os.environ["GOOGLE_CLIENT_SECRET"]})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 400 and body.get("error") == "invalid_grant":
        raise NeedsReconnect("Gmail authorization expired or was revoked (invalid_grant)")
    if r.status_code >= 400:
        raise GmailError(f"Google token error HTTP {r.status_code}: {body.get('error_description') or body.get('error') or r.text[:200]}")
    return body


def connect(database_url, code, redirect_uri):
    """Finish the Connect flow: swap the code for tokens, find the mailbox address, store encrypted."""
    tokens = _token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    if not tokens.get("refresh_token"):
        raise GmailError("Google did not return a refresh token. Remove the app's access in your Google "
                         "account (Security > Third-party access) and connect again.")
    scopes = set((tokens.get("scope") or "").split())
    if SCOPE not in scopes:
        raise GmailError("Read access to Gmail was not granted. Connect again and tick the Gmail box.")
    r = _request("GET", f"{API}/profile", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    if r.status_code >= 400:
        raise GmailError(f"Could not read the mailbox address (HTTP {r.status_code})")
    email = r.json()["emailAddress"].lower()
    conn, cur = _db(database_url)
    cur.execute("""
        INSERT INTO gmail_accounts (email, access_token, access_expires_at, refresh_token, connected_at,
                                    needs_reconnect, last_error)
        VALUES (%s, %s, %s, %s, now(), false, NULL)
        ON CONFLICT (email) DO UPDATE SET access_token = EXCLUDED.access_token,
            access_expires_at = EXCLUDED.access_expires_at, refresh_token = EXCLUDED.refresh_token,
            connected_at = now(), needs_reconnect = false, last_error = NULL""",
                (email, _enc(tokens["access_token"]),
                 datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 120),
                 _enc(tokens["refresh_token"])))
    conn.commit()
    conn.close()
    return email


def _access_token(database_url, email, force_refresh=False):
    conn, cur = _db(database_url)
    cur.execute("""SELECT access_token, access_expires_at, refresh_token, needs_reconnect
                   FROM gmail_accounts WHERE email = %s""", (email,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise NeedsReconnect(f"{email} is not connected")
    access, access_exp, refresh, needs = row
    if needs:
        conn.close()
        raise NeedsReconnect(f"{email} needs to be reconnected")
    if not force_refresh and access and access_exp and access_exp > datetime.now(timezone.utc):
        conn.close()
        return _dec(access)
    try:
        tokens = _token_call({"grant_type": "refresh_token", "refresh_token": _dec(refresh)})
    except NeedsReconnect as e:
        conn.close()
        _mark_error(database_url, email, str(e), reconnect=True)
        raise
    cur.execute("UPDATE gmail_accounts SET access_token = %s, access_expires_at = %s WHERE email = %s",
                (_enc(tokens["access_token"]),
                 datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 120), email))
    conn.commit()
    conn.close()
    return tokens["access_token"]


def _get(database_url, email, path, params=None):
    token = _access_token(database_url, email)
    refreshed, waits = False, 0
    while True:
        r = _request("GET", f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401 and not refreshed:
            token, refreshed = _access_token(database_url, email, force_refresh=True), True
            continue
        # Gmail answers 403 (not 429) when requests come too fast: wait and try again
        if r.status_code == 403 and re.search(r"rateLimitExceeded|userRateLimitExceeded|quota", r.text, re.I):
            if waits >= 6:
                raise GmailError("Gmail rate limit: too many requests. The next update continues where this one stopped.")
            time.sleep(min(60, 5 * 2 ** waits))
            waits += 1
            continue
        if r.status_code in (401, 403):
            msg = f"Gmail refused access (HTTP {r.status_code})"
            _mark_error(database_url, email, msg, reconnect=True)
            raise NeedsReconnect(msg)
        if r.status_code >= 400:
            raise GmailError(f"Gmail API error HTTP {r.status_code} on {path.split('/')[0]}: {r.text[:200]}")
        return r.json()


def disconnect(database_url, email):
    """Revoke the token at Google (best effort) and delete the account and everything imported from it."""
    conn, cur = _db(database_url)
    cur.execute("SELECT refresh_token FROM gmail_accounts WHERE email = %s", (email,))
    row = cur.fetchone()
    if row and row[0] and configured():
        try:
            _request("POST", REVOKE_URL, data={"token": _dec(row[0])}, retries=2)
        except Exception:  # noqa: BLE001 - already revoked is fine
            pass
    cur.execute("DELETE FROM compass_payments WHERE account = %s", (email,))
    cur.execute("DELETE FROM gmail_accounts WHERE email = %s", (email,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- parsing

def _money(s):
    s = s.strip()
    neg = s.startswith("(") or s.startswith("-")
    v = float(re.sub(r"[^\d.]", "", s) or 0)
    return -v if neg else v


def _b64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _walk(part):
    yield part
    for p in part.get("parts") or []:
        yield from _walk(p)


def pdf_text(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def body_items(body):
    """Line items from the email's own table: | Bill #123 | Description | $1,234.56 |"""
    items = []
    for line in body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0].lower().startswith("bill"):
            amount = next((_money(c) for c in cells[2:] if AMOUNT_RE.fullmatch(c.replace(" ", ""))), None)
            items.append({"bill_no": re.sub(r"(?i)bill\s*#?", "", cells[0]).strip(), "description": cells[1],
                          "amount": amount})
    return items


MONEY_RE = re.compile(r"\(\$[\d,]+\.\d{2}\)|-?\$[\d,]+\.\d{2}")  # ($62.50) = -62.50 on 2024 statements
LABEL_RE = re.compile(r"\b(Commission Income|Referral Income|Compass Resource Fee|External TC Fee|Title Clearing|"
                      r"Paid By|Flat Transaction|Bonus|Commission)\b")
BILL_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s+Bill\s*#\s*(\d+)")
# Reference codes in front of the description: "asc-1109-538-", "565700 - 45519 - "
REF_RE = re.compile(r"^(asc-\d+-\d+-|\d{6}\s*-\s*\d+\s*-\s*)", re.I)


def _address(lines):
    """The property address in front of each line's label: the words all lines share, or the text
    before the first known label when the bill has one line."""
    texts = [MONEY_RE.split(l)[0].strip() for l in lines]
    if len(texts) > 1:
        words = [t.split() for t in texts]
        common = []
        for group in zip(*words):
            if len(set(group)) > 1:
                break
            common.append(group[0])
        shared = " ".join(common)
        m = LABEL_RE.search(shared)  # two labels can start with the same word ("Compass ...")
        return shared[:m.start()].strip() if m else shared
    m = LABEL_RE.search(texts[0]) if texts else None
    return texts[0][:m.start()].strip() if m else ""


def parse_statement(text):
    """Compass remittance statement -> amount paid, YTD income and one item per bill.
    Columns are either Date/Close Date, Internal Reference, Description or Property Address,
    Final Close Price, Memo or Description, Amount (2025+) or Date, Type, Description, Memo, Amount (2024).
    Each bill lists labeled amounts (Commission Income, Referral Income, fees as negatives)."""
    flat = " ".join(text.split())
    # Escrow statements have no "Amount Paid"; their total is "Paid by Title/Escrow $X" after the table
    paid = (re.search(r"Amount Paid\s+(-?\$[\d,]+\.\d{2})", flat)
            or re.search(r"Payment Questions:.*?Paid by Title/Escrow\s+(\$[\d,]+\.\d{2})", flat))
    ytd = re.search(r"YTD Income\s+(-?\$[\d,]+\.\d{2})", flat)
    out = {"amount_paid": _money(paid.group(1)) if paid else None,
           "ytd_income": _money(ytd.group(1)) if ytd else None, "items": []}
    start = re.search(r"\bAmount\s+(?=\d{1,2}/\d{1,2}/\d{4}\s+Bill)", flat)
    if not start:
        return out
    # The table ends at "Sub-total" (payments) or at the contact lines / YTD figures (escrow statements)
    ends = [i for i in (flat.find(k, start.end()) for k in
                        ("Sub-total", "Commission Payment Questions", "Non-Commission Payment", "Incentive Payment",
                         "Invoicing Related", "YTD ")) if i > 0]
    end = min(ends) if ends else len(flat)
    header = flat[:start.end()]
    has_price = "Final Close Price" in header[header.rfind("Amount Paid"):]
    body = flat[start.end():end]
    bills = list(BILL_RE.finditer(body))
    # 2024 statements repeat "date Bill #N address" on every line of the same bill: merge them
    groups = []
    for i, m in enumerate(bills):
        seg = body[m.end():bills[i + 1].start() if i + 1 < len(bills) else len(body)].strip()
        if groups and groups[-1][0].group(2) == m.group(2):
            groups[-1][1].append(seg)
        else:
            groups.append((m, [seg]))
    for m, segs in groups:
        chunk = " ".join(segs)
        tokens = list(MONEY_RE.finditer(chunk))
        if not tokens:
            continue
        close_price, parts = None, []
        if has_price:  # the first amount is the close price; the labeled amounts follow it
            desc = REF_RE.sub("", chunk[:tokens[0].start()].strip()).strip()
            close_price, prev = _money(tokens[0].group(0)), tokens[0].end()
            for t in tokens[1:]:
                parts.append((chunk[prev:t.start()].strip() or "Amount", _money(t.group(0))))
                prev = t.end()
        else:  # "address label $amount" per line: the address is what every line of the bill starts with
            lines = [REF_RE.sub("", s).strip() for s in segs]
            desc = _address(lines)
            for line in lines:
                prev = len(desc) if desc and line.startswith(desc) else 0
                for t in MONEY_RE.finditer(line):
                    parts.append((line[prev:t.start()].strip() or desc or "Amount", _money(t.group(0))))
                    prev = t.end()
            desc = desc or (parts[0][0] if parts else "")
        gross = sum(v for k, v in parts if re.search(r"income", k, re.I))
        out["items"].append({
            "bill_no": m.group(2), "date": datetime.strptime(m.group(1), "%m/%d/%Y").date(), "description": desc,
            "close_price": close_price, "gross": gross if gross else None,
            "amount": round(sum(v for _, v in parts), 2),
            "components": "; ".join(f"{k} {v:,.2f}" for k, v in parts),
            "is_assist": bool(ASSIST_RE.search(desc) or any(ASSIST_RE.search(k) for k, _ in parts))})
    return out


def _total(subject, body, text):
    for s in (subject, body):
        m = re.search(r"for\s+(\$[\d,]+\.\d{2})", s or "")
        if m:
            return _money(m.group(1))
    m = re.search(r"(?i)(?:total|payment amount|amount paid|net pay)[^\n$]*(\$\s?[\d,]+\.\d{2})", text or "")
    return _money(m.group(1)) if m else None


# ---------------------------------------------------------------- import

def _header(msg, name):
    return next((h["value"] for h in msg.get("payload", {}).get("headers", []) if h["name"].lower() == name), "")


def import_message(database_url, email, message_id):
    msg = _get(database_url, email, f"messages/{message_id}", {"format": "full"})
    subject = _header(msg, "subject")
    try:
        received = parsedate_to_datetime(_header(msg, "date"))
    except (TypeError, ValueError):
        received = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, timezone.utc)
    body, pdf_name, text = "", None, ""
    for part in _walk(msg.get("payload", {})):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if mime == "text/plain" and data and not part.get("filename"):
            body += _b64(data).decode("utf-8", "replace")
        elif part.get("filename", "").lower().endswith(".pdf") or mime == "application/pdf":
            att = (part.get("body") or {}).get("attachmentId")
            raw = _b64(_get(database_url, email, f"messages/{message_id}/attachments/{att}")["data"]) if att \
                else (_b64(data) if data else b"")
            if raw:
                pdf_name = part.get("filename")
                try:
                    text += pdf_text(raw)
                except Exception as e:  # noqa: BLE001 - keep the email even if the PDF can't be read
                    text += f"[PDF could not be read: {e}]"
    stmt = parse_statement(text)
    items, source = stmt["items"], "pdf"
    if not items:  # no readable statement: fall back to the email's own table
        items, source = body_items(body), "email"
    escrow = "paid by escrow" in subject.lower()
    if escrow:  # "Agent Remittance Paid by Escrow/Title #26755 Hemet St Hemet, CA 92544 - 9/14/2026"
        m = re.search(r"#\s*(.+?)\s+-\s+(\d{1,2}/\d{1,2}/\d{4})\s*$", subject)
        prop = (re.search(r"commission for (.+?) on \d{1,2}/\d{1,2}/\d{4}", body) or m)
        prop = prop.group(1).strip() if prop else None
        paid, payment = (re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*$", subject), None)
    else:
        prop = None
        paid = re.search(r"sent on (\d{1,2}/\d{1,2}/\d{4})", body) or re.search(r"(\d{1,2}/\d{1,2}/\d{4})", subject)
        payment = re.search(r"#+(\d+)", subject)
    note = None if items else "No line items found; see the statement text."
    total = stmt["amount_paid"] if stmt["amount_paid"] is not None else _total(subject, body, text)
    conn, cur = _db(database_url)
    cur.execute("""
        INSERT INTO compass_payments (message_id, account, payment_no, paid_on, received_at, total, ytd_income,
                                      subject, pdf_name, pdf_text, parse_note, kind, property, pulled_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (message_id) DO UPDATE SET total = EXCLUDED.total, ytd_income = EXCLUDED.ytd_income,
            paid_on = EXCLUDED.paid_on, pdf_text = EXCLUDED.pdf_text, parse_note = EXCLUDED.parse_note,
            kind = EXCLUDED.kind, property = EXCLUDED.property, payment_no = EXCLUDED.payment_no, pulled_at = now()""",
                (message_id, email, payment.group(1) if payment else None,
                 datetime.strptime(paid.group(1), "%m/%d/%Y").date() if paid else received.date(),
                 received, total, stmt["ytd_income"], subject, pdf_name, text, note,
                 "escrow" if escrow else "payment", prop))
    cur.execute("DELETE FROM compass_payment_items WHERE message_id = %s", (message_id,))
    for n, it in enumerate(items, 1):
        cur.execute("""INSERT INTO compass_payment_items (message_id, line_no, bill_no, description, amount,
                                                          is_assist, source, bill_date, close_price, gross, components)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (message_id, n, it["bill_no"], it["description"], it["amount"],
                     it.get("is_assist", bool(ASSIST_RE.search(it["description"] or ""))), source,
                     it.get("date"), it.get("close_price"), it.get("gross"), it.get("components")))
    conn.commit()
    conn.close()


def pull(database_url, email, reimport=False):
    """Import every matching email not imported yet (all of them with reimport). Returns the count."""
    ids, page = [], None
    while True:
        params = {"q": REMITTANCE_QUERY, "maxResults": 100, "includeSpamTrash": "false"}
        if page:
            params["pageToken"] = page
        res = _get(database_url, email, "messages", params)
        ids += [m["id"] for m in res.get("messages", [])]
        page = res.get("nextPageToken")
        if not page:
            break
    conn, cur = _db(database_url)
    cur.execute("SELECT message_id FROM compass_payments WHERE account = %s", (email,))
    done = {r[0] for r in cur.fetchall()}
    conn.close()
    todo = ids if reimport else [i for i in ids if i not in done]
    for mid in todo:
        import_message(database_url, email, mid)
        time.sleep(0.25)  # stay under Gmail's per-user rate limit
    conn, cur = _db(database_url)
    cur.execute("UPDATE gmail_accounts SET last_pull_at = now(), last_error = NULL WHERE email = %s", (email,))
    conn.commit()
    conn.close()
    return len(todo), len(ids)


def pull_in_background(database_url, email, reimport=False, retry=False):
    """Run pull() in a background thread so the page answers at once (a full import reads every PDF
    and takes longer than the web server's request timeout). Returns False if one is already running."""
    conn, cur = _db(database_url)
    cur.execute("""UPDATE gmail_accounts SET import_started_at = now(),
                          needs_reconnect = CASE WHEN %s THEN false ELSE needs_reconnect END
                   WHERE email = %s
                   AND (import_started_at IS NULL OR import_started_at < now() - interval '15 minutes')""",
                (retry, email))
    started = cur.rowcount == 1
    conn.commit()
    conn.close()
    if not started:
        return False

    def run():
        try:
            new, total = pull(database_url, email, reimport=reimport)
            print(f"Gmail {email}: imported {new} of {total} remittance emails.")
        except Exception as e:  # noqa: BLE001 - shown on the page
            _mark_error(database_url, email, f"Import failed: {e}", reconnect=isinstance(e, NeedsReconnect))
        finally:
            c, k = _db(database_url)
            k.execute("UPDATE gmail_accounts SET import_started_at = NULL WHERE email = %s", (email,))
            c.commit()
            c.close()

    threading.Thread(target=run, daemon=True).start()
    return True


def payments(cur):
    """Every imported payment with its line items, newest first."""
    ensure_tables(cur)
    cur.execute("""SELECT p.message_id, p.account, p.payment_no, p.paid_on, p.total, p.pdf_name, p.parse_note,
                          p.ytd_income, p.kind, p.property, p.received_at,
                          COALESCE(json_agg(json_build_object('bill_no', i.bill_no, 'description', i.description,
                                   'amount', i.amount, 'is_assist', i.is_assist, 'source', i.source,
                                   'bill_date', i.bill_date, 'close_price', i.close_price, 'gross', i.gross,
                                   'components', i.components)
                                   ORDER BY i.line_no) FILTER (WHERE i.line_no IS NOT NULL), '[]')
                   FROM compass_payments p LEFT JOIN compass_payment_items i ON i.message_id = p.message_id
                   GROUP BY p.message_id ORDER BY p.paid_on DESC, p.received_at DESC""")
    keys = ["message_id", "account", "payment_no", "paid_on", "total", "pdf_name", "parse_note", "ytd_income", "kind", "property", "received_at", "items"]
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def payment_text(cur, message_id):
    ensure_tables(cur)
    cur.execute("SELECT subject, pdf_name, pdf_text FROM compass_payments WHERE message_id = %s", (message_id,))
    row = cur.fetchone()
    return dict(zip(["subject", "pdf_name", "pdf_text"], row)) if row else None


def cron_pull(database_url):
    """Called from the cron job: import new remittances for every connected mailbox, never raise."""
    if not configured():
        print("Gmail import skipped: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set.")
        return
    conn, cur = _db(database_url)
    conn.commit()
    cur.execute("SELECT email FROM gmail_accounts WHERE NOT needs_reconnect")
    emails = [r[0] for r in cur.fetchall()]
    conn.close()
    for email in emails:
        try:
            new, total = pull(database_url, email)
            print(f"Gmail {email}: imported {new} new Compass remittances ({total} matching emails).")
        except NeedsReconnect as e:
            print(f"Gmail {email} needs reconnecting: {e}")
        except Exception as e:  # noqa: BLE001 - report and carry on
            _mark_error(database_url, email, f"Import failed: {e}")
            print(f"Gmail {email} import failed: {e}")


# ---------------------------------------------------------------- closed deals vs receipts

ORDINALS = {"first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th", "sixth": "6th",
            "seventh": "7th", "eighth": "8th", "ninth": "9th", "tenth": "10th"}
ALIASES = {"mt": "mount", "ste": "suite", "n": "north", "s": "south", "e": "east", "w": "west"}
# Words that don't tell two addresses apart
GENERIC = {"st", "street", "ave", "avenue", "rd", "road", "dr", "drive", "ln", "lane", "ct", "court", "way", "blvd",
           "pl", "place", "cir", "circle", "unit", "apt", "suite", "san", "diego", "sd", "ca", "adj", "north", "south",
           "east", "west", "the", "de", "la", "del", "el", "vista", "chula", "city"}


def _addr_key(text):
    """(street number, set of distinctive words) for fuzzy address matching."""
    words = [ORDINALS.get(w, ALIASES.get(w, w)) for w in re.findall(r"[a-z0-9]+", (text or "").lower())]
    words = [w for w in words if not re.fullmatch(r"9\d{4}", w)]  # zip codes
    if words and words[0] == "adj":
        words = words[1:]
    number = words[0] if words and words[0].isdigit() else None
    return number, {w for w in words[1:] if w not in GENERIC and not w.isdigit()}


def _street(text):
    """First distinctive word of the street name ("caminito", "enfield"), for typo suggestions;
    the city words later in the address must not count as the same street."""
    words = [ORDINALS.get(w, ALIASES.get(w, w)) for w in re.findall(r"[a-z0-9]+", (text or "").lower())]
    words = [w for w in words if w != "adj"]
    rest = words[1:] if words and words[0].isdigit() else words
    return next((w for w in rest if w not in GENERIC and not w.isdigit()), None)


def _digits_close(a, b):
    """True when two street numbers look like a typo of each other: same digits in another order,
    or one digit different (7832 / 7382, 1832 / 1831, 1607 / 1670)."""
    if not a or not b or a == b:
        return False
    if sorted(a) == sorted(b):
        return True
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


_memo = threading.local()


def memo_start():
    """Reuse deal_receipts results until memo_stop (one page build asks for the same years several times)."""
    _memo.d = {}


def memo_stop():
    _memo.d = None


def deal_receipts(cur, start, end):
    memo = getattr(_memo, "d", None)
    if memo is not None:
        if (start, end) not in memo:
            memo[(start, end)] = _deal_receipts(cur, start, end)
        deals, unmatched = memo[(start, end)]
        return list(deals), list(unmatched)
    return _deal_receipts(cur, start, end)


def _deal_receipts(cur, start, end):
    """CTE closed deals in [start, end) with the Compass receipts that match them (same street number,
    within 120 days, sharing a street word when there is a choice). Deals around the period are matched
    too, so a receipt for a deal of the next or previous year doesn't show as unmatched.
    A deal without a receipt gets `suggestions`: unmatched receipts that look like the same deal with a
    typo in the CTE street number (same street, close in time) or in the date (same address, farther apart).
    Returns (deals in the period, receipts dated in the period that match no deal)."""
    ensure_tables(cur)
    lo, hi = start - timedelta(days=150), end + timedelta(days=150)
    cur.execute("""SELECT file_year, row_num, address, close_date, gci, primary_agent, deal_type, sale_price
                   FROM cte_deals WHERE status = 'Closed' AND close_date >= %s AND close_date < %s
                   ORDER BY close_date DESC""", (lo, hi))
    deals = [dict(zip(["file_year", "row_num", "address", "close_date", "gci", "agent", "deal_type", "sale_price"], r),
                  receipts=[], suggestions=[]) for r in cur.fetchall()]
    for d in deals:
        d["key"] = _addr_key(d["address"])
    cur.execute("""SELECT p.message_id, p.kind, p.paid_on, p.property, i.description, i.bill_no, i.bill_date,
                          i.close_price, i.gross, i.amount, i.components
                   FROM compass_payment_items i JOIN compass_payments p ON p.message_id = i.message_id
                   WHERE NOT i.is_assist""")
    keys = ["message_id", "kind", "paid_on", "property", "description", "bill_no", "bill_date", "close_price",
            "gross", "amount", "components"]
    receipts = [dict(zip(keys, r)) for r in cur.fetchall()]
    unmatched = []
    for rc in receipts:
        when = rc["bill_date"] or rc["paid_on"]
        rc["key"] = _addr_key(rc["property"] or rc["description"])
        number, words = rc["key"]
        best = None
        for d in deals:
            dn, dw = d["key"]
            if not number or dn != number or not d["close_date"] or not when:
                continue
            gap = abs((when - d["close_date"]).days)
            if gap > 120:
                continue
            # buyer and listing sides are separate CTE rows with the same address: spread receipts over them
            score = (len(words & dw) > 0, -(gap // 10), -len(d["receipts"]), -gap)
            if best is None or score > best[0]:
                best = (score, d)
        if best:
            best[1]["receipts"].append(rc)
        elif when:
            unmatched.append(rc)
    for d in deals:
        if d["receipts"] or not d["close_date"]:
            continue
        dn, street = d["key"][0], _street(d["address"])
        for rc in unmatched:
            when = rc["bill_date"] or rc["paid_on"]
            number = rc["key"][0]
            same_street = street and street == _street(rc["property"] or rc["description"])
            gap = abs((when - d["close_date"]).days)
            # same street: a number that looks like a typo, or any other number when it closed within two weeks
            if same_street and dn != number and (_digits_close(dn, number) and gap <= 120 or gap <= 14):
                d["suggestions"].append(dict(rc, why="street number differs"))
            elif same_street and dn == number and 120 < gap <= 400:
                d["suggestions"].append(dict(rc, why="close date differs"))
    in_period = [d for d in deals if d["close_date"] and start <= d["close_date"] < end]
    return in_period, [rc for rc in unmatched if start <= (rc["bill_date"] or rc["paid_on"]) < end]


def monthly_vs_books(cur, year):
    """Month by month: Compass's YTD Income from the latest statement of the month vs QuickBooks
    income summed from January (qbo_pnl, pulled from the QuickBooks API)."""
    ensure_tables(cur)
    # Highest YTD in the month: statements for late-December closings are often run in January and
    # already show the new year's (small) YTD, so the latest one isn't always the right one
    # by the day the statement was sent: YTD Income is as of that day, even for an older closing
    cur.execute("""SELECT date_trunc('month', received_at AT TIME ZONE 'America/Los_Angeles')::date, MAX(ytd_income)
                   FROM compass_payments WHERE ytd_income IS NOT NULL
                     AND EXTRACT(YEAR FROM received_at AT TIME ZONE 'America/Los_Angeles') = %s
                   GROUP BY 1""", (year,))
    compass = dict(cur.fetchall())
    books = {}
    cur.execute("SELECT to_regclass('qbo_pnl') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("SELECT month, income FROM qbo_pnl WHERE EXTRACT(YEAR FROM month) = %s ORDER BY month", (year,))
        books = {m: float(v or 0) for m, v in cur.fetchall()}
    rows, c_ytd, b_ytd, prev_c = [], None, 0.0, 0.0
    for month in range(1, 13):
        m = date(year, month, 1)
        if m > date.today():
            break
        if m in compass:
            c_ytd = max(float(compass[m]), c_ytd or 0.0)  # YTD only grows within a year
        b_ytd += books.get(m, 0.0)
        c_month = (c_ytd - prev_c) if c_ytd is not None else None
        rows.append({"month": m, "compass_ytd": c_ytd, "books_ytd": b_ytd if books else None,
                     "compass_month": c_month, "books_month": books.get(m) if books else None,
                     "statement": m in compass})
        if c_ytd is not None:
            prev_c = c_ytd
    return rows


def books_match(cur, year):
    """Every QuickBooks income entry in `year` matched to a Compass payment (same amount within $1,
    within 3 weeks; or two payments booked as one deposit, or one payment split in two). What's left on
    either side is what makes QuickBooks and Compass differ."""
    cur.execute("SELECT to_regclass('qbo_income_txns') IS NOT NULL")
    if not cur.fetchone()[0]:
        return None
    lo, hi = date(year - 1, 12, 1), date(year + 1, 1, 31)
    cur.execute("""SELECT id, txn_date, txn_type, doc_num, name, memo, account, amount FROM qbo_income_txns
                   WHERE txn_date >= %s AND txn_date <= %s ORDER BY txn_date""", (lo, hi))
    qb = [dict(zip(["id", "date", "type", "doc", "name", "memo", "account", "amount"], r)) for r in cur.fetchall()]
    if not qb:
        return None
    cur.execute("""SELECT message_id, COALESCE(paid_on, (received_at AT TIME ZONE 'America/Los_Angeles')::date), total,
                          kind, property, payment_no, subject
                   FROM compass_payments WHERE total IS NOT NULL
                     AND COALESCE(paid_on, (received_at AT TIME ZONE 'America/Los_Angeles')::date) BETWEEN %s AND %s""", (lo, hi))
    cp = [dict(zip(["id", "date", "amount", "kind", "property", "payment_no", "subject"], r)) for r in cur.fetchall()]
    for x in qb + cp:
        x["amount"] = float(x["amount"])
        x["match"] = None
    days = lambda a, b: abs((a - b).days)
    # one to one: same amount, nearest date
    for t in qb:
        best = None
        for p in cp:
            if p["match"] is None and abs(p["amount"] - t["amount"]) <= 1.0 and days(p["date"], t["date"]) <= 21:
                if best is None or days(p["date"], t["date"]) < days(best["date"], t["date"]):
                    best = p
        if best:
            t["match"], best["match"] = [best], [t]
    # two Compass payments booked as one QuickBooks entry, or one payment booked as two entries
    for big, small in ((qb, cp), (cp, qb)):
        for t in big:
            if t["match"]:
                continue
            free = [p for p in small if p["match"] is None and days(p["date"], t["date"]) <= 21]
            pair = next(((a, b) for i, a in enumerate(free) for b in free[i + 1:]
                         if abs(a["amount"] + b["amount"] - t["amount"]) <= 1.0), None)
            if pair:
                t["match"] = list(pair)
                for p in pair:
                    p["match"] = [t]
    inyear = lambda x: x["date"].year == year
    qb_y, cp_y = [t for t in qb if inyear(t)], [p for p in cp if inyear(p)]
    return {"qb_only": [t for t in qb_y if not t["match"]], "compass_only": [p for p in cp_y if not p["match"]],
            "matched": sum(1 for t in qb_y if t["match"]), "qb_count": len(qb_y), "compass_count": len(cp_y),
            "qb_total": sum(t["amount"] for t in qb_y), "compass_total": sum(p["amount"] for p in cp_y),
            "qb_only_total": sum(t["amount"] for t in qb_y if not t["match"]),
            "compass_only_total": sum(p["amount"] for p in cp_y if not p["match"]),
            "by_account": _by_account(qb_y)}


def _by_account(rows):
    out = {}
    for t in rows:
        out[t["account"] or "(no account)"] = out.get(t["account"] or "(no account)", 0.0) + t["amount"]
    return sorted(out.items(), key=lambda x: -x[1])
