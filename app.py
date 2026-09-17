
import os
from flask import Flask, render_template
import psycopg2
import psycopg2.extras

app = Flask(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]

# Same rotating avatar palette as the design mockup, for agents without a photo
PALETTE = ['#E8A87C', '#7B8FF0', '#8FD3C8', '#F2B84B', '#C99BE0',
           '#7ECF8B', '#E88BA0', '#8FB8E0', '#D9A066', '#9AA5B1']


def duration_label(minutes):
    minutes = minutes or 0
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


def get_agents():
    """Pull every row from agent_activity, compute each agent's Score using
    Follow Up Boss's own Leaderboard weighting, and rank them."""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM agent_activity ORDER BY agent_name")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    agents = []
    for r in rows:
        appts = r["appts"] or 0
        conversations = r["conversations"] or 0
        attempts = r["attempts"] or 0
        texts = r["texts"] or 0
        emails = r["emails"] or 0
        zillow = r["zillow"] or 0

        score = appts * 500 + conversations * 100 + attempts * 10 + texts * 2 + emails * 1 + zillow * 5

        agents.append({
            "name": r["agent_name"],
            "initials": "".join(w[0] for w in r["agent_name"].split()[:2]).upper(),
            "appts": appts,
            "conversations": conversations,
            "conversations_dur_label": duration_label(r["conversations_dur_min"]),
            "attempts": attempts,
            "texts": texts,
            "zillow": zillow,
            "emails": emails,
            "score": score,
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
def leaderboard():
    agents, totals = get_agents()
    return render_template(
        "leaderboard.html",
        podium=agents[:3],
        rest=agents[3:],
        totals=totals,
        has_data=len(agents) > 0,
    )


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
