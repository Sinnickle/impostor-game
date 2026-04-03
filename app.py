from flask import Flask, render_template, redirect, request
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
app.config["SECRET_KEY"] = "impostor-secret-key"

socketio = SocketIO(app, cors_allowed_origins="*")


# -----------------------------
# Helper functions
# -----------------------------
def generate_code(length=4):
    return "".join(random.choices(string.ascii_uppercase, k=length))


def emit_team_and_score_updates(code):
    game = games[code]
    socketio.emit("update_teams", game["teams"], room=code)
    socketio.emit("update_scores", game["scores"], room=code)


def setup_round(code):
    game = games[code]

    game["state"] = "role"
    game["impostor"] = random.choice(game["teams"])
    game["word"] = random.choice(WORD_LIST)
    game["order"] = random.sample(game["teams"], len(game["teams"]))
    game["current_turn"] = 0
    game["responses"] = {}
    game["votes"] = {}


def send_role_info(code):
    game = games[code]

    socketio.emit("game_started", {
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


def start_phrase_phase(code):
    game = games[code]
    game["state"] = "phrase"

    current_team = game["order"][game["current_turn"]]

    socketio.emit("phrase_phase_started", {
        "current_team": current_team,
        "current_index": game["current_turn"] + 1,
        "total_teams": len(game["order"]),
        "responses": game["responses"]
    }, room=code)


def go_to_next_team_or_voting(code):
    game = games[code]
    game["current_turn"] += 1

    if game["current_turn"] >= len(game["order"]):
        game["state"] = "voting"

        socketio.emit("all_phrases_complete", {
            "responses": game["responses"]
        }, room=code)

        socketio.emit("start_voting", {
            "teams": game["teams"],
            "responses": game["responses"]
        }, room=code)
    else:
        start_phrase_phase(code)


def calculate_round_result(code):
    game = games[code]

    vote_counts = Counter(game["votes"].values())

    if not vote_counts:
        # Nobody voted, impostor survives
        voted_team = "No team"
        actual_impostor = game["impostor"]
        result_text = f"No votes were submitted. {actual_impostor} survives and gets +4 points."
        game["scores"][actual_impostor] += 4
    else:
        max_votes = max(vote_counts.values())
        tied_teams = [team for team, count in vote_counts.items() if count == max_votes]

        # If tied, pick one randomly for now
        voted_team = random.choice(tied_teams)
        actual_impostor = game["impostor"]

        if voted_team == actual_impostor:
            result_text = f"{actual_impostor} was caught. All HUMAN teams get +2 points."
            for team in game["teams"]:
                if team != actual_impostor:
                    game["scores"][team] += 2
        else:
            result_text = f"{actual_impostor} was not caught. The impostor gets +4 points."
            game["scores"][actual_impostor] += 4

    socketio.emit("round_result", {
        "voted_team": voted_team,
        "actual_impostor": actual_impostor,
        "result_text": result_text,
        "scores": game["scores"]
    }, room=code)

    socketio.emit("update_scores", game["scores"], room=code)


def start_next_round_or_end(code):
    game = games[code]

    if game["round"] >= game["max_rounds"]:
        game["state"] = "game_over"

        socketio.emit("game_over", {
            "scores": game["scores"]
        }, room=code)
        return

    game["round"] += 1
    setup_round(code)
    send_role_info(code)
    start_phrase_phase(code)


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/game/<code>")
def game_room(code):
    if code not in games:
        return redirect("/")
    return render_template("game.html", code=code)


# -----------------------------
# SocketIO events
# -----------------------------
@socketio.on("create_game")
def create_game():
    code = generate_code()

    while code in games:
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


@socketio.on("join_game")
def join_game(data):
    code = data.get("code")
    team = data.get("team")
    is_host = data.get("isHost", False)
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found!")
        return

    game = games[code]

    if is_host:
        game["players"][sid] = "HOST"
        game["host_sid"] = sid
        join_room(code)

        emit_team_and_score_updates(code)

        logging.info(f"Host joined game {code}")
        return

    if not team or not str(team).strip():
        emit("error", "Invalid team name!")
        return

    team = str(team).strip()

    # Prevent duplicate active team names
    active_non_host_teams = [name for name in game["players"].values() if name != "HOST"]
    if team in active_non_host_teams:
        emit("error", "That team name is already taken!")
        return

    if team not in game["teams"]:
        game["teams"].append(team)

    if team not in game["scores"]:
        game["scores"][team] = 0

    game["players"][sid] = team
    join_room(code)

    emit_team_and_score_updates(code)
    logging.info(f"{team} joined game {code}")


@socketio.on("start_game_request")
def start_game_request(data):
    code = data.get("code")
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found!")
        return

    game = games[code]

    if sid != game["host_sid"]:
        emit("error", "Only the host can start the game.")
        return

    if len(game["teams"]) < 3:
        emit("error", "You need at least 3 teams to start the game.")
        return

    setup_round(code)
    emit_team_and_score_updates(code)
    send_role_info(code)
    start_phrase_phase(code)


@socketio.on("submit_phrase")
def submit_phrase(data):
    code = data.get("code")
    phrase = (data.get("phrase") or "").strip()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found!")
        return

    game = games[code]

    if game["state"] != "phrase":
        emit("error", "It is not the phrase phase right now.")
        return

    if sid not in game["players"]:
        emit("error", "You are not part of this game.")
        return

    team_name = game["players"][sid]

    if team_name == "HOST":
        emit("error", "Host cannot submit a phrase.")
        return

    current_team = game["order"][game["current_turn"]]

    if team_name != current_team:
        emit("error", f"It is currently {current_team}'s turn.")
        return

    if not phrase:
        emit("error", "Phrase cannot be empty.")
        return

    words = phrase.split()
    if len(words) > 3:
        emit("error", "Phrase must be no longer than 3 words.")
        return

    if team_name in game["responses"]:
        emit("error", "Your team already submitted a phrase.")
        return

    # Optional anti-cheat: humans cannot directly say the word
    if team_name != game["impostor"] and phrase.lower() == game["word"].lower():
        emit("error", "You cannot directly submit the secret word.")
        return

    game["responses"][team_name] = phrase

    socketio.emit("phrase_submitted", {
        "team": team_name,
        "phrase": phrase,
        "responses": game["responses"]
    }, room=code)

    go_to_next_team_or_voting(code)


@socketio.on("submit_vote")
def submit_vote(data):
    code = data.get("code")
    voted_team = data.get("voted_team")
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found!")
        return

    game = games[code]

    if game["state"] != "voting":
        emit("error", "It is not the voting phase right now.")
        return

    if sid not in game["players"]:
        emit("error", "You are not part of this game.")
        return

    voter_team = game["players"][sid]

    if voter_team == "HOST":
        emit("error", "Host cannot vote.")
        return

    if voted_team not in game["teams"]:
        emit("error", "Invalid team selected.")
        return

    if voted_team == voter_team:
        emit("error", "You cannot vote for your own team.")
        return

    if voter_team in game["votes"]:
        emit("error", "Your team already voted.")
        return

    game["votes"][voter_team] = voted_team

    emit("vote_received", {
        "message": f"You voted for {voted_team}."
    }, to=sid)

    # When all non-host teams have voted, calculate result automatically
    if len(game["votes"]) >= len(game["teams"]):
        calculate_round_result(code)
        start_next_round_or_end(code)


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid

    for code, game in list(games.items()):
        if sid not in game["players"]:
            continue

        team = game["players"].pop(sid)

        if team == "HOST":
            logging.info(f"Host disconnected from {code}")
            game["host_sid"] = None
        else:
            logging.info(f"{team} disconnected from {code}")

            # Remove team only if no other socket is using it
            remaining_teams = [name for name in game["players"].values() if name != "HOST"]
            if team not in remaining_teams:
                if team in game["teams"]:
                    game["teams"].remove(team)

        # If no players remain at all, delete game
        if not game["players"]:
            del games[code]
            logging.info(f"Game {code} deleted (empty)")
            break

        emit_team_and_score_updates(code)
        break


# -----------------------------
# Run app
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)