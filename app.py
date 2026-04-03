from flask import Flask, render_template, redirect, url_for, request
from flask_socketio import SocketIO, emit, join_room
from collections import Counter

import random
import string
import os
import logging

games = {}

WORD_LIST = [
    "Algorithm",
    "Pathfinding",
    "Recursion",
    "Stack",
    "Queue",
    "Variable",
    "Function",
    "Loop",
    "Array",
    "Boolean"
]

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

# Use eventlet async mode (compatible with Flask-SocketIO and Railway)
socketio = SocketIO(app, cors_allowed_origins="*")

# -----------------------------
# Helper function: generate code
# -----------------------------
def generate_code(length=4):
    return ''.join(random.choices(string.ascii_uppercase, k=length))

# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/game/<code>")
def game_room(code):
    if code not in games:
        return redirect("/")  # Redirect if invalid code
    return render_template("game.html", code=code)

# -----------------------------
# SocketIO Events
# -----------------------------

@socketio.on("create_game")
def create_game():
    code = generate_code()
    games[code] = {
        "teams": [],
        "players": {},
        "host_sid": None,

        "state": "lobby",
        "round": 1,
        "max_rounds": 3,

        "impostor": None,
        "word": None,
        "order": [],
        "current_turn": 0,

        "responses": {},
        "votes": {},
        "scores": {}
    }
    emit("redirect", code)

@socketio.on("start_game_request")
def start_game_request(data):
    code = data.get("code")

    if code not in games:
        emit("error", "Game code not found!")
        return

    game = games[code]

    if len(game["teams"]) < 3:
        emit("error", "You need at least 3 teams to start the game.")
        return

    game["state"] = "role"
    game["impostor"] = random.choice(game["teams"])
    game["word"] = random.choice(WORD_LIST)
    game["order"] = random.sample(game["teams"], len(game["teams"]))
    game["current_turn"] = 0
    game["responses"] = {}
    game["votes"] = {}

    emit("game_started", {
        "round": game["round"],
        "order": game["order"]
    }, room=code)

    for sid, team_name in game["players"].items():
        if team_name == "HOST":
            socketio.emit("role_info", {
                "role": "HOST",
                "word": game["word"],
                "impostor": game["impostor"]
            }, to=sid)
        elif team_name == game["impostor"]:
            socketio.emit("role_info", {
                "role": "IMPOSTOR"
            }, to=sid)
        else:
            socketio.emit("role_info", {
                "role": "HUMAN",
                "word": game["word"]
            }, to=sid)

@socketio.on("join_game")
def join_game(data):
    code = data.get("code")
    team = data.get("team")
    is_host = data.get("isHost", False)

    if code not in games:
        emit("error", "Game code not found!")
        return

    game = games[code]
    sid = request.sid

    # 🟢 Handle host separately
    if is_host:
        game["players"][sid] = "HOST"
        game["host_sid"] = sid
        join_room(code)
        print(f"Host joined game {code}")
        emit("update_teams", game["teams"], room=code)
        return

    # 🔴 Normal player validation
    if not team or team.strip() == "":
        emit("error", "Invalid team name!")
        return

    team = team.strip()

    if team not in game["teams"]:
        game["teams"].append(team)
    if team not in game["scores"]:
        game["scores"][team] = 0

    game["players"][sid] = team

    join_room(code)
    emit("update_teams", game["teams"], room=code)

    print(f"{team} joined game {code}")

@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid

    for code, game in list(games.items()):
        if sid in game["players"]:
            team = game["players"].pop(sid)

            # Ignore host
            if team == "HOST":
                print(f"Host disconnected from {code}")
                return

            # Remove team if no one else is using it
            if team not in game["players"].values():
                if team in game["teams"]:
                    game["teams"].remove(team)

            emit("update_teams", game["teams"], room=code)
            print(f"{team} disconnected from {code}")

            # Delete empty game
            if not game["players"]:
                del games[code]
                print(f"Game {code} deleted (empty)")

            break

# -----------------------------
# Run the app
# -----------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # use Railway port
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)