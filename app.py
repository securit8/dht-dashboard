import os
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request
import psycopg2
import psycopg2.extras

app = Flask(__name__)
DATABASE_URL = os.environ["DATABASE_URL"]

PALETTE = ['#E8A87C', '#7B8FF0', '#8FD3C8', '#F2B84B', '#C99BE0',
           '#7ECF8B', '#E88BA0', '#8FB8E0', '#D9A066', '#9AA5B1']

PERIODS = {"today", "week", "month", "year"}


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
               COUNT(*) FILTER (WHERE event_type = 'attempt') AS attempts
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
        agents.append({
            "name": r["agent_name"],
            "initials": "".join(w[0] for w in r["agent_name"].split()[:2]).upper(),
            "appts": appts, "conversations": conversations,
            "conversations_dur_label": duration_label(r["conversations_dur_min"]),
            "attempts": attempts, "texts": 0, "zillow": 0, "emails": 0,
            "score": appts * 500 + conversations * 100 + attempts * 10,
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
        "texts": 0, "zillow": 0, "emails": 0,
    }
    return agents, totals


@app.route("/")
def leaderboard():
    period = request.args.get("period", "today")
    if period not in PERIODS:
        period = "today"
    agents, totals = get_agents(period)
    return render_template("leaderboard.html", podium=agents[:3], rest=agents[3:],
                            totals=totals, has_data=len(agents) > 0, period=period)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
