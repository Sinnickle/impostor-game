from flask import Flask, render_template
from flask_socketio import SocketIO
import os, random, string

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="asgi")  # use asyncio

games = {}

def generate_code(length=4):
    return ''.join(random.choices(string.ascii_uppercase, k=length))

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/game/<code>")
def game_room(code):
    return render_template("game.html", code=code)

@socketio.on("create_game")
def create_game():
    code = generate_code()
    games[code] = ["Host"]
    emit("redirect", code)

@socketio.on("join_game")
def join_game(data):
    code = data.get("code")
    team = data.get("team")
    if code not in games or not team:
        emit("error", "Invalid join!")
        return
    if team not in games[code]:
        games[code].append(team)
    join_room(code)
    emit("update_teams", games[code], room=code)