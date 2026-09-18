import os
import secrets
from functools import wraps
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, session, redirect, url_for

import psycopg2
import psycopg2.extras

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

DATABASE_URL = os.environ["DATABASE_URL"]
DASHBOARD_USERNAME = os.environ["DASHBOARD_USERNAME"]
DASHBOARD_PASSWORD = os.environ["DASHBOARD_PASSWORD"]

PALETTE = ['#E8A87C', '#7B8FF0', '#8FD3C8', '#F2B84B', '#C99BE0',
           '#7ECF8B', '#E88BA0', '#8FB8E0', '#D9A066', '#9AA5B1']

PERIODS = {"today", "week", "month", "year"}

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        print(f"DEBUG login_required: session={dict(session)}", flush=True)
        if not session.get("logged_in"):
            print("DEBUG login_required: NOT logged in, redirecting", flush=True)
            return redirect(url_for("login", next=request.path))
        print("DEBUG login_required: logged in, calling view", flush=True)
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if secrets.compare_digest(username, DASHBOARD_USERNAME) and secrets.compare_digest(password, DASHBOARD_PASSWORD):
            session["logged_in"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("leaderboard"))
        error = "Incorrect username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def period_start(period):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return today_start - timedelta(days=today_start.weekday())
    if period == "month":
        return today_start.replace(day=1)
    if period == "year":
        return today_start.replace(month=1, day=1)
    return today_start


def duration_label(minutes):
    minutes = minutes or 0
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


def get_agents(period):
    start = period_start(period)
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT agent_name,
               COUNT(*) FILTER (WHERE event_type = 'appt') AS appts,
               COUNT(*) FILTER (WHERE event_type = 'conversation') AS conversations,
               COALESCE(SUM(duration_min) FILTER (WHERE event_type = 'conversation'), 0) AS conversations_dur_min,
               COUNT(*) FILTER (WHERE event_type = 'attempt') AS attempts,
               COUNT(*) FILTER (WHERE event_type = 'text') AS texts,
               COUNT(*) FILTER (WHERE event_type = 'zillow') AS zillow,
               COUNT(*) FILTER (WHERE event_type = 'email') AS emails
        FROM agent_events
        WHERE created_at >= %s
        GROUP BY agent_name
    """, (start,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    agents = []
    for r in rows:
        appts, conversations, attempts = r["appts"] or 0, r["conversations"] or 0, r["attempts"] or 0
        texts, zillow, emails = r["texts"] or 0, r["zillow"] or 0, r["emails"] or 0
        agents.append({
            "name": r["agent_name"],
            "initials": "".join(w[0] for w in r["agent_name"].split()[:2]).upper(),
            "appts": appts, "conversations": conversations,
            "conversations_dur_label": duration_label(r["conversations_dur_min"]),
            "attempts": attempts, "texts": texts, "zillow": zillow, "emails": emails,
            "score": appts * 500 + conversations * 100 + attempts * 10 + texts * 2 + emails * 1 + zillow * 5,
        })
    agents.sort(key=lambda a: a["score"], reverse=True)
    for i, a in enumerate(agents):
        a["rank"] = i + 1
        a["avatar_bg"] = PALETTE[i % len(PALETTE)]

    totals = {
        "appts": sum(a["appts"] for a in agents),
        "conversations": sum(a["conversations"] for a in agents),
        "conversations_dur_label": duration_label(sum(r["conversations_dur_min"] or 0 for r in rows)),
        "attempts": sum(a["attempts"] for a in agents),
        "texts": sum(a["texts"] for a in agents),
        "zillow": sum(a["zillow"] for a in agents),
        "emails": sum(a["emails"] for a in agents),
    }
    return agents, totals

@app.route("/")
@login_required
def leaderboard():
    print("DEBUG leaderboard: view function actually running", flush=True)
    period = request.args.get("period", "today")
    if period not in PERIODS:
        period = "today"
    agents, totals = get_agents(period)
    print(f"DEBUG leaderboard: got {len(agents)} agents, rendering leaderboard.html", flush=True)
    return render_template("leaderboard.html", podium=agents[:3], rest=agents[3:],
                            totals=totals, has_data=len(agents) > 0, period=period)


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
