from flask import Flask, render_template
from flask_socketio import SocketIO, emit, join_room
import os, random, string

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="asgi")

# Store games: {code: [player1, player2, ...]}
games = {}

# Helper: generate 4-letter code
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
    return render_template("game.html", code=code)

# -----------------------------
# SocketIO Events
# -----------------------------
@socketio.on("create_game")
def create_game():
    code = generate_code()
    games[code] = ["Host"]
    emit("redirect", code)

@socketio.on("join_game")
def join_game(data):
    code = data.get("code")
    team = data.get("team")
    if not code or not team or code not in games:
        emit("error", "Invalid join!")
        return
    if team not in games[code]:
        games[code].append(team)
    join_room(code)
    emit("update_teams", games[code], room=code)

# -----------------------------
# Run using ASGI server (uvicorn handles this)
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)