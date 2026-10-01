"""Entry point of the separate demo service ("Try it with random numbers").

Render start command:  gunicorn demo_wsgi:app --workers 1 --threads 4
The demo service has no keys and no DATABASE_URL: it starts its own throwaway Postgres inside the container,
fills it with random numbers, and serves the same app in demo mode. It can't reach the real database or any
connected account. The random data is made again on every restart and at least once a day.
"""
import os
import secrets
import tempfile
import threading
import time
from datetime import date

import psycopg2

os.environ["DEMO_MODE"] = "1"
for key in ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_TOKEN_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
            "GMAIL_TOKEN_KEY", "FUB_API_KEY", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"):
    os.environ.pop(key, None)  # never use a real key here, even if one was set by mistake
if not os.environ.get("DATABASE_URL"):
    import pgserver
    _srv = pgserver.get_server(os.path.join(tempfile.gettempdir(), "dht-demo-pg"), cleanup_mode=None)
    os.environ["DATABASE_URL"] = _srv.get_uri()
os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("DASHBOARD_USERNAME", "demo")
os.environ.setdefault("DASHBOARD_PASSWORD", secrets.token_hex(16))  # nobody signs in to the demo
os.environ.setdefault("QBO_ENV", "production")
os.environ.setdefault("TEAM_TZ", "America/Los_Angeles")

import demo  # noqa: E402


def _reseed():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    demo.seed(cur)
    cur.execute("CREATE TABLE IF NOT EXISTS demo_meta (id INT PRIMARY KEY, seeded_on DATE)")
    cur.execute("INSERT INTO demo_meta VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET seeded_on = EXCLUDED.seeded_on", (date.today(),))
    conn.commit()
    conn.close()


_reseed()

from app import app  # noqa: E402

demo.apply(app)


def _daily():
    while True:
        time.sleep(3600)
        try:
            conn = psycopg2.connect(os.environ["DATABASE_URL"])
            cur = conn.cursor()
            cur.execute("SELECT seeded_on FROM demo_meta WHERE id = 1")
            row = cur.fetchone()
            conn.close()
            if not row or row[0] < date.today():
                _reseed()
        except Exception:  # noqa: BLE001
            app.logger.exception("demo reseed")


threading.Thread(target=_daily, name="demo-reseed", daemon=True).start()
