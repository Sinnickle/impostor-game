from flask import Flask, render_template
from flask_socketio import SocketIO, emit, join_room
import os
import random
import string

app = Flask(__name__)
socketio = SocketIO(app)

games = {}

def generate_code():
    return ''.join(random.choices(string.ascii_uppercase, k=4))

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/game/<code>")
def game(code):
    return render_template("game.html", code=code)

# Create game
@socketio.on("create_game")
def create_game():
    code = generate_code()
    games[code] = []
    emit("redirect", code)

# Join game
@socketio.on("join_game")
def join_game(data):
    code = data["code"]
    team = data["team"]

    if code in games:
        join_room(code)
        games[code].append(team)

        # ONLY send updates to that room
        emit("update_teams", games[code], room=code)
    else:
        emit("error", "Game not found")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)