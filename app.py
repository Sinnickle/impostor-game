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

PHRASE_TIME_LIMIT = 30
VOTING_TIME_LIMIT = 120

games = {}


def make_code(length=5):
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
        if code not in games:
            return code


def sanitize_phrase(phrase):
    if phrase is None:
        return ""
    return " ".join(str(phrase).strip().split())


def create_game_state():
    return {
        "host_sid": None,
        "host_connected": False,

        "teams": [],
        "team_sids": {},
        "players_by_sid": {},

        "state": "lobby",  # lobby, intro_wait, intro_playing, agreement, role, phrase, voting, paused_after_result, game_over
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
        "intro_ready": set(),
        "intro_finished": set(),
        "additional_round_voters": set(),

        "turn_token": 0,
        "vote_token": 0,
    }


def all_teams_intro_finished(game):
    return len(game["teams"]) > 0 and len(game["intro_finished"]) == len(game["teams"])


def all_teams_agreed(game):
    return len(game["teams"]) >= 3 and len(game["agreement_ready"]) == len(game["teams"])


def emit_status(code, message):
    socketio.emit("status_message", {"message": message}, room=code)


def get_host_button_mode(game):
    if game["state"] == "lobby":
        return "start"
    if game["state"] == "game_over":
        return "restart_game"
    if game["state"] in {"role", "phrase", "voting", "paused_after_result"}:
        return "restart_round"
    return "none"


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
        "intro_ready": sorted(list(game["intro_ready"])),
        "intro_finished": sorted(list(game["intro_finished"])),
        "host_button_mode": get_host_button_mode(game),
        "host_can_continue": (
            (game["state"] == "agreement" and all_teams_agreed(game)) or
            (game["state"] == "role") or
            (game["state"] == "paused_after_result")
        ),
    }, room=code)


def emit_waiting_screen_to_player(sid, team_name):
    socketio.emit("player_waiting_screen", {"team_name": team_name}, to=sid)


def emit_ready_screen_to_player(sid, team_name):
    socketio.emit("player_ready_prompt", {"team_name": team_name}, to=sid)


def emit_intro_video_to_player(sid, team_name):
    socketio.emit("player_intro_video", {"team_name": team_name}, to=sid)


def emit_individual_agreement_to_player(code, sid):
    game = games[code]
    socketio.emit("agreement_phase", {
        "message": f"All teams must agree/ready before Round {game['round']} begins."
    }, to=sid)


def move_all_players_to_waiting(code):
    game = games[code]
    for team in game["teams"]:
        sid = game["team_sids"].get(team)
        if sid:
            emit_waiting_screen_to_player(sid, team)


def move_all_players_to_ready(code):
    game = games[code]
    for team in game["teams"]:
        sid = game["team_sids"].get(team)
        if sid:
            emit_ready_screen_to_player(sid, team)


def emit_private_role_info(code):
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
            }, to=sid)
        else:
            socketio.emit("role_assignment", {
                "role": "HUMAN",
                "word": game["word"],
                "round": game["round"],
                "max_rounds": game["max_rounds"],
                "order": game["order"],
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


def emit_round_started(code, preserved=False):
    game = games[code]
    socketio.emit("round_started", {
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "order": game["order"],
        "preserved": preserved,
    }, room=code)


def move_to_agreement(code, message=None):
    game = games[code]
    game["state"] = "agreement"
    emit_roster_update(code)
    socketio.emit("agreement_phase", {
        "message": message or f"All teams must agree/ready before Round {game['round']} begins."
    }, room=code)


def begin_round(code, preserved=False, preserve_order=False):
    game = games[code]

    game["state"] = "role"
    game["responses"] = {}
    game["votes"] = {}
    game["additional_round_voters"] = set()
    game["current_turn_index"] = 0
    game["turn_token"] += 1
    game["vote_token"] += 1

    if not preserved:
        game["impostor"] = random.choice(game["teams"])
        game["word"] = random.choice(WORD_LIST)

    if not preserved or not preserve_order or not game["order"]:
        game["order"] = game["teams"][:]
        random.shuffle(game["order"])

    emit_roster_update(code)
    emit_round_started(code, preserved=preserved)
    emit_private_role_info(code)

    if preserved:
        emit_status(code, f"Round {game['round']} restarted. Same impostor, same word, same turn order.")
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
        "seconds": PHRASE_TIME_LIMIT,
        "responses": game["responses"],
    }, room=code)

    socketio.start_background_task(run_phrase_timer, code, current_team, token, PHRASE_TIME_LIMIT)


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
            "auto_submitted": True,
            "responses": game["responses"]
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
        "time_limit": VOTING_TIME_LIMIT,
    }, room=code)

    socketio.start_background_task(run_vote_timer, code, token, VOTING_TIME_LIMIT)


def run_vote_timer(code, token, seconds):
    for remaining in range(seconds, 0, -1):
        game = games.get(code)
        if not game:
            return
        if game["state"] != "voting":
            return
        if game["vote_token"] != token:
            return

        socketio.emit("vote_timer_update", {"seconds_left": remaining}, room=code)
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
    if game["state"] != "voting":
        return

    vote_targets = []
    for team in game["teams"]:
        voted = game["votes"].get(team)
        if voted == "ADDITIONAL_ROUND":
            game["additional_round_voters"].add(team)
        elif voted:
            vote_targets.append(voted)

    additional_round_votes = len(game["additional_round_voters"])
    total_teams = len(game["teams"])
    additional_round_triggered = additional_round_votes > (total_teams / 2)

    individual_correct = []
    for voter_team, voted_team in game["votes"].items():
        if voted_team == game["impostor"]:
            individual_correct.append(voter_team)
            game["scores"][voter_team] += 1

    majority_team = None
    majority_correct = False
    result_text = ""

    if additional_round_triggered:
        game["state"] = "paused_after_result"
        emit_roster_update(code)

        socketio.emit("round_result", {
            "scores": game["scores"],
            "responses": game["responses"],
            "additional_round_triggered": True,
            "additional_round_votes": additional_round_votes,
            "majority_team": None,
            "actual_impostor": game["impostor"],
            "majority_correct": False,
            "individual_correct_teams": individual_correct,
            "result_text": "An additional round was approved. Same round, same roles, same word, same turn order.",
        }, room=code)
        return

    if vote_targets:
        counts = Counter(vote_targets)
        top_count = max(counts.values())
        top_teams = [team for team, count in counts.items() if count == top_count]
        if len(top_teams) == 1:
            majority_team = top_teams[0]

    if majority_team == game["impostor"]:
        majority_correct = True
        for team in game["teams"]:
            if team != game["impostor"]:
                game["scores"][team] += 2
        result_text = "The impostor was caught. All human teams gain +2 points."
    else:
        game["scores"][game["impostor"]] += 4
        result_text = "The impostor survived. The impostor team gains +4 points."

    game["state"] = "paused_after_result"
    emit_roster_update(code)

    socketio.emit("round_result", {
        "scores": game["scores"],
        "responses": game["responses"],
        "additional_round_triggered": False,
        "additional_round_votes": additional_round_votes,
        "majority_team": majority_team,
        "actual_impostor": game["impostor"],
        "majority_correct": majority_correct,
        "individual_correct_teams": individual_correct,
        "result_text": result_text,
    }, room=code)


def send_full_sync_to_sid(code, sid, is_host, team_name):
    game = games[code]

    emit("registered", {
        "role_type": "HOST" if is_host else "TEAM",
        "code": code,
        "team_name": team_name,
        "state": game["state"]
    }, to=sid)

    emit("roster_update", {
        "code": code,
        "teams": game["teams"],
        "scores": game["scores"],
        "state": game["state"],
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "agreement_ready": sorted(list(game["agreement_ready"])),
        "intro_ready": sorted(list(game["intro_ready"])),
        "intro_finished": sorted(list(game["intro_finished"])),
        "host_button_mode": get_host_button_mode(game),
        "host_can_continue": (
            (game["state"] == "agreement" and all_teams_agreed(game)) or
            (game["state"] == "role") or
            (game["state"] == "paused_after_result")
        ),
    }, to=sid)

    if is_host:
        if game["state"] == "agreement":
            emit("agreement_phase", {
                "message": f"All teams must agree/ready before Round {game['round']} begins."
            }, to=sid)
        elif game["state"] == "role":
            emit_round_started(code)
            emit_private_role_info(code)
        elif game["state"] == "phrase":
            emit_round_started(code)
            emit_private_role_info(code)
            current_team = game["order"][game["current_turn_index"]] if game["current_turn_index"] < len(game["order"]) else None
            emit("start_turn", {
                "current_team": current_team,
                "turn_index": game["current_turn_index"] + 1,
                "total_turns": len(game["order"]),
                "seconds": PHRASE_TIME_LIMIT,
                "responses": game["responses"],
            }, to=sid)
        elif game["state"] == "voting":
            emit("start_voting", {
                "teams": game["teams"],
                "responses": game["responses"],
                "time_limit": VOTING_TIME_LIMIT,
            }, to=sid)
        elif game["state"] == "paused_after_result":
            emit("status_message", {
                "message": "Round results are being shown. Waiting for host action."
            }, to=sid)
        elif game["state"] == "game_over":
            sorted_scores = sorted(
                game["scores"].items(),
                key=lambda item: (-item[1], item[0].lower())
            )
            emit("game_over", {
                "scores": game["scores"],
                "sorted_scores": sorted_scores
            }, to=sid)
        return

    if game["state"] == "lobby":
        emit_waiting_screen_to_player(sid, team_name)

    elif game["state"] in {"intro_wait", "intro_playing"}:
        if team_name in game["intro_finished"]:
            emit_individual_agreement_to_player(code, sid)
        elif team_name in game["intro_ready"]:
            emit_intro_video_to_player(sid, team_name)
        else:
            emit_ready_screen_to_player(sid, team_name)

    elif game["state"] == "agreement":
        emit("agreement_phase", {
            "message": f"All teams must agree/ready before Round {game['round']} begins."
        }, to=sid)

    elif game["state"] == "role":
        emit_round_started(code)
        emit_private_role_info(code)

    elif game["state"] == "phrase":
        emit_round_started(code)
        emit_private_role_info(code)
        current_team = game["order"][game["current_turn_index"]] if game["current_turn_index"] < len(game["order"]) else None
        emit("start_turn", {
            "current_team": current_team,
            "turn_index": game["current_turn_index"] + 1,
            "total_turns": len(game["order"]),
            "seconds": PHRASE_TIME_LIMIT,
            "responses": game["responses"],
        }, to=sid)

    elif game["state"] == "voting":
        emit("start_voting", {
            "teams": game["teams"],
            "responses": game["responses"],
            "time_limit": VOTING_TIME_LIMIT,
        }, to=sid)

    elif game["state"] == "paused_after_result":
        emit("status_message", {
            "message": "Round results are being shown. Waiting for host action."
        }, to=sid)

    elif game["state"] == "game_over":
        sorted_scores = sorted(
            game["scores"].items(),
            key=lambda item: (-item[1], item[0].lower())
        )
        emit("game_over", {
            "scores": game["scores"],
            "sorted_scores": sorted_scores
        }, to=sid)


def reset_to_agreement_new_game(code):
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
    game["intro_ready"] = set()
    game["intro_finished"] = set()
    game["additional_round_voters"] = set()
    game["scores"] = {team: 0 for team in game["teams"]}
    game["turn_token"] += 1
    game["vote_token"] += 1
    emit_roster_update(code)
    socketio.emit("agreement_phase", {
        "message": "All teams must agree/ready before Round 1 begins."
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
    emit("game_created", {"code": code}, to=request.sid)


@socketio.on("register_view")
def register_view(data):
    code = str(data.get("code", "")).strip().upper()
    is_host = bool(data.get("is_host"))
    team_name = str(data.get("team_name", "")).strip()

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    sid = request.sid
    join_room(code)

    if is_host:
        game["host_sid"] = sid
        game["host_connected"] = True
        game["players_by_sid"][sid] = "HOST"
        send_full_sync_to_sid(code, sid, True, "")
        emit_status(code, "Host connected.")
        return

    if not team_name:
        emit("error", "Team name required.")
        return

    if team_name not in game["teams"]:
        if game["state"] != "lobby":
            emit("error", "Cannot join after the game has already started.")
            return
        game["teams"].append(team_name)
        game["scores"][team_name] = 0

    old_sid = game["team_sids"].get(team_name)
    if old_sid and old_sid != sid:
        game["players_by_sid"].pop(old_sid, None)

    game["team_sids"][team_name] = sid
    game["players_by_sid"][sid] = team_name

    emit_roster_update(code)
    send_full_sync_to_sid(code, sid, False, team_name)


@socketio.on("set_round_count")
def set_round_count(data):
    code = str(data.get("code", "")).strip().upper()
    rounds = int(data.get("rounds", 3))
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if sid != game["host_sid"]:
        emit("error", "Only the host can change round count.")
        return

    if game["state"] != "lobby":
        emit("error", "Round count can only be changed in the lobby.")
        return

    rounds = max(1, min(20, rounds))
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

    if game["state"] != "lobby":
        emit("error", "Start Game is only available from the lobby.")
        return

    if len(game["teams"]) < 3:
        emit("error", "At least 3 teams are required.")
        return

    game["state"] = "intro_wait"
    game["intro_ready"] = set()
    game["intro_finished"] = set()
    game["agreement_ready"] = set()

    emit_roster_update(code)
    move_all_players_to_ready(code)
    emit_status(code, "Players are now being shown the READY button.")


@socketio.on("player_intro_ready")
def player_intro_ready(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if game["state"] not in {"intro_wait", "intro_playing"}:
        emit("error", "Intro READY is not available right now.")
        return

    team_name = game["players_by_sid"].get(sid)
    if not team_name or team_name == "HOST":
        emit("error", "Only players can do that.")
        return

    if team_name in game["intro_finished"]:
        return

    game["intro_ready"].add(team_name)
    emit_roster_update(code)

    emit_intro_video_to_player(sid, team_name)
    emit_status(code, f"{team_name} is ready for the intro.")

    if game["state"] == "intro_wait":
        game["state"] = "intro_playing"
        emit_roster_update(code)


@socketio.on("player_intro_finished")
def player_intro_finished(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if game["state"] not in {"intro_wait", "intro_playing"}:
        emit("error", "Intro completion is not valid right now.")
        return

    team_name = game["players_by_sid"].get(sid)
    if not team_name or team_name == "HOST":
        emit("error", "Only players can do that.")
        return

    # finishing intro also auto-counts as agreement ready
    game["intro_finished"].add(team_name)
    game["agreement_ready"].add(team_name)

    emit_roster_update(code)
    emit_status(code, f"{team_name} finished the intro and is ready.")

    emit_individual_agreement_to_player(code, sid)

    if all_teams_intro_finished(game):
        move_to_agreement(code, message="All teams are ready. Host can press Continue to begin Round 1.")


@socketio.on("agree_ready")
def agree_ready(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if game["state"] not in {"intro_wait", "intro_playing", "agreement"}:
        emit("error", "Agreement phase is not active.")
        return

    team_name = game["players_by_sid"].get(sid)
    if not team_name or team_name == "HOST":
        emit("error", "Only players can agree.")
        return

    if team_name not in game["intro_finished"]:
        emit("error", "You must finish the intro before agreeing.")
        return

    if team_name in game["agreement_ready"]:
        emit("agreement_update", {
            "ready_teams": sorted(list(game["agreement_ready"])),
            "total_teams": len(game["teams"])
        }, to=sid)
        return

    game["agreement_ready"].add(team_name)
    emit_roster_update(code)

    socketio.emit("agreement_update", {
        "ready_teams": sorted(list(game["agreement_ready"])),
        "total_teams": len(game["teams"])
    }, room=code)

    if all_teams_agreed(game):
        emit_status(code, "All teams are ready. Host can continue to begin the round.")
    else:
        emit_status(code, f"{team_name} is ready.")


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

    if game["state"] == "agreement":
        if not all_teams_agreed(game):
            emit("error", "Not all teams are ready yet.")
            return
        begin_round(code, preserved=False, preserve_order=False)
        return

    if game["state"] == "role":
        start_phrase_phase(code)
        return

    if game["state"] == "paused_after_result":
        if len(game["additional_round_voters"]) > (len(game["teams"]) / 2):
            begin_round(code, preserved=True, preserve_order=True)
            return

        if game["round"] >= game["max_rounds"]:
            game["state"] = "game_over"
            emit_roster_update(code)

            sorted_scores = sorted(
                game["scores"].items(),
                key=lambda item: (-item[1], item[0].lower())
            )
            socketio.emit("game_over", {
                "scores": game["scores"],
                "sorted_scores": sorted_scores
            }, room=code)
            emit_status(code, "Game over.")
            return

        # IMPORTANT: go directly to next round, no more agreement phase
        game["round"] += 1
        begin_round(code, preserved=False, preserve_order=False)
        return

    emit("error", "Continue is not available right now.")


@socketio.on("restart_round")
def restart_round(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if sid != game["host_sid"]:
        emit("error", "Only the host can restart the round.")
        return

    if game["state"] not in {"role", "phrase", "voting", "paused_after_result"}:
        emit("error", "Restart Round is only available during an active round.")
        return

    begin_round(code, preserved=True, preserve_order=True)


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

    if game["state"] != "game_over":
        emit("error", "Restart Game is only available after the game ends.")
        return

    reset_to_agreement_new_game(code)
    emit_status(code, "Game reset. Back to agreement phase at Round 1.")


@socketio.on("submit_phrase")
def submit_phrase(data):
    code = str(data.get("code", "")).strip().upper()
    phrase = sanitize_phrase(data.get("phrase", ""))
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
        emit("error", "Only players can submit phrases.")
        return

    current_team = game["order"][game["current_turn_index"]]
    if team_name != current_team:
        emit("error", "It is not your turn.")
        return

    if team_name in game["responses"]:
        emit("error", "Your team already submitted.")
        return

    if not phrase:
        emit("error", "Phrase cannot be empty.")
        return

    if len(phrase.split()) > 3:
        emit("error", "Phrase must be 3 words or fewer.")
        return

    game["responses"][team_name] = phrase
    socketio.emit("phrase_locked", {
        "team": team_name,
        "phrase": phrase,
        "auto_submitted": False,
        "responses": game["responses"]
    }, room=code)

    game["current_turn_index"] += 1
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
        emit("error", "Only players can vote.")
        return

    if voter_team in game["votes"]:
        emit("error", "Your team already voted.")
        return

    if voted_team == "ADDITIONAL_ROUND":
        game["votes"][voter_team] = voted_team
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

        emit_roster_update(code)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)