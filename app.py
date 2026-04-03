from flask import Flask, render_template, redirect, url_for
from flask_socketio import SocketIO, emit, join_room
import random
import string
import os
import logging

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
    # Create game with Host
    games[code] = ["Host"]
    print(f"Game created: {code}")
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

    if team not in games[code]:
        games[code].append(team)

    join_room(code)
    emit("update_teams", games[code], room=code)
    print(f"{team} joined game {code}")

# -----------------------------
# Run the app
# -----------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # use Railway port
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)