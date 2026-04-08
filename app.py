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

        # lobby, intro_wait, intro_playing, agreement, role, phrase, voting, paused_after_result, game_over
        "state": "lobby",
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

        "skip_intro": False,

        "turn_token": 0,
        "vote_token": 0,
    }


def current_turn_team(game):
    if not game["order"]:
        return None
    if game["current_turn_index"] < 0 or game["current_turn_index"] >= len(game["order"]):
        return None
    return game["order"][game["current_turn_index"]]


def build_sync_payload(code, game, for_host=False, team_name=""):
    sorted_scores = sorted(
        game["scores"].items(),
        key=lambda item: (-item[1], item[0].lower())
    )

    my_role = None
    my_word = None
    if not for_host and team_name:
        if game["impostor"] == team_name:
            my_role = "IMPOSTOR"
        elif game["word"]:
            my_role = "HUMAN"
            my_word = game["word"]

    return {
        "code": code,
        "state": game["state"],
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "teams": list(game["teams"]),
        "scores": game["scores"],
        "sorted_scores": sorted_scores,
        "order": list(game["order"]),
        "current_turn": current_turn_team(game),
        "responses": game["responses"],
        "votes_count": len(game["votes"]),
        "impostor": game["impostor"] if for_host else None,
        "word": game["word"] if for_host else None,
        "my_role": my_role,
        "my_word": my_word,
        "skip_intro": game["skip_intro"],
        "intro_finished_count": len(game["intro_finished"]),
        "intro_total_count": len(game["teams"]),
    }


def send_full_sync_to_sid(code, sid, for_host=False, team_name=""):
    game = games[code]
    emit("full_sync", build_sync_payload(code, game, for_host, team_name), to=sid)


def emit_roster_update(code):
    if code not in games:
        return
    game = games[code]
    socketio.emit("roster_update", {
        "teams": list(game["teams"]),
        "scores": game["scores"],
        "sorted_scores": sorted(
            game["scores"].items(),
            key=lambda item: (-item[1], item[0].lower())
        ),
        "round": game["round"],
        "max_rounds": game["max_rounds"],
    }, room=code)


def emit_status(code, message):
    if code not in games:
        return
    socketio.emit("status_update", {"message": message}, room=code)


def choose_impostor_and_word(code):
    game = games[code]
    if not game["teams"]:
        game["impostor"] = None
        game["word"] = None
        return
    game["impostor"] = random.choice(game["teams"])
    game["word"] = random.choice(WORD_LIST)


def emit_role_reveal(code):
    game = games[code]
    for team in game["teams"]:
        sid = game["team_sids"].get(team)
        if not sid:
            continue
        if team == game["impostor"]:
            emit("role_reveal", {
                "role": "IMPOSTOR",
                "word": None,
                "round": game["round"],
                "max_rounds": game["max_rounds"]
            }, to=sid)
        else:
            emit("role_reveal", {
                "role": "HUMAN",
                "word": game["word"],
                "round": game["round"],
                "max_rounds": game["max_rounds"]
            }, to=sid)

    if game["host_sid"]:
        emit("host_role_info", {
            "impostor": game["impostor"],
            "word": game["word"],
            "round": game["round"],
            "max_rounds": game["max_rounds"]
        }, to=game["host_sid"])


def emit_round_started(code, preserved=False):
    game = games[code]
    socketio.emit("round_started", {
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "order": game["order"],
        "preserved": preserved
    }, room=code)


def begin_round(code, preserved=False, preserve_order=False):
    if code not in games:
        return

    game = games[code]

    if not game["teams"]:
        game["state"] = "lobby"
        emit_status(code, "No teams are in the room.")
        emit_roster_update(code)
        return

    game["state"] = "role"
    game["responses"] = {}
    game["votes"] = {}
    game["additional_round_voters"] = set()
    game["current_turn_index"] = 0
    game["turn_token"] += 1
    game["vote_token"] += 1

    if not preserved or not game["impostor"] or not game["word"]:
        choose_impostor_and_word(code)

    if not preserve_order or not game["order"]:
        game["order"] = list(game["teams"])
        random.shuffle(game["order"])
    else:
        game["order"] = [team for team in game["order"] if team in game["teams"]]
        missing = [team for team in game["teams"] if team not in game["order"]]
        game["order"].extend(missing)

    emit_round_started(code, preserved=preserved)
    emit_role_reveal(code)
    emit_roster_update(code)
    emit_status(code, f"Round {game['round']} role reveal.")

    socketio.emit("show_role_stage", {
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "order": game["order"]
    }, room=code)


def start_phrase_phase(code):
    if code not in games:
        return
    game = games[code]
    game["state"] = "phrase"
    game["current_turn_index"] = 0
    game["turn_token"] += 1
    emit_status(code, "Phrase submission has started.")
    start_next_turn(code)


def start_next_turn(code):
    if code not in games:
        return

    game = games[code]

    while game["current_turn_index"] < len(game["order"]) and game["order"][game["current_turn_index"]] in game["responses"]:
        game["current_turn_index"] += 1

    if game["current_turn_index"] >= len(game["order"]):
        begin_voting_phase(code)
        return

    current_team = game["order"][game["current_turn_index"]]
    token = game["turn_token"]

    socketio.emit("turn_started", {
        "team": current_team,
        "token": token,
        "time_limit": PHRASE_TIME_LIMIT,
        "order": game["order"],
        "responses": game["responses"]
    }, room=code)

    def auto_advance_if_needed(room_code, expected_token, expected_team):
        game_now = games.get(room_code)
        if not game_now:
            return
        if game_now["state"] != "phrase":
            return
        if game_now["turn_token"] != expected_token:
            return
        if game_now["current_turn_index"] >= len(game_now["order"]):
            return
        if game_now["order"][game_now["current_turn_index"]] != expected_team:
            return
        if expected_team in game_now["responses"]:
            return

        game_now["responses"][expected_team] = "[NO PHRASE]"
        socketio.emit("phrase_locked", {
            "team": expected_team,
            "phrase": "[NO PHRASE]",
            "auto_submitted": True,
            "responses": game_now["responses"]
        }, room=room_code)

        game_now["current_turn_index"] += 1
        start_next_turn(room_code)

    socketio.start_background_task(
        delayed_call,
        PHRASE_TIME_LIMIT,
        auto_advance_if_needed,
        code,
        token,
        current_team
    )


def begin_voting_phase(code):
    if code not in games:
        return
    game = games[code]
    game["state"] = "voting"
    game["votes"] = {}
    game["vote_token"] += 1

    socketio.emit("voting_started", {
        "time_limit": VOTING_TIME_LIMIT,
        "teams": game["teams"],
        "responses": game["responses"],
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "token": game["vote_token"]
    }, room=code)

    emit_status(code, "Voting phase has started.")

    def auto_finish_voting(room_code, expected_token):
        game_now = games.get(room_code)
        if not game_now:
            return
        if game_now["state"] != "voting":
            return
        if game_now["vote_token"] != expected_token:
            return
        calculate_round_result(room_code)

    socketio.start_background_task(
        delayed_call,
        VOTING_TIME_LIMIT,
        auto_finish_voting,
        code,
        game["vote_token"]
    )


def delayed_call(seconds, fn, *args):
    socketio.sleep(seconds)
    fn(*args)


def calculate_round_result(code):
    if code not in games:
        return

    game = games[code]
    if game["state"] != "voting":
        return

    game["state"] = "paused_after_result"

    votes = dict(game["votes"])
    vote_counts = Counter(votes.values())

    additional_round_votes = vote_counts.get("ADDITIONAL_ROUND", 0)

    if additional_round_votes > len(game["teams"]) / 2:
        game["additional_round_voters"] = {team for team, target in votes.items() if target == "ADDITIONAL_ROUND"}

        socketio.emit("round_result", {
            "mode": "additional_round",
            "message": "Majority voted for an additional round. Same roles, same word, same impostor. No one is revealed and no points are awarded.",
            "round": game["round"],
            "max_rounds": game["max_rounds"],
            "scores": game["scores"],
            "impostor": None,
            "majority_target": "ADDITIONAL_ROUND"
        }, room=code)

        emit_status(code, "Additional round selected. Waiting for host to continue.")
        return

    valid_vote_counts = Counter({k: v for k, v in vote_counts.items() if k != "ADDITIONAL_ROUND"})
    majority_target = None

    if valid_vote_counts:
        top_count = max(valid_vote_counts.values())
        top_targets = [team for team, count in valid_vote_counts.items() if count == top_count]
        if len(top_targets) == 1:
            majority_target = top_targets[0]

    individually_correct = []
    for voter, target in votes.items():
        if target == game["impostor"]:
            individually_correct.append(voter)
            if voter in game["scores"]:
                game["scores"][voter] += 1

    majority_correct = (majority_target == game["impostor"])
    if majority_correct:
        for team in game["teams"]:
            if team in game["scores"]:
                game["scores"][team] += 2

    message_lines = []
    message_lines.append(f"The impostor was: {game['impostor']}.")
    if majority_target:
        message_lines.append(f"Majority vote target: {majority_target}.")
    else:
        message_lines.append("No single majority target was reached.")

    if majority_correct:
        message_lines.append("Majority guessed correctly: every team gets +2.")
    else:
        message_lines.append("Majority did not correctly identify the impostor: +0 majority points.")

    if individually_correct:
        message_lines.append(
            "Teams that individually guessed the impostor correctly (+1): "
            + ", ".join(individually_correct)
        )
    else:
        message_lines.append("No team individually guessed the impostor correctly.")

    socketio.emit("round_result", {
        "mode": "normal",
        "message": " ".join(message_lines),
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "scores": game["scores"],
        "impostor": game["impostor"],
        "majority_target": majority_target,
        "individually_correct": individually_correct,
        "majority_correct": majority_correct
    }, room=code)

    emit_roster_update(code)
    emit_status(code, "Round result ready. Host may continue.")


def move_to_next_round_or_end(code):
    if code not in games:
        return
    game = games[code]

    if game["round"] >= game["max_rounds"]:
        game["state"] = "game_over"
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

    game["round"] += 1
    begin_round(code, preserved=False, preserve_order=False)


def reset_to_round_one_new_game(code):
    game = games[code]

    game["round"] = 1
    game["state"] = "role"

    game["impostor"] = None
    game["word"] = None
    game["order"] = []
    game["current_turn_index"] = 0

    game["responses"] = {}
    game["votes"] = {}
    game["additional_round_voters"] = set()

    game["scores"] = {team: 0 for team in game["teams"]}

    game["agreement_ready"] = set()
    game["intro_ready"] = set()
    game["intro_finished"] = set()

    game["turn_token"] += 1
    game["vote_token"] += 1

    begin_round(code, preserved=False, preserve_order=False)


def prune_team_from_game(code, team_name):
    game = games.get(code)
    if not game or not team_name or team_name == "HOST":
        return

    if team_name in game["teams"]:
        game["teams"].remove(team_name)

    game["team_sids"].pop(team_name, None)
    game["scores"].pop(team_name, None)

    game["agreement_ready"].discard(team_name)
    game["intro_ready"].discard(team_name)
    game["intro_finished"].discard(team_name)
    game["additional_round_voters"].discard(team_name)

    game["responses"].pop(team_name, None)
    game["votes"].pop(team_name, None)

    if game["impostor"] == team_name:
        game["impostor"] = None

    removed_turn_index = None
    if team_name in game["order"]:
        removed_turn_index = game["order"].index(team_name)
        game["order"] = [t for t in game["order"] if t != team_name]

    if removed_turn_index is not None:
        if removed_turn_index < game["current_turn_index"]:
            game["current_turn_index"] = max(0, game["current_turn_index"] - 1)
        elif removed_turn_index == game["current_turn_index"]:
            if game["current_turn_index"] >= len(game["order"]):
                game["current_turn_index"] = len(game["order"])

    cleaned_votes = {}
    for voter, target in game["votes"].items():
        if voter == team_name:
            continue
        if target == team_name:
            continue
        cleaned_votes[voter] = target
    game["votes"] = cleaned_votes


def handle_player_departure(code, team_name):
    game = games.get(code)
    if not game:
        return

    active_turn_team = current_turn_team(game)
    was_current_turn = (active_turn_team == team_name)

    prune_team_from_game(code, team_name)

    socketio.emit("player_removed", {"team": team_name}, room=code)
    emit_status(code, f"{team_name} left the game.")
    emit_roster_update(code)

    if not game["teams"]:
        games.pop(code, None)
        return

    if len(game["teams"]) == 1 and game["state"] in {"phrase", "voting", "paused_after_result", "role"}:
        game["state"] = "game_over"
        sorted_scores = sorted(
            game["scores"].items(),
            key=lambda item: (-item[1], item[0].lower())
        )
        socketio.emit("game_over", {
            "scores": game["scores"],
            "sorted_scores": sorted_scores
        }, room=code)
        emit_status(code, "Game ended because only one team remains.")
        return

    if game["state"] in {"role", "phrase", "voting", "paused_after_result"} and game["impostor"] is None:
        begin_round(code, preserved=False, preserve_order=False)
        return

    if game["state"] == "phrase":
        if was_current_turn:
            start_next_turn(code)
            return

        if len(game["responses"]) >= len(game["teams"]):
            begin_voting_phase(code)
            return

        socketio.emit("turn_started", {
            "team": current_turn_team(game),
            "token": game["turn_token"],
            "time_limit": PHRASE_TIME_LIMIT,
            "order": game["order"],
            "responses": game["responses"]
        }, room=code)
        return

    if game["state"] == "voting":
        if len(game["votes"]) >= len(game["teams"]):
            game["vote_token"] += 1
            calculate_round_result(code)


def handle_host_departure(code):
    game = games.get(code)
    if not game:
        return

    socketio.emit("host_left_game", {
        "message": "The Host has left the game."
    }, room=code)

    games.pop(code, None)


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
    emit_status(code, f"Round count set to {rounds}.")
    emit("round_count_saved", {"rounds": rounds}, room=code)


@socketio.on("set_skip_intro")
def set_skip_intro(data):
    code = str(data.get("code", "")).strip().upper()
    skip_intro = bool(data.get("skip_intro"))
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if sid != game["host_sid"]:
        emit("error", "Only the host can change intro settings.")
        return

    if game["state"] != "lobby":
        emit("error", "Intro settings can only be changed in the lobby.")
        return

    game["skip_intro"] = skip_intro
    socketio.emit("skip_intro_updated", {"skip_intro": skip_intro}, room=code)


@socketio.on("start_game")
def start_game(data):
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
        emit("error", "Game has already started.")
        return

    if len(game["teams"]) < 2:
        emit("error", "At least 2 teams are required to start.")
        return

    game["agreement_ready"] = set()
    game["intro_ready"] = set()
    game["intro_finished"] = set()
    game["state"] = "intro_wait"

    socketio.emit("game_start_sequence", {
        "skip_intro": game["skip_intro"],
        "round": game["round"],
        "max_rounds": game["max_rounds"]
    }, room=code)

    emit_status(code, "Game started. Waiting for teams to begin intro.")


@socketio.on("player_ready_for_intro")
def player_ready_for_intro(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if game["state"] not in {"intro_wait", "intro_playing"}:
        emit("error", "Intro is not available right now.")
        return

    team_name = game["players_by_sid"].get(sid)
    if not team_name or team_name == "HOST":
        emit("error", "Only players can ready up.")
        return

    game["intro_ready"].add(team_name)

    if game["skip_intro"]:
        emit("player_should_skip_intro", {"code": code}, to=sid)
    else:
        emit("player_should_watch_intro", {"code": code}, to=sid)

    game["state"] = "intro_playing"

    socketio.emit("intro_progress", {
        "finished_count": len(game["intro_finished"]),
        "total_count": len(game["teams"]),
        "ready_count": len(game["intro_ready"])
    }, room=code)


@socketio.on("player_intro_finished")
def player_intro_finished(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    team_name = game["players_by_sid"].get(sid)

    if not team_name or team_name == "HOST":
        emit("error", "Only players can complete intro.")
        return

    game["intro_finished"].add(team_name)

    socketio.emit("intro_progress", {
        "finished_count": len(game["intro_finished"]),
        "total_count": len(game["teams"]),
        "ready_count": len(game["intro_ready"])
    }, room=code)

    if len(game["intro_finished"]) >= len(game["teams"]):
        game["state"] = "agreement"
        socketio.emit("all_intro_finished", {
            "message": "All teams finished the intro."
        }, room=code)
        emit_status(code, "All teams finished the intro. Host may continue.")


@socketio.on("player_skip_intro_finished")
def player_skip_intro_finished(data):
    player_intro_finished(data)


@socketio.on("continue_after_intro")
def continue_after_intro(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if sid != game["host_sid"]:
        emit("error", "Only the host can continue.")
        return

    if game["state"] not in {"agreement", "intro_playing", "intro_wait"}:
        emit("error", "Continue is not available right now.")
        return

    if len(game["intro_finished"]) < len(game["teams"]):
        emit("error", "Not all teams finished the intro yet.")
        return

    begin_round(code, preserved=False, preserve_order=False)


@socketio.on("host_continue_round")
def host_continue_round(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]

    if sid != game["host_sid"]:
        emit("error", "Only the host can continue the round.")
        return

    if game["state"] == "role":
        start_phrase_phase(code)
        return

    if game["state"] == "paused_after_result":
        if game["additional_round_voters"]:
            game["responses"] = {}
            game["votes"] = {}
            game["state"] = "role"
            begin_round(code, preserved=True, preserve_order=True)
            return

        move_to_next_round_or_end(code)
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

    if game["state"] not in {"paused_after_result", "role", "phrase", "voting"}:
        emit("error", "Restart Round is not available right now.")
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

    reset_to_round_one_new_game(code)
    emit_status(code, "Game restarted. Starting again from Round 1.")


@socketio.on("restart_action")
def restart_action(data):
    mode = str(data.get("mode", "")).strip()
    code = str(data.get("code", "")).strip().upper()

    if mode == "restart_round":
        restart_round({"code": code})
        return

    if mode == "restart_game":
        restart_game({"code": code})
        return

    emit("error", "Invalid restart mode.")


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
        emit("error", "Phrase submission is inactive.")
        return

    team_name = game["players_by_sid"].get(sid)
    if not team_name or team_name == "HOST":
        emit("error", "Only players can submit phrases.")
        return

    current_team = current_turn_team(game)
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

    socketio.emit("vote_progress", {
        "count": len(game["votes"]),
        "total": len(game["teams"])
    }, room=code)

    if len(game["votes"]) >= len(game["teams"]):
        game["vote_token"] += 1
        calculate_round_result(code)


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid

    for code, game in list(games.items()):
        identity = game["players_by_sid"].get(sid)

        if not identity:
            continue

        game["players_by_sid"].pop(sid, None)

        if identity == "HOST":
            if game["host_sid"] == sid:
                game["host_connected"] = False
                game["host_sid"] = None
                handle_host_departure(code)
            return

        if game["team_sids"].get(identity) == sid:
            game["team_sids"].pop(identity, None)

        handle_player_departure(code, identity)
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)