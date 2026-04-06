from flask import Flask, render_template, redirect, request
from flask_socketio import SocketIO, emit, join_room
from collections import Counter

import random
import string
import os
import logging

games = {}

WORD_LIST = [
    # --- Computer Science (50) ---
    "Algorithm", "Array", "Function", "Variable", "Loop",
    "Stack", "Queue", "Tree", "Graph", "HashMap",
    "Binary", "Pointer", "Object", "Class", "Method",
    "Runtime", "Compiler", "Syntax", "Bug", "Debugging",
    "Exception", "Optimization", "Search", "Sort", "Traversal",
    "Database", "Query", "Table", "API", "Request",
    "Response", "Server", "Client", "Protocol", "Cache",
    "Latency", "Thread", "Memory", "Storage", "Encryption",
    "Hashing", "Authentication", "Token", "Security", "Frontend",
    "Backend", "Framework", "Library", "Deployment", "Cloud",

    # --- General Words (50) ---
    "Sky", "Ocean", "Mountain", "River", "Forest",
    "Desert", "Island", "Volcano", "Storm", "Rainbow",

    "Dragon", "Castle", "Knight", "Treasure", "Sword",
    "Shield", "Wizard", "Potion", "Crown", "Throne",

    "Banana", "Pizza", "Burger", "Pancake", "Cookie",
    "Cupcake", "Popcorn", "Milkshake", "Sandwich", "Waffle",

    "Guitar", "Piano", "Drum", "Violin", "Microphone",
    "Speaker", "Headphones", "Camera", "Painting", "Dance",

    "Tiger", "Elephant", "Penguin", "Giraffe", "Dolphin",
    "Panda", "Kangaroo", "Zebra", "Owl", "Octopus"
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
    game["ready_teams"] = set()

    game["turn_token"] += 1


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

    emit_ready_status(code)


def emit_ready_status(code):
    game = games[code]
    socketio.emit("ready_status", {
        "ready_teams": sorted(list(game["ready_teams"])),
        "total_teams": len(game["teams"]),
        "all_ready": len(game["ready_teams"]) == len(game["teams"])
    }, room=code)


def start_phrase_phase(code):
    if code not in games:
        return

    game = games[code]

    if game["current_turn"] >= len(game["order"]):
        go_to_next_team_or_pause_before_voting(code)
        return

    game["state"] = "phrase"
    game["turn_token"] += 1
    turn_token = game["turn_token"]

    current_team = game["order"][game["current_turn"]]

    socketio.emit("phrase_phase_started", {
        "current_team": current_team,
        "current_index": game["current_turn"] + 1,
        "total_teams": len(game["order"]),
        "responses": game["responses"],
        "time_limit": 30
    }, room=code)

    socketio.start_background_task(run_phrase_timer, code, current_team, turn_token, 30)


def run_phrase_timer(code, current_team, turn_token, seconds):
    for remaining in range(seconds, 0, -1):
        if code not in games:
            return

        game = games[code]

        if game["state"] != "phrase":
            return

        if game["turn_token"] != turn_token:
            return

        current_index = game["current_turn"]
        if current_index >= len(game["order"]):
            return

        if game["order"][current_index] != current_team:
            return

        if current_team in game["responses"]:
            return

        socketio.emit("phrase_timer_update", {
            "current_team": current_team,
            "seconds_left": remaining
        }, room=code)

        socketio.sleep(1)

    if code not in games:
        return

    game = games[code]

    if game["state"] != "phrase":
        return

    if game["turn_token"] != turn_token:
        return

    current_index = game["current_turn"]
    if current_index >= len(game["order"]):
        return

    if game["order"][current_index] != current_team:
        return

    if current_team in game["responses"]:
        return

    game["responses"][current_team] = "[No Response]"

    socketio.emit("phrase_submitted", {
        "team": current_team,
        "phrase": "[No Response]",
        "responses": game["responses"],
        "auto_submitted": True
    }, room=code)

    go_to_next_team_or_pause_before_voting(code)


def go_to_next_team_or_pause_before_voting(code):
    if code not in games:
        return

    game = games[code]
    game["current_turn"] += 1

    if game["current_turn"] >= len(game["order"]):
        game["state"] = "paused_before_voting"

        socketio.emit("all_phrases_complete", {
            "responses": game["responses"]
        }, room=code)

        socketio.emit("show_continue", {
            "message": "All phrases are in. Host, press Continue to begin voting."
        }, room=code)
    else:
        start_phrase_phase(code)


def begin_voting_phase(code):
    game = games[code]
    game["state"] = "voting"

    socketio.emit("start_voting", {
        "teams": game["teams"],
        "responses": game["responses"]
    }, room=code)


def calculate_round_result(code):
    game = games[code]
    actual_impostor = game["impostor"]
    vote_counts = Counter(game["votes"].values())

    individual_correct_teams = sorted(
        [team for team, voted_for in game["votes"].items() if voted_for == actual_impostor]
    )

    majority_team = None
    majority_correct = False

    if vote_counts:
        max_votes = max(vote_counts.values())
        top_teams = [team for team, count in vote_counts.items() if count == max_votes]

        if len(top_teams) == 1:
            majority_team = top_teams[0]
            majority_correct = (majority_team == actual_impostor)

    if majority_correct:
        for team in game["teams"]:
            if team != actual_impostor:
                game["scores"][team] += 2

        for team in individual_correct_teams:
            game["scores"][team] += 1

        result_text = (
            f"Majority caught the impostor. All HUMAN teams get +2. "
            f"Teams that individually voted correctly also get +1."
        )
    else:
        game["scores"][actual_impostor] += 4

        for team in individual_correct_teams:
            game["scores"][team] += 1

        if majority_team is None:
            result_text = (
                f"No majority choice was reached. {actual_impostor} survives and gets +4. "
                f"Any teams that individually voted for the real impostor still get +1."
            )
        else:
            result_text = (
                f"Majority voted for {majority_team}, not the real impostor. "
                f"{actual_impostor} survives and gets +4. "
                f"Any teams that individually voted for the real impostor still get +1."
            )

    socketio.emit("round_result", {
        "majority_team": majority_team,
        "actual_impostor": actual_impostor,
        "majority_correct": majority_correct,
        "individual_correct_teams": individual_correct_teams,
        "result_text": result_text,
        "scores": game["scores"]
    }, room=code)

    socketio.emit("update_scores", game["scores"], room=code)

    game["state"] = "paused_after_result"

    socketio.emit("show_continue", {
        "message": "Round result shown. Host, press Continue when everyone is ready."
    }, room=code)


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

    game["state"] = "waiting_ready"

    socketio.emit("role_ready_required", {
        "message": "All teams must press READY before phrase submission can begin."
    }, room=code)


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
        "scores": {},
        "ready_teams": set(),
        "turn_token": 0
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
        emit_ready_status(code)

        logging.info(f"Host joined game {code}")
        return

    if not team or not str(team).strip():
        emit("error", "Invalid team name!")
        return

    team = str(team).strip()

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
    emit_ready_status(code)
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

    game["state"] = "waiting_ready"

    socketio.emit("role_ready_required", {
        "message": "All teams must press READY before phrase submission can begin."
    }, room=code)


@socketio.on("team_ready")
def team_ready(data):
    code = data.get("code")
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found!")
        return

    game = games[code]

    if sid not in game["players"]:
        emit("error", "You are not part of this game.")
        return

    team_name = game["players"][sid]

    if team_name == "HOST":
        emit("error", "Host does not use the READY button.")
        return

    if game["state"] != "waiting_ready":
        emit("error", "READY is not needed right now.")
        return

    game["ready_teams"].add(team_name)
    emit_ready_status(code)

    emit("ready_confirmed", {
        "message": "Your team is marked READY."
    }, to=sid)

    if len(game["ready_teams"]) == len(game["teams"]):
        socketio.emit("show_continue", {
            "message": "All teams are READY. Host, press Continue to begin phrase submission."
        }, room=code)


@socketio.on("host_continue")
def host_continue(data):
    code = data.get("code")
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found!")
        return

    game = games[code]

    if sid != game["host_sid"]:
        emit("error", "Only the host can continue.")
        return

    if game["state"] == "waiting_ready":
        if len(game["ready_teams"]) < len(game["teams"]):
            emit("error", "Not all teams are READY yet.")
            return

    socketio.emit("hide_continue", {}, room=code)

    if game["state"] == "waiting_ready":
        start_phrase_phase(code)

    elif game["state"] == "paused_before_voting":
        begin_voting_phase(code)

    elif game["state"] == "paused_after_result":
        start_next_round_or_end(code)

    else:
        emit("error", "There is nothing to continue right now.")


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

    if team_name != game["impostor"] and phrase.lower() == game["word"].lower():
        emit("error", "You cannot directly submit the secret word.")
        return

    game["responses"][team_name] = phrase
    game["turn_token"] += 1

    socketio.emit("phrase_submitted", {
        "team": team_name,
        "phrase": phrase,
        "responses": game["responses"],
        "auto_submitted": False
    }, room=code)

    go_to_next_team_or_pause_before_voting(code)


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

    if len(game["votes"]) >= len(game["teams"]):
        calculate_round_result(code)


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

            remaining_teams = [name for name in game["players"].values() if name != "HOST"]
            if team not in remaining_teams:
                if team in game["teams"]:
                    game["teams"].remove(team)

            game["ready_teams"].discard(team)

        if not game["players"]:
            del games[code]
            logging.info(f"Game {code} deleted (empty)")
            break

        emit_team_and_score_updates(code)
        emit_ready_status(code)
        break


# -----------------------------
# Run app
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)