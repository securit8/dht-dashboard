"""QuickBooks Online: OAuth connection and the Profit & Loss pull.

Read-only by design: the only API calls are the Profit & Loss report and CompanyInfo
(both GET). Tokens are encrypted in Postgres with QBO_TOKEN_KEY. Disconnecting revokes
the token at Intuit and deletes the connection and every stored QuickBooks number.

Settings (Render environment group "quickbooks"):
  QBO_CLIENT_ID, QBO_CLIENT_SECRET  keys from the Intuit developer app
  QBO_TOKEN_KEY                     any long random string; encrypts the stored tokens
  QBO_ENV                           "sandbox" (Intuit's test company) or "production"
"""
import base64
import hashlib
import os
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2
import requests
from cryptography.fernet import Fernet, InvalidToken

SCOPE = "com.intuit.quickbooks.accounting"
DISCOVERY = {"sandbox": "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration",
             "production": "https://developer.api.intuit.com/.well-known/openid_configuration"}
API_BASE = {"sandbox": "https://sandbox-quickbooks.api.intuit.com",
            "production": "https://quickbooks.api.intuit.com"}
MINOR_VERSION = "75"
PNL_MONTHS = 36  # months of history pulled each time


class NeedsReconnect(Exception):
    """The refresh token is expired or revoked; someone has to click Connect again."""


def env():
    return "production" if os.environ.get("QBO_ENV", "sandbox").strip().lower() == "production" else "sandbox"


def configured():
    return all(os.environ.get(k) for k in ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_TOKEN_KEY"))


def _fernet():
    key = hashlib.sha256(os.environ["QBO_TOKEN_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _enc(s):
    return _fernet().encrypt(s.encode()).decode()


def _dec(s):
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken:
        raise NeedsReconnect("Stored token can't be decrypted (QBO_TOKEN_KEY changed?)")


_discovery_cache = {}


def discovery():
    """Intuit's current OAuth endpoints (their discovery document), cached for a day."""
    e = env()
    cached = _discovery_cache.get(e)
    if cached and cached[0] > time.time():
        return cached[1]
    doc = _request("GET", DISCOVERY[e]).json()
    _discovery_cache[e] = (time.time() + 86400, doc)
    return doc


def _request(method, url, retries=3, **kw):
    """HTTP with a few retries for network errors, 429 and 5xx (never for 4xx auth errors)."""
    kw.setdefault("timeout", 30)
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
        CREATE TABLE IF NOT EXISTS qbo_connection (
            id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            env TEXT, realm_id TEXT, company_name TEXT,
            access_token TEXT, access_expires_at TIMESTAMPTZ,
            refresh_token TEXT, refresh_expires_at TIMESTAMPTZ,
            connected_at TIMESTAMPTZ, last_pull_at TIMESTAMPTZ,
            needs_reconnect BOOLEAN DEFAULT false, last_error TEXT
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qbo_pnl (
            month DATE PRIMARY KEY,
            income NUMERIC, cogs NUMERIC, gross_profit NUMERIC, expenses NUMERIC,
            net_operating_income NUMERIC, other_income NUMERIC, other_expenses NUMERIC,
            net_income NUMERIC, pulled_at TIMESTAMPTZ DEFAULT now()
        )""")


def _db(database_url):
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    ensure_tables(cur)
    return conn, cur


def status(cur):
    """Connection state for the QuickBooks page (no tokens)."""
    ensure_tables(cur)
    cur.execute("""SELECT env, realm_id, company_name, connected_at, last_pull_at, needs_reconnect, last_error,
                          refresh_expires_at FROM qbo_connection WHERE id = 1""")
    row = cur.fetchone()
    if not row:
        return None
    keys = ["env", "realm_id", "company_name", "connected_at", "last_pull_at", "needs_reconnect", "last_error",
            "refresh_expires_at"]
    return dict(zip(keys, row))


def pnl_rows(cur, months=24):
    ensure_tables(cur)
    cur.execute("""SELECT month, income, cogs, gross_profit, expenses, net_operating_income, other_income,
                          other_expenses, net_income
                   FROM qbo_pnl ORDER BY month DESC LIMIT %s""", (months,))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # Newest first; drop the empty months before the books start
    while rows and not any(rows[-1][k] for k in cols if k != "month"):
        rows.pop()
    return rows


def _mark_error(database_url, message, reconnect=False):
    conn, cur = _db(database_url)
    cur.execute("UPDATE qbo_connection SET last_error = %s, needs_reconnect = needs_reconnect OR %s WHERE id = 1",
                (message[:500], reconnect))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- OAuth

def authorize_url(redirect_uri, state):
    from urllib.parse import urlencode
    return discovery()["authorization_endpoint"] + "?" + urlencode({
        "client_id": os.environ["QBO_CLIENT_ID"], "response_type": "code", "scope": SCOPE,
        "redirect_uri": redirect_uri, "state": state})


def _token_call(data):
    r = _request("POST", discovery()["token_endpoint"], data=data,
                 auth=(os.environ["QBO_CLIENT_ID"], os.environ["QBO_CLIENT_SECRET"]),
                 headers={"Accept": "application/json"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 400 and body.get("error") == "invalid_grant":
        raise NeedsReconnect("QuickBooks authorization expired or was revoked (invalid_grant)")
    r.raise_for_status()
    return body


def _save_tokens(cur, tokens):
    now = datetime.now(timezone.utc)
    cur.execute("""UPDATE qbo_connection SET access_token = %s, access_expires_at = %s, refresh_token = %s,
                          refresh_expires_at = %s, needs_reconnect = false, last_error = NULL WHERE id = 1""",
                (_enc(tokens["access_token"]), now + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 120),
                 _enc(tokens["refresh_token"]),
                 now + timedelta(seconds=int(tokens.get("x_refresh_token_expires_in", 8640000)))))


def connect(database_url, code, realm_id, redirect_uri):
    """Finish the Connect flow: swap the code for tokens and store them encrypted."""
    tokens = _token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    conn, cur = _db(database_url)
    cur.execute("DELETE FROM qbo_connection")
    cur.execute("DELETE FROM qbo_pnl")  # never mix numbers from a different company
    cur.execute("INSERT INTO qbo_connection (id, env, realm_id, connected_at) VALUES (1, %s, %s, now())",
                (env(), realm_id))
    _save_tokens(cur, tokens)
    conn.commit()
    conn.close()
    try:
        info = api_get(database_url, f"companyinfo/{realm_id}").get("CompanyInfo", {})
        conn, cur = _db(database_url)
        cur.execute("UPDATE qbo_connection SET company_name = %s WHERE id = 1", (info.get("CompanyName"),))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001 - the name is only for display
        pass


def _access_token(database_url, force_refresh=False):
    """A valid access token, refreshing it (and saving the new refresh token) when expired."""
    conn, cur = _db(database_url)
    cur.execute("""SELECT env, realm_id, access_token, access_expires_at, refresh_token, needs_reconnect
                   FROM qbo_connection WHERE id = 1""")
    row = cur.fetchone()
    if not row:
        conn.close()
        raise NeedsReconnect("QuickBooks is not connected")
    conn_env, realm_id, access, access_exp, refresh, needs = row
    if needs:
        conn.close()
        raise NeedsReconnect("QuickBooks needs to be reconnected")
    if conn_env != env():
        conn.close()
        raise NeedsReconnect(f"Connected to {conn_env} but QBO_ENV is {env()}; reconnect")
    if not force_refresh and access and access_exp and access_exp > datetime.now(timezone.utc):
        conn.close()
        return realm_id, _dec(access)
    try:
        tokens = _token_call({"grant_type": "refresh_token", "refresh_token": _dec(refresh)})
    except NeedsReconnect as e:
        conn.close()
        _mark_error(database_url, str(e), reconnect=True)
        raise
    _save_tokens(cur, tokens)
    conn.commit()
    conn.close()
    return realm_id, tokens["access_token"]


def api_get(database_url, path, params=None):
    """GET from the Accounting API. On 401 the token is refreshed once and the call repeated."""
    realm_id, token = _access_token(database_url)
    url = f"{API_BASE[env()]}/v3/company/{realm_id}/{path}"
    params = {**(params or {}), "minorversion": MINOR_VERSION}
    for attempt in range(2):
        r = _request("GET", url, params=params,
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if r.status_code == 401 and attempt == 0:
            realm_id, token = _access_token(database_url, force_refresh=True)
            continue
        if r.status_code in (401, 403):
            _mark_error(database_url, f"QuickBooks refused access (HTTP {r.status_code})", reconnect=True)
            raise NeedsReconnect(f"QuickBooks refused access (HTTP {r.status_code})")
        r.raise_for_status()
        return r.json()


def disconnect(database_url):
    """Revoke the token at Intuit (best effort) and delete everything QuickBooks-related."""
    conn, cur = _db(database_url)
    cur.execute("SELECT refresh_token FROM qbo_connection WHERE id = 1")
    row = cur.fetchone()
    if row and row[0] and configured():
        try:
            _request("POST", discovery()["revocation_endpoint"], json={"token": _dec(row[0])},
                     auth=(os.environ["QBO_CLIENT_ID"], os.environ["QBO_CLIENT_SECRET"]),
                     headers={"Accept": "application/json"}, retries=2)
        except Exception:  # noqa: BLE001 - already revoked on Intuit's side is fine
            pass
    cur.execute("DELETE FROM qbo_connection")
    cur.execute("DELETE FROM qbo_pnl")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- Profit & Loss

# P&L report section group -> our column
PNL_GROUPS = {"Income": "income", "COGS": "cogs", "GrossProfit": "gross_profit", "Expenses": "expenses",
              "NetOperatingIncome": "net_operating_income", "OtherIncome": "other_income",
              "OtherExpenses": "other_expenses", "NetIncome": "net_income"}


def _num(v):
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def parse_pnl(report):
    """{month_start: {column: value}} from a ProfitAndLoss report summarized by month."""
    columns = report.get("Columns", {}).get("Column", [])
    month_cols = {}
    for i, col in enumerate(columns):
        meta = {m.get("Name"): m.get("Value") for m in col.get("MetaData", [])}
        if meta.get("StartDate") and col.get("ColType") == "Money" and col.get("ColTitle", "").lower() != "total":
            month_cols[i] = date.fromisoformat(meta["StartDate"]).replace(day=1)
    out = {m: {c: 0.0 for c in PNL_GROUPS.values()} for m in month_cols.values()}
    for row in report.get("Rows", {}).get("Row", []):
        key = PNL_GROUPS.get(row.get("group"))
        cells = (row.get("Summary") or {}).get("ColData") or []
        if not key or not cells:
            continue
        for i, month in month_cols.items():
            if i < len(cells):
                out[month][key] = _num(cells[i].get("value"))
    return out


def pull_pnl(database_url, months=PNL_MONTHS):
    """Pull the monthly Profit & Loss for the last `months` months into qbo_pnl."""
    today = date.today()
    start = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    report = api_get(database_url, "reports/ProfitAndLoss", {
        "start_date": start.isoformat(), "end_date": today.isoformat(),
        "summarize_column_by": "Month", "accounting_method": "Accrual"})
    data = parse_pnl(report)
    conn, cur = _db(database_url)
    for month, v in data.items():
        cur.execute("""
            INSERT INTO qbo_pnl (month, income, cogs, gross_profit, expenses, net_operating_income,
                                 other_income, other_expenses, net_income, pulled_at)
            VALUES (%(m)s, %(income)s, %(cogs)s, %(gross_profit)s, %(expenses)s, %(net_operating_income)s,
                    %(other_income)s, %(other_expenses)s, %(net_income)s, now())
            ON CONFLICT (month) DO UPDATE SET income = EXCLUDED.income, cogs = EXCLUDED.cogs,
                gross_profit = EXCLUDED.gross_profit, expenses = EXCLUDED.expenses,
                net_operating_income = EXCLUDED.net_operating_income, other_income = EXCLUDED.other_income,
                other_expenses = EXCLUDED.other_expenses, net_income = EXCLUDED.net_income, pulled_at = now()
        """, {"m": month, **v})
    cur.execute("UPDATE qbo_connection SET last_pull_at = now(), last_error = NULL WHERE id = 1")
    conn.commit()
    conn.close()
    return len(data)


def cron_pull(database_url):
    """Called from the cron job: pull if connected, never raise."""
    if not configured():
        print("QuickBooks pull skipped: QBO_CLIENT_ID / QBO_CLIENT_SECRET / QBO_TOKEN_KEY not set.")
        return
    conn, cur = _db(database_url)
    conn.commit()
    cur.execute("SELECT 1 FROM qbo_connection WHERE id = 1")
    connected = cur.fetchone() is not None
    conn.close()
    if not connected:
        print("QuickBooks pull skipped: not connected yet (use Connect on the QuickBooks page).")
        return
    try:
        print(f"QuickBooks ({env()}): pulled {pull_pnl(database_url)} months of Profit & Loss.")
    except NeedsReconnect as e:
        print(f"QuickBooks needs reconnecting: {e}")
    except Exception as e:  # noqa: BLE001 - report and carry on
        _mark_error(database_url, f"Pull failed: {e}")
        print(f"QuickBooks pull failed: {e}")
