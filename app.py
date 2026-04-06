import os
import random
import string
from collections import Counter

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config["SECRET_KEY"] = "impostor-secret-key"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

WORD_LIST = [
    "Algorithm", "Binary", "Compiler", "Database", "Encryption",
    "Function", "Interface", "Kernel", "Loop", "Memory",
    "Network", "Object", "Packet", "Queue", "Recursion",
    "Server", "Stack", "Syntax", "Thread", "Variable",
    "Array", "Boolean", "Cache", "Class", "Cloud",
    "Debugging", "Framework", "Frontend", "Backend", "Hash",
    "Integer", "Iteration", "Library", "Machine Learning", "Pointer",
    "Runtime", "Script", "Search", "Sorting", "Terminal"
]

games = {}


def make_code(length=5):
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
        if code not in games:
            return code


def create_game_state():
    return {
        "host_sid": None,
        "host_connected": False,

        "teams": [],
        "team_sids": {},          # team_name -> sid
        "players_by_sid": {},     # sid -> team_name or HOST

        "state": "lobby",         # lobby, agreement, role, phrase, voting, paused_after_result, game_over
        "round": 1,
        "max_rounds": 3,

        "impostor": None,
        "word": None,
        "order": [],
        "current_turn_index": 0,

        "responses": {},
        "votes": {},
        "scores": {},

        "agreement_ready": set(),
        "additional_round_voters": set(),

        "phrase_time_limit": 30,
        "vote_time_limit": 120,

        "turn_token": 0,
        "vote_token": 0,
    }


def get_game(code):
    return games.get(code)


def non_host_team_count(game):
    return len(game["teams"])


def all_teams_agreed(game):
    return len(game["agreement_ready"]) == len(game["teams"]) and len(game["teams"]) >= 3


def sanitize_phrase(phrase):
    if phrase is None:
        return ""
    return " ".join(str(phrase).strip().split())


def emit_roster_update(code):
    game = games[code]
    socketio.emit("roster_update", {
        "code": code,
        "teams": game["teams"],
        "scores": game["scores"],
        "state": game["state"],
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "agreement_ready": sorted(list(game["agreement_ready"])),
    }, room=code)


def emit_status(code, message):
    socketio.emit("status_message", {"message": message}, room=code)


def send_private_role_info(code):
    game = games[code]

    for team in game["teams"]:
        sid = game["team_sids"].get(team)
        if not sid:
            continue

        if team == game["impostor"]:
            socketio.emit("role_assignment", {
                "role": "IMPOSTOR",
                "word": None,
                "round": game["round"],
                "max_rounds": game["max_rounds"],
                "order": game["order"],
                "phrase_time_limit": game["phrase_time_limit"],
            }, to=sid)
        else:
            socketio.emit("role_assignment", {
                "role": "HUMAN",
                "word": game["word"],
                "round": game["round"],
                "max_rounds": game["max_rounds"],
                "order": game["order"],
                "phrase_time_limit": game["phrase_time_limit"],
            }, to=sid)

    if game["host_sid"]:
        socketio.emit("host_role_overview", {
            "round": game["round"],
            "max_rounds": game["max_rounds"],
            "word": game["word"],
            "impostor": game["impostor"],
            "order": game["order"],
            "scores": game["scores"],
        }, to=game["host_sid"])


def emit_round_header(code, preserved=False):
    game = games[code]
    socketio.emit("round_started", {
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "order": game["order"],
        "preserved": preserved,
        "role_screen_seconds": 60
    }, room=code)


def begin_round(code, preserve_roles=False):
    game = games[code]

    game["state"] = "role"
    game["responses"] = {}
    game["votes"] = {}
    game["additional_round_voters"] = set()
    game["current_turn_index"] = 0

    game["turn_token"] += 1
    game["vote_token"] += 1

    if not preserve_roles:
        game["impostor"] = random.choice(game["teams"])
        game["word"] = random.choice(WORD_LIST)

    game["order"] = random.sample(game["teams"], len(game["teams"]))

    emit_roster_update(code)
    emit_round_header(code, preserved=preserve_roles)
    send_private_role_info(code)

    if preserve_roles:
        emit_status(code, "Additional round approved. Same roles, same word, same impostor.")
    else:
        emit_status(code, f"Round {game['round']} is ready. Roles have been assigned.")


def start_phrase_phase(code):
    game = games[code]
    game["state"] = "phrase"
    emit_roster_update(code)
    start_next_turn(code)


def start_next_turn(code):
    game = games[code]

    if game["current_turn_index"] >= len(game["order"]):
        begin_voting_phase(code)
        return

    current_team = game["order"][game["current_turn_index"]]
    game["turn_token"] += 1
    token = game["turn_token"]

    socketio.emit("start_turn", {
        "current_team": current_team,
        "turn_index": game["current_turn_index"] + 1,
        "total_turns": len(game["order"]),
        "seconds": game["phrase_time_limit"],
        "responses": game["responses"],
    }, room=code)

    socketio.start_background_task(run_phrase_timer, code, current_team, token, game["phrase_time_limit"])


def run_phrase_timer(code, team_name, token, seconds):
    for remaining in range(seconds, 0, -1):
        game = games.get(code)
        if not game:
            return

        if game["state"] != "phrase":
            return

        if game["turn_token"] != token:
            return

        current_team = game["order"][game["current_turn_index"]]
        if current_team != team_name:
            return

        socketio.emit("phrase_timer_update", {
            "current_team": team_name,
            "seconds_left": remaining
        }, room=code)

        socketio.sleep(1)

    game = games.get(code)
    if not game:
        return

    if game["state"] != "phrase":
        return

    if game["turn_token"] != token:
        return

    current_team = game["order"][game["current_turn_index"]]
    if current_team != team_name:
        return

    if team_name not in game["responses"]:
        game["responses"][team_name] = "(No phrase submitted)"
        socketio.emit("phrase_locked", {
            "team": team_name,
            "phrase": game["responses"][team_name],
            "auto_submitted": True
        }, room=code)

        game["current_turn_index"] += 1
        socketio.sleep(1)
        start_next_turn(code)


def begin_voting_phase(code):
    game = games[code]
    game["state"] = "voting"
    game["votes"] = {}
    game["additional_round_voters"] = set()
    game["vote_token"] += 1
    token = game["vote_token"]

    emit_roster_update(code)

    socketio.emit("start_voting", {
        "teams": game["teams"],
        "responses": game["responses"],
        "time_limit": game["vote_time_limit"],
    }, room=code)

    socketio.start_background_task(run_vote_timer, code, token, game["vote_time_limit"])


def run_vote_timer(code, token, seconds):
    for remaining in range(seconds, 0, -1):
        game = games.get(code)
        if not game:
            return

        if game["state"] != "voting":
            return

        if game["vote_token"] != token:
            return

        socketio.emit("vote_timer_update", {
            "seconds_left": remaining
        }, room=code)

        socketio.sleep(1)

    game = games.get(code)
    if not game:
        return

    if game["state"] != "voting":
        return

    if game["vote_token"] != token:
        return

    calculate_round_result(code)


def calculate_round_result(code):
    game = games[code]
    actual_impostor = game["impostor"]

    accusation_votes = {
        voter: vote
        for voter, vote in game["votes"].items()
        if vote != "ADDITIONAL_ROUND"
    }

    additional_round_count = len(game["additional_round_voters"])

    vote_counts = Counter(accusation_votes.values())
    majority_team = None
    majority_correct = False

    if vote_counts:
        max_votes = max(vote_counts.values())
        top_choices = [team for team, count in vote_counts.items() if count == max_votes]
        if len(top_choices) == 1:
            majority_team = top_choices[0]
            majority_correct = (majority_team == actual_impostor)

    individual_correct_teams = sorted([
        team_name for team_name, voted_for in accusation_votes.items()
        if voted_for == actual_impostor
    ])

    # Individual rewards: +1 if a team personally guessed correctly
    for team_name in individual_correct_teams:
        game["scores"][team_name] += 1

    result_text = ""
    if majority_correct:
        # Every human team gets +2
        for team_name in game["teams"]:
            if team_name != actual_impostor:
                game["scores"][team_name] += 2
        result_text = "The majority caught the impostor. All HUMAN teams gain +2."
    else:
        game["scores"][actual_impostor] += 4
        if majority_team is None:
            result_text = "No clear majority formed. The impostor survives and gains +4."
        else:
            result_text = "The majority accused the wrong team. The impostor survives and gains +4."

    additional_round_triggered = additional_round_count > (len(game["teams"]) / 2)

    game["state"] = "paused_after_result"
    emit_roster_update(code)

    socketio.emit("round_result", {
        "majority_team": majority_team,
        "actual_impostor": actual_impostor,
        "majority_correct": majority_correct,
        "individual_correct_teams": individual_correct_teams,
        "result_text": result_text,
        "scores": game["scores"],
        "responses": game["responses"],
        "votes": game["votes"],
        "additional_round_votes": additional_round_count,
        "additional_round_triggered": additional_round_triggered,
    }, room=code)


def advance_after_result(code):
    game = games[code]

    additional_round_triggered = len(game["additional_round_voters"]) > (len(game["teams"]) / 2)

    if additional_round_triggered:
        begin_round(code, preserve_roles=True)
        return

    if game["round"] >= game["max_rounds"]:
        end_game(code)
        return

    game["round"] += 1
    begin_round(code, preserve_roles=False)


def end_game(code):
    game = games[code]
    game["state"] = "game_over"

    sorted_scores = sorted(
        game["scores"].items(),
        key=lambda item: (-item[1], item[0].lower())
    )

    top_three = sorted_scores[:3]

    emit_roster_update(code)
    socketio.emit("game_over", {
        "scores": game["scores"],
        "sorted_scores": sorted_scores,
        "top_three": top_three,
        "winner": top_three[0] if top_three else None
    }, room=code)


def reset_to_agreement(code):
    game = games[code]

    game["state"] = "agreement"
    game["round"] = 1
    game["impostor"] = None
    game["word"] = None
    game["order"] = []
    game["current_turn_index"] = 0
    game["responses"] = {}
    game["votes"] = {}
    game["agreement_ready"] = set()
    game["additional_round_voters"] = set()
    game["turn_token"] += 1
    game["vote_token"] += 1

    for team in game["teams"]:
        game["scores"][team] = 0

    emit_roster_update(code)
    socketio.emit("agreement_phase", {
        "message": "Game restarted. All teams must agree/ready again before Round 1."
    }, room=code)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/game")
def game():
    return render_template("game.html")


@socketio.on("create_game")
def create_game():
    code = make_code()
    games[code] = create_game_state()
    emit("game_created", {"code": code})


@socketio.on("join_game")
def join_game(data):
    code = str(data.get("code", "")).strip().upper()
    team_name = str(data.get("team_name", "")).strip()

    if code not in games:
        emit("error", "Game code not found.")
        return

    if not team_name:
        emit("error", "Please enter a team name.")
        return

    game = games[code]

    if game["state"] not in ("lobby", "agreement"):
        emit("error", "The game has already started.")
        return

    if team_name in game["teams"]:
        emit("joined_game", {"code": code, "team_name": team_name})
        return

    emit("joined_game", {"code": code, "team_name": team_name})


@socketio.on("register_view")
def register_view(data):
    code = str(data.get("code", "")).strip().upper()
    is_host = bool(data.get("is_host", False))
    team_name = str(data.get("team_name", "")).strip()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    join_room(code)

    # Clear any stale sid mapping for this sid first
    old_identity = game["players_by_sid"].get(sid)
    if old_identity and old_identity != "HOST":
        if game["team_sids"].get(old_identity) == sid:
            game["team_sids"].pop(old_identity, None)
    game["players_by_sid"].pop(sid, None)

    if is_host:
        if game["host_sid"] and game["host_sid"] != sid:
            game["players_by_sid"].pop(game["host_sid"], None)

        game["host_sid"] = sid
        game["host_connected"] = True
        game["players_by_sid"][sid] = "HOST"

        emit("registered", {
            "role_type": "HOST",
            "code": code,
            "state": game["state"]
        }, to=sid)

    else:
        if not team_name:
            emit("error", "Missing team name.")
            return

        if team_name not in game["teams"]:
            if game["state"] not in ("lobby", "agreement"):
                emit("error", "You cannot join after the competition has started.")
                return
            game["teams"].append(team_name)
            game["scores"][team_name] = 0

        old_sid = game["team_sids"].get(team_name)
        if old_sid and old_sid != sid:
            game["players_by_sid"].pop(old_sid, None)

        game["team_sids"][team_name] = sid
        game["players_by_sid"][sid] = team_name

        emit("registered", {
            "role_type": "TEAM",
            "code": code,
            "team_name": team_name,
            "state": game["state"]
        }, to=sid)

    emit_roster_update(code)

    # Sync the current view for late refreshes/rejoins
    if game["state"] == "agreement":
        emit("agreement_phase", {
            "message": "All teams must agree/ready before the game begins."
        }, to=sid)
    elif game["state"] == "role":
        emit_round_header(code)
        send_private_role_info(code)
    elif game["state"] == "phrase":
        emit_round_header(code)
        send_private_role_info(code)
        current_team = game["order"][game["current_turn_index"]] if game["current_turn_index"] < len(game["order"]) else None
        emit("start_turn", {
            "current_team": current_team,
            "turn_index": game["current_turn_index"] + 1,
            "total_turns": len(game["order"]),
            "seconds": game["phrase_time_limit"],
            "responses": game["responses"],
        }, to=sid)
    elif game["state"] == "voting":
        emit("start_voting", {
            "teams": game["teams"],
            "responses": game["responses"],
            "time_limit": game["vote_time_limit"],
        }, to=sid)
    elif game["state"] == "paused_after_result":
        emit("status_message", {
            "message": "Round results are being shown. Waiting for host to continue."
        }, to=sid)
    elif game["state"] == "game_over":
        sorted_scores = sorted(
            game["scores"].items(),
            key=lambda item: (-item[1], item[0].lower())
        )
        emit("game_over", {
            "scores": game["scores"],
            "sorted_scores": sorted_scores,
            "top_three": sorted_scores[:3],
            "winner": sorted_scores[0] if sorted_scores else None
        }, to=sid)


@socketio.on("set_round_count")
def set_round_count(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if sid != game["host_sid"]:
        emit("error", "Only the host can change the round count.")
        return

    if game["state"] not in ("lobby", "agreement"):
        emit("error", "Rounds can only be changed before gameplay starts.")
        return

    try:
        rounds = int(data.get("rounds", 3))
    except Exception:
        emit("error", "Round count must be a number.")
        return

    if rounds < 1 or rounds > 20:
        emit("error", "Round count must be between 1 and 20.")
        return

    game["max_rounds"] = rounds
    emit_roster_update(code)
    socketio.emit("round_count_updated", {"max_rounds": rounds}, room=code)


@socketio.on("start_game_request")
def start_game_request(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]

    if sid != game["host_sid"]:
        emit("error", "Only the host can start the game.")
        return

    if len(game["teams"]) < 3:
        emit("error", "You need at least 3 teams to start.")
        return

    game["state"] = "agreement"
    game["round"] = 1
    game["impostor"] = None
    game["word"] = None
    game["order"] = []
    game["responses"] = {}
    game["votes"] = {}
    game["agreement_ready"] = set()
    game["additional_round_voters"] = set()
    game["turn_token"] += 1
    game["vote_token"] += 1

    for team in game["teams"]:
        game["scores"][team] = 0

    emit_roster_update(code)
    socketio.emit("agreement_phase", {
        "message": "Agreement phase started. Every team must press READY / AGREE."
    }, room=code)


@socketio.on("agree_ready")
def agree_ready(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if game["state"] != "agreement":
        emit("error", "Agreement phase is not active.")
        return

    team_name = game["players_by_sid"].get(sid)
    if not team_name or team_name == "HOST":
        emit("error", "Only teams can ready up here.")
        return

    game["agreement_ready"].add(team_name)
    emit_roster_update(code)
    socketio.emit("agreement_update", {
        "ready_teams": sorted(list(game["agreement_ready"])),
        "total_teams": len(game["teams"]),
    }, room=code)

    if all_teams_agreed(game):
        begin_round(code, preserve_roles=False)


@socketio.on("host_continue")
def host_continue(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if sid != game["host_sid"]:
        emit("error", "Only the host can continue.")
        return

    if game["state"] == "role":
        start_phrase_phase(code)
        return

    if game["state"] == "paused_after_result":
        advance_after_result(code)
        return

    emit("error", "There is no host-controlled continue action right now.")


@socketio.on("submit_phrase")
def submit_phrase(data):
    code = str(data.get("code", "")).strip().upper()
    phrase = sanitize_phrase(data.get("phrase"))
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]

    if game["state"] != "phrase":
        emit("error", "Phrase submission is not active.")
        return

    team_name = game["players_by_sid"].get(sid)
    if not team_name or team_name == "HOST":
        emit("error", "Only teams can submit phrases.")
        return

    current_team = game["order"][game["current_turn_index"]]
    if team_name != current_team:
        emit("error", "It is not your turn.")
        return

    if not phrase:
        emit("error", "Please enter a phrase.")
        return

    word_count = len(phrase.split())
    if word_count > 3:
        emit("error", "Your phrase must be no longer than 3 words.")
        return

    if game["word"] and phrase.lower() == game["word"].lower():
        emit("error", "You cannot directly say the given word.")
        return

    game["responses"][team_name] = phrase
    game["turn_token"] += 1

    socketio.emit("phrase_locked", {
        "team": team_name,
        "phrase": phrase,
        "auto_submitted": False
    }, room=code)

    game["current_turn_index"] += 1
    socketio.sleep(1)
    start_next_turn(code)


@socketio.on("submit_vote")
def submit_vote(data):
    code = str(data.get("code", "")).strip().upper()
    voted_team = str(data.get("voted_team", "")).strip()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]

    if game["state"] != "voting":
        emit("error", "Voting is not active.")
        return

    voter_team = game["players_by_sid"].get(sid)
    if not voter_team or voter_team == "HOST":
        emit("error", "Only teams can vote.")
        return

    if voter_team in game["votes"]:
        emit("error", "Your team has already voted.")
        return

    if voted_team == "ADDITIONAL_ROUND":
        game["votes"][voter_team] = "ADDITIONAL_ROUND"
        game["additional_round_voters"].add(voter_team)
        emit("vote_received", {"message": "You voted for an additional round."}, to=sid)
    else:
        if voted_team not in game["teams"]:
            emit("error", "Invalid team selected.")
            return

        if voted_team == voter_team:
            emit("error", "You cannot vote for your own team.")
            return

        game["votes"][voter_team] = voted_team
        emit("vote_received", {"message": f"You voted for {voted_team}."}, to=sid)

    if len(game["votes"]) >= len(game["teams"]):
        game["vote_token"] += 1
        calculate_round_result(code)


@socketio.on("restart_game")
def restart_game(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if sid != game["host_sid"]:
        emit("error", "Only the host can restart the game.")
        return

    reset_to_agreement(code)


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid

    for code, game in games.items():
        identity = game["players_by_sid"].pop(sid, None)

        if identity == "HOST":
            if game["host_sid"] == sid:
                game["host_connected"] = False
            continue

        if identity and identity != "HOST":
            if game["team_sids"].get(identity) == sid:
                game["team_sids"].pop(identity, None)

        # Teams remain registered even if they disconnect.
        # That way refreshes/rejoins do not erase the lobby.
        emit_roster_update(code)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)