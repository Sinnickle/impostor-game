from flask import Flask, render_template, redirect, url_for, request
from flask_socketio import SocketIO, emit, join_room
import random
import string
import os
import logging

games = {}

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

# Use eventlet async mode (compatible with Flask-SocketIO and Railway)
socketio = SocketIO(app, cors_allowed_origins="*")

# Store all games: {code: [player1, player2, ...]}
games = {}

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
        "players": {}
    }
    emit("redirect", code)

@socketio.on("join_game")
def join_game(data):
    code = data.get("code")
    team = data.get("team")

    if not team or team.strip() == "":
        emit("error", "Invalid team name!")
        return

    if code not in games:
        emit("error", "Game code not found!")
        return

    game = games[code]
    sid = request.sid  # unique socket ID

    # Prevent duplicate teams
    if team not in game["teams"]:
        game["teams"].append(team)

    # Track player
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