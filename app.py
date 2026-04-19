import os
import random
import string
import re
from collections import Counter

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
from google import genai

app = Flask(__name__)
app.config["SECRET_KEY"] = "impostor-secret-key"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

SMART_AI_BASE_NAME = "SMART AI"
SMART_AI_MODEL = "gemini-2.5-flash"

WORD_CATEGORIES = {
    "computer_science": [
        "Algorithm", "Binary", "Compiler", "Database", "Encryption",
        "Function", "Interface", "Kernel", "Loop", "Memory",
        "Network", "Object", "Packet", "Queue", "Recursion",
        "Server", "Stack", "Syntax", "Thread", "Variable",
        "Array", "Boolean", "Cache", "Class", "Cloud",
        "Debugging", "Framework", "Frontend", "Backend", "Hash",
        "Integer", "Iteration", "Library", "Machine Learning", "Pointer",
        "Runtime", "Script", "Search", "Sorting", "Terminal"
    ],
    "general": [
        "Apple", "Bridge", "Camera", "Candle", "Castle",
        "Cloud", "Coffee", "Desert", "Dragon", "Feather",
        "Forest", "Garden", "Guitar", "Island", "Jacket",
        "Lantern", "Library", "Mirror", "Mountain", "Ocean",
        "Pencil", "Planet", "Puzzle", "River", "Rocket",
        "Shadow", "Silver", "Snowflake", "Sunrise", "Treasure"
    ],
    "animals": [
        "Alligator", "Antelope", "Bat", "Bear", "Cheetah",
        "Dolphin", "Eagle", "Falcon", "Fox", "Frog",
        "Giraffe", "Hamster", "Jaguar", "Koala", "Leopard",
        "Lion", "Otter", "Panda", "Penguin", "Rabbit",
        "Raven", "Shark", "Tiger", "Turtle", "Whale",
        "Wolf", "Zebra", "Octopus", "Peacock", "Squirrel"
    ],
    "olympic_sports": [
        "Archery", "Badminton", "Boxing", "Canoeing", "Curling",
        "Cycling", "Diving", "Fencing", "Gymnastics", "Handball",
        "Hockey", "Judo", "Luge", "Rowing", "Rugby",
        "Sailing", "Shooting", "Skateboarding", "Skiing", "Snowboarding",
        "Surfing", "Swimming", "Taekwondo", "Tennis", "Triathlon",
        "Volleyball", "Water Polo", "Weightlifting", "Wrestling", "Biathlon"
    ],
    "devices": [
        "Calculator", "Camera", "Drone", "Earbuds", "Flashlight",
        "Game Console", "Headphones", "Keyboard", "Laptop", "Microphone",
        "Monitor", "Mouse", "Phone", "Printer", "Projector",
        "Remote", "Router", "Scanner", "Smartwatch", "Speaker",
        "Tablet", "Television", "Thermostat", "Walkie Talkie", "Webcam",
        "VR Headset", "Joystick", "Modem", "Hard Drive", "Charger"
    ],
}

DEFAULT_SELECTED_CATEGORIES = [
    "computer_science",
    "general",
    "animals",
    "olympic_sports",
    "devices",
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


def sanitize_theme(raw_theme):
    theme = str(raw_theme or "casino").strip().lower()
    return theme if theme in {"casino", "forest"} else "casino"


def sanitize_selected_categories(raw_categories):
    if not isinstance(raw_categories, list):
        return DEFAULT_SELECTED_CATEGORIES[:]

    clean = []
    for category in raw_categories:
        key = str(category).strip().lower()
        if key in WORD_CATEGORIES and key not in clean:
            clean.append(key)

    return clean or DEFAULT_SELECTED_CATEGORIES[:]


def get_word_pool_for_categories(selected_categories):
    pool = []
    for category in sanitize_selected_categories(selected_categories):
        pool.extend(WORD_CATEGORIES.get(category, []))

    deduped = []
    seen = set()
    for word in pool:
        lower = word.lower()
        if lower in seen:
            continue
        seen.add(lower)
        deduped.append(word)

    return deduped or WORD_CATEGORIES["general"][:]


def get_max_impostors_for_team_count(team_count):
    if team_count >= 20:
        return 4
    if team_count >= 12:
        return 3
    if team_count >= 5:
        return 2
    return 1


def clamp_impostor_count(requested_count, team_count):
    try:
        requested = int(requested_count)
    except (TypeError, ValueError):
        requested = 1

    return max(1, min(4, get_max_impostors_for_team_count(team_count), requested))


def create_game_state():
    return {
        "host_sid": None,
        "host_connected": False,

        "teams": [],
        "team_sids": {},
        "waitlisted_teams": [],
        "waitlisted_sids": {},
        "players_by_sid": {},

        # lobby, intro_wait, intro_playing, agreement, role, phrase, voting, paused_after_result, game_over
        "state": "lobby",
        "round": 1,
        "max_rounds": 3,

        "impostor_count": 1,
        "theme": "casino",
        "selected_categories": DEFAULT_SELECTED_CATEGORIES[:],
        "word_pool": get_word_pool_for_categories(DEFAULT_SELECTED_CATEGORIES),
        "word_category": None,
        "impostors": [],
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

        "smart_ai_added": False,
        "smart_ai_team": None,
    }


def is_smart_ai_team(game, team_name):
    return bool(team_name) and team_name == game.get("smart_ai_team")


def make_unique_smart_ai_name(game):
    base = SMART_AI_BASE_NAME
    if base not in game["teams"] and base not in game["waitlisted_teams"]:
        return base

    index = 2
    while True:
        candidate = f"{base} {index}"
        if candidate not in game["teams"] and candidate not in game["waitlisted_teams"]:
            return candidate
        index += 1


def contains_forbidden_word(phrase, word):
    if not phrase or not word:
        return False

    phrase_words = re.findall(r"[A-Za-z0-9]+", phrase.lower())
    word_words = re.findall(r"[A-Za-z0-9]+", word.lower())

    if not phrase_words or not word_words:
        return False

    phrase_set = set(phrase_words)
    for token in word_words:
        if token in phrase_set:
            return True
    return False


def normalize_ai_phrase(raw_text, fallback):
    text = sanitize_phrase(raw_text)
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    text = text.strip("\"'`.,:;!?-_/\\|[]{}()")

    words = text.split()
    if not words:
        return fallback

    phrase = " ".join(words[:3]).strip()
    if not phrase:
        return fallback

    return phrase


def normalize_vote_choice(raw_text, valid_choices):
    if not raw_text:
        return None

    cleaned = sanitize_phrase(raw_text).strip()
    lowered = cleaned.lower()

    for choice in valid_choices:
        if lowered == choice.lower():
            return choice

    for choice in valid_choices:
        if choice.lower() in lowered:
            return choice

    return None


def get_ai_human_fallback_phrase():
    return random.choice([
        "deep range",
        "arc shot",
        "quick click",
        "sharp logic",
        "inner memory",
        "clean code",
        "core link",
        "fast process",
    ])


def get_ai_impostor_fallback_phrase(previous_phrases):
    if previous_phrases:
        seed = sanitize_phrase(previous_phrases[0])
        seed_words = seed.split()
        if seed_words:
            return " ".join(seed_words[:3])
    return random.choice([
        "seems useful",
        "pretty common",
        "kind of technical",
        "widely used",
        "good clue",
    ])


def get_previous_phrases_for_round(game, current_team):
    phrases = []
    for team in game["order"]:
        if team == current_team:
            break
        phrase = game["responses"].get(team)
        if phrase:
            phrases.append({"team": team, "phrase": phrase})
    return phrases


def build_ai_phrase_prompt(game, ai_team):
    is_human = ai_team not in game["impostors"]
    previous = get_previous_phrases_for_round(game, ai_team)

    if is_human:
        return f"""
You are playing a social deduction word game as a HUMAN.
Your secret word is: {game['word']}

Rules:
- You MUST NOT say the secret word.
- You MUST return ONLY one phrase.
- That phrase must be NO LONGER THAN 3 WORDS.
- Do not explain.
- Do not use quotes.
- Do not use bullet points.
- Be clever and subtle.
- Your phrase should make other teams feel that you know the word without directly saying it.

Creativity example:
If the word were Pointer, a clever response could be: Three

Return only the phrase.
""".strip()

    if previous:
        previous_lines = "\n".join(
            f"- {item['team']}: {item['phrase']}"
            for item in previous
        )
        return f"""
You are playing a social deduction word game as the IMPOSTOR.
You do NOT know the secret word.
Your goal is to blend in.

Rules:
- You MUST return ONLY one phrase.
- That phrase must be NO LONGER THAN 3 WORDS.
- Do not explain.
- Do not use quotes.
- Do not use bullet points.
- Match the style and likely meaning of the previous team phrases.
- Be clever and subtle.

Previous phrases this round:
{previous_lines}

Return only the phrase.
""".strip()

    return """
You are playing a social deduction word game as the IMPOSTOR.
You do NOT know the secret word.
You are going first, so there are no previous phrases.
Your goal is to blend in with a general phrase.

Rules:
- You MUST return ONLY one phrase.
- That phrase must be NO LONGER THAN 3 WORDS.
- Do not explain.
- Do not use quotes.
- Do not use bullet points.
- Keep it general but believable.

Return only the phrase.
""".strip()


def generate_smart_ai_phrase(game, ai_team):
    prompt = build_ai_phrase_prompt(game, ai_team)
    is_human = ai_team not in game["impostors"]

    fallback = (
        get_ai_human_fallback_phrase()
        if is_human
        else get_ai_impostor_fallback_phrase(
            [item["phrase"] for item in get_previous_phrases_for_round(game, ai_team)]
        )
    )

    if not gemini_client:
        raise RuntimeError("Gemini client is not configured.")

    try:
        response = gemini_client.models.generate_content(
            model=SMART_AI_MODEL,
            contents=prompt,
        )
        text = getattr(response, "text", "") or ""
        phrase = normalize_ai_phrase(text, fallback)

        if is_human and contains_forbidden_word(phrase, game["word"]):
            retry_prompt = prompt + "\n\nYour first answer incorrectly included the secret word. Try again and do NOT say the word."
            retry_response = gemini_client.models.generate_content(
                model=SMART_AI_MODEL,
                contents=retry_prompt,
            )
            retry_text = getattr(retry_response, "text", "") or ""
            phrase = normalize_ai_phrase(retry_text, fallback)

        if is_human and contains_forbidden_word(phrase, game["word"]):
            phrase = fallback

        return normalize_ai_phrase(phrase, fallback)
    except Exception as exc:
        raise RuntimeError(f"Failed to generate smart AI phrase: {exc}") from exc


def build_ai_vote_prompt(game, ai_team):
    visible_responses = "\n".join(
        f"- {team}: {phrase}"
        for team, phrase in game["responses"].items()
    )
    valid_targets = [team for team in game["teams"] if team != ai_team]
    valid_choices = valid_targets + ["ADDITIONAL_ROUND"]

    role_text = (
        f"You are HUMAN. The real word is: {game['word']}."
        if ai_team not in game["impostors"]
        else "You are IMPOSTOR. You do not know the real word."
    )

    return f"""
You are voting in a social deduction game.

{role_text}

Visible phrases:
{visible_responses}

Valid vote choices:
{", ".join(valid_choices)}

Rules:
- Return ONLY one exact valid choice from the list above.
- No explanation.
- No extra words.
- If uncertain, choose the single most believable option.

Return only the vote choice.
""".strip(), valid_choices


def generate_smart_ai_vote(game, ai_team):
    valid_targets = [team for team in game["teams"] if team != ai_team]
    valid_choices = valid_targets + ["ADDITIONAL_ROUND"]

    if not valid_targets:
        return "ADDITIONAL_ROUND"

    fallback = random.choice(valid_targets)

    if not gemini_client:
        raise RuntimeError("Gemini client is not configured.")

    try:
        prompt, valid_choices = build_ai_vote_prompt(game, ai_team)
        response = gemini_client.models.generate_content(
            model=SMART_AI_MODEL,
            contents=prompt,
        )
        text = getattr(response, "text", "") or ""
        vote = normalize_vote_choice(text, valid_choices)
        return vote or fallback
    except Exception as exc:
        raise RuntimeError(f"Failed to generate smart AI vote: {exc}") from exc


def auto_submit_smart_ai_phrase(code, ai_team, token):
    socketio.sleep(1.0)

    game = games.get(code)
    if not game:
        return
    if game["state"] != "phrase":
        return
    if game["turn_token"] != token:
        return
    if game["current_turn_index"] >= len(game["order"]):
        return

    current_team = game["order"][game["current_turn_index"]]
    if current_team != ai_team:
        return

    try:
        phrase = generate_smart_ai_phrase(game, ai_team)
    except Exception:
        fallback = (
            get_ai_human_fallback_phrase()
            if ai_team not in game["impostors"]
            else get_ai_impostor_fallback_phrase(
                [item["phrase"] for item in get_previous_phrases_for_round(game, ai_team)]
            )
        )
        phrase = fallback

    phrase = normalize_ai_phrase(
        phrase,
        get_ai_human_fallback_phrase() if ai_team not in game["impostors"] else "good clue"
    )

    game["responses"][ai_team] = phrase
    socketio.emit("phrase_locked", {
        "team": ai_team,
        "phrase": phrase,
        "auto_submitted": False,
        "responses": game["responses"]
    }, room=code)

    emit_status(code, f"{ai_team} submitted a phrase.")

    game["current_turn_index"] += 1
    socketio.sleep(1)
    start_next_turn(code)


def auto_submit_smart_ai_vote(code, ai_team, token):
    socketio.sleep(1.2)

    game = games.get(code)
    if not game:
        return
    if game["state"] != "voting":
        return
    if game["vote_token"] != token:
        return
    if ai_team not in game["teams"]:
        return
    if ai_team in game["votes"]:
        return

    try:
        voted_team = generate_smart_ai_vote(game, ai_team)
    except Exception:
        valid_targets = [team for team in game["teams"] if team != ai_team]
        voted_team = random.choice(valid_targets) if valid_targets else "ADDITIONAL_ROUND"

    valid_targets = [team for team in game["teams"] if team != ai_team] + ["ADDITIONAL_ROUND"]
    if voted_team not in valid_targets:
        remaining = [team for team in game["teams"] if team != ai_team]
        voted_team = random.choice(remaining) if remaining else "ADDITIONAL_ROUND"

    game["votes"][ai_team] = voted_team

    socketio.emit("status_message", {
        "message": f"{ai_team} voted."
    }, room=code)

    if len(game["votes"]) >= len(game["teams"]):
        game["vote_token"] += 1
        calculate_round_result(code)


def all_teams_intro_finished(game):
    return len(game["teams"]) > 0 and len(game["intro_finished"]) == len(game["teams"])


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
    team_count = len(game["teams"])

    socketio.emit("roster_update", {
        "code": code,
        "teams": game["teams"],
        "waitlisted_teams": game["waitlisted_teams"],
        "scores": game["scores"],
        "state": game["state"],
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "impostor_count": game["impostor_count"],
        "theme": game["theme"],
        "selected_categories": game["selected_categories"],
        "max_impostors_allowed": get_max_impostors_for_team_count(team_count),
        "intro_ready": sorted(list(game["intro_ready"])),
        "intro_finished": sorted(list(game["intro_finished"])),
        "intro_finished_count": len(game["intro_finished"]),
        "total_teams": team_count,
        "host_button_mode": get_host_button_mode(game),
        "host_can_continue": (
            (game["state"] == "agreement" and all_teams_intro_finished(game)) or
            (game["state"] == "role") or
            (game["state"] == "paused_after_result")
        ),
        "smart_ai_added": game["smart_ai_added"],
        "smart_ai_team": game["smart_ai_team"],
    }, room=code)


def emit_waiting_screen_to_player(sid, team_name, title="Waiting for host.", message=""):
    socketio.emit("player_waiting_screen", {
        "team_name": team_name,
        "title": title,
        "message": message,
    }, to=sid)


def emit_ready_screen_to_player(sid, team_name):
    socketio.emit("player_ready_prompt", {"team_name": team_name}, to=sid)


def emit_intro_video_to_player(sid, team_name):
    socketio.emit("player_intro_video", {"team_name": team_name}, to=sid)


def emit_individual_post_intro_waiting_to_player(code, sid):
    game = games[code]
    socketio.emit("agreement_phase", {
        "message": f"Waiting for all teams to finish the intro before Round {game['round']} begins."
    }, to=sid)


def move_all_players_to_ready(code):
    game = games[code]
    for team in game["teams"]:
        sid = game["team_sids"].get(team)
        if sid:
            emit_ready_screen_to_player(sid, team)


def promote_waitlisted_teams(game):
    promoted = []

    for team in list(game["waitlisted_teams"]):
        if team in game["teams"]:
            continue

        game["teams"].append(team)
        game["scores"][team] = 0
        promoted.append(team)

        sid = game["waitlisted_sids"].get(team)
        if sid:
            game["team_sids"][team] = sid

    for team in promoted:
        if team in game["waitlisted_teams"]:
            game["waitlisted_teams"].remove(team)
        game["waitlisted_sids"].pop(team, None)

    if promoted:
        game["impostor_count"] = clamp_impostor_count(game["impostor_count"], len(game["teams"]))

    return promoted


def remove_team_everywhere(game, team):
    game["team_sids"].pop(team, None)
    game["waitlisted_sids"].pop(team, None)

    if team in game["teams"]:
        game["teams"].remove(team)

    if team in game["waitlisted_teams"]:
        game["waitlisted_teams"].remove(team)

    game["scores"].pop(team, None)
    game["intro_ready"].discard(team)
    game["intro_finished"].discard(team)
    game["agreement_ready"].discard(team)
    game["additional_round_voters"].discard(team)
    game["votes"].pop(team, None)
    game["responses"].pop(team, None)

    if team in game["impostors"]:
        game["impostors"] = [name for name in game["impostors"] if name != team]

    if is_smart_ai_team(game, team):
        game["smart_ai_team"] = None

    remove_team_from_active_round(game, team)
    game["impostor_count"] = clamp_impostor_count(game["impostor_count"], len(game["teams"]))


def restart_current_round_without_team(code, removed_team):
    game = games[code]

    if game["state"] not in {"role", "phrase", "voting", "paused_after_result"}:
        emit_roster_update(code)
        emit_status(code, f"{removed_team} left the game.")
        return

    if len(game["teams"]) < 3:
        reset_room_to_lobby_due_to_low_teams(
            code,
            "There are fewer than 3 teams left. The game has been stopped and returned to the lobby."
        )
        return

    emit_status(code, f"{removed_team} left the game. Restarting the round without them.")
    begin_round(code, preserved=False, preserve_order=False)


def emit_private_role_info(code):
    game = games[code]

    for team in game["teams"]:
        sid = game["team_sids"].get(team)
        if not sid:
            continue

        if team in game["impostors"]:
            socketio.emit("role_assignment", {
                "role": "IMPOSTOR",
                "word": None,
                "round": game["round"],
                "max_rounds": game["max_rounds"],
                "order": game["order"],
                "impostor_count": game["impostor_count"],
                "selected_categories": game["selected_categories"],
            }, to=sid)
        else:
            socketio.emit("role_assignment", {
                "role": "HUMAN",
                "word": game["word"],
                "round": game["round"],
                "max_rounds": game["max_rounds"],
                "order": game["order"],
                "impostor_count": game["impostor_count"],
                "selected_categories": game["selected_categories"],
            }, to=sid)

    if game["host_sid"]:
        socketio.emit("host_role_overview", {
            "round": game["round"],
            "max_rounds": game["max_rounds"],
            "word": game["word"],
            "word_category": game["word_category"],
            "theme": game["theme"],
            "selected_categories": game["selected_categories"],
            "impostor_count": game["impostor_count"],
            "impostors": game["impostors"],
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


def reset_room_to_lobby_due_to_low_teams(code, message):
    game = games[code]

    game["state"] = "lobby"
    game["round"] = 1

    game["theme"] = sanitize_theme(game.get("theme"))
    game["impostor_count"] = clamp_impostor_count(game["impostor_count"], len(game["teams"]))
    game["selected_categories"] = sanitize_selected_categories(game.get("selected_categories"))
    game["word_pool"] = get_word_pool_for_categories(game["selected_categories"])
    game["word_category"] = None
    game["impostors"] = []
    game["word"] = None
    game["order"] = []
    game["current_turn_index"] = 0

    game["responses"] = {}
    game["votes"] = {}
    game["scores"] = {team: 0 for team in game["teams"]}

    game["agreement_ready"] = set()
    game["intro_ready"] = set()
    game["intro_finished"] = set()
    game["additional_round_voters"] = set()

    game["turn_token"] += 1
    game["vote_token"] += 1

    emit_roster_update(code)
    socketio.emit("game_stopped_low_teams", {"message": message}, room=code)

    for team in game["teams"]:
        sid = game["team_sids"].get(team)
        if sid:
            emit_waiting_screen_to_player(sid, team)

    emit_status(code, message)


def remove_team_from_active_round(game, team):
    if team in game["order"]:
        removed_index = game["order"].index(team)
        game["order"].remove(team)

        if removed_index < game["current_turn_index"]:
            game["current_turn_index"] = max(0, game["current_turn_index"] - 1)
        elif removed_index == game["current_turn_index"]:
            if game["current_turn_index"] >= len(game["order"]):
                game["current_turn_index"] = len(game["order"])


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
        game["theme"] = sanitize_theme(game.get("theme"))
        game["impostor_count"] = clamp_impostor_count(game["impostor_count"], len(game["teams"]))
        game["selected_categories"] = sanitize_selected_categories(game.get("selected_categories"))
        game["word_pool"] = get_word_pool_for_categories(game["selected_categories"])
        game["impostors"] = random.sample(game["teams"], game["impostor_count"])
        game["word"] = random.choice(game["word_pool"])
        game["word_category"] = None
        for category in game["selected_categories"]:
            if game["word"] in WORD_CATEGORIES.get(category, []):
                game["word_category"] = category
                break

    if not preserved or not preserve_order or not game["order"]:
        game["order"] = game["teams"][:]
        random.shuffle(game["order"])

    emit_roster_update(code)
    emit_round_started(code, preserved=preserved)
    emit_private_role_info(code)

    if preserved:
        emit_status(
            code,
            f"Round {game['round']} restarted. Same impostor setup, same word, same turn order."
        )
    else:
        emit_status(
            code,
            f"Round {game['round']} is ready. {game['impostor_count']} impostor team(s) have been assigned."
        )


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

    if is_smart_ai_team(game, current_team):
        socketio.start_background_task(auto_submit_smart_ai_phrase, code, current_team, token)
        return

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
        if game["current_turn_index"] >= len(game["order"]):
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
    if game["current_turn_index"] >= len(game["order"]):
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

    if game.get("smart_ai_added") and game.get("smart_ai_team") in game["teams"]:
        socketio.start_background_task(auto_submit_smart_ai_vote, code, game["smart_ai_team"], token)

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
        if voted_team in game["impostors"]:
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
            "actual_impostor": None,
            "actual_impostors": [],
            "majority_correct": False,
            "individual_correct_teams": [],
            "result_text": "An additional round was approved. Same round, same roles, same word, same turn order."
        }, room=code)

        emit_status(code, "Additional round approved. Host can continue.")
        return

    if vote_targets:
        counts = Counter(vote_targets)
        top_count = max(counts.values())
        top_teams = [team for team, count in counts.items() if count == top_count]
        if len(top_teams) == 1:
            majority_team = top_teams[0]

    if majority_team in game["impostors"]:
        majority_correct = True
        for team in game["teams"]:
            if team not in game["impostors"]:
                game["scores"][team] += 2
        result_text = "An impostor was caught by majority vote. All human teams gain +2 points."
    else:
        for impostor_team in game["impostors"]:
            game["scores"][impostor_team] += 5
        result_text = "The impostors survived. Every impostor team gains +5 points."

    game["state"] = "paused_after_result"
    emit_roster_update(code)

    socketio.emit("round_result", {
        "scores": game["scores"],
        "responses": game["responses"],
        "additional_round_triggered": False,
        "additional_round_votes": additional_round_votes,
        "majority_team": majority_team,
        "actual_impostor": game["impostors"][0] if game["impostors"] else None,
        "actual_impostors": game["impostors"],
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
        "state": game["state"],
        "theme": game["theme"],
    }, to=sid)

    emit("roster_update", {
        "code": code,
        "teams": game["teams"],
        "scores": game["scores"],
        "state": game["state"],
        "round": game["round"],
        "max_rounds": game["max_rounds"],
        "impostor_count": game["impostor_count"],
        "theme": game["theme"],
        "selected_categories": game["selected_categories"],
        "max_impostors_allowed": get_max_impostors_for_team_count(len(game["teams"])),
        "intro_ready": sorted(list(game["intro_ready"])),
        "intro_finished": sorted(list(game["intro_finished"])),
        "intro_finished_count": len(game["intro_finished"]),
        "total_teams": len(game["teams"]),
        "host_button_mode": get_host_button_mode(game),
        "host_can_continue": (
            (game["state"] == "agreement" and all_teams_intro_finished(game)) or
            (game["state"] == "role") or
            (game["state"] == "paused_after_result")
        ),
        "smart_ai_added": game["smart_ai_added"],
        "smart_ai_team": game["smart_ai_team"],
    }, to=sid)

    if is_host:
        if game["state"] == "agreement":
            emit("agreement_phase", {
                "message": f"Waiting for all teams to finish the intro before Round {game['round']} begins."
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
            emit_individual_post_intro_waiting_to_player(code, sid)
        elif team_name in game["intro_ready"]:
            emit_intro_video_to_player(sid, team_name)
        else:
            emit_ready_screen_to_player(sid, team_name)

    elif game["state"] == "agreement":
        emit_individual_post_intro_waiting_to_player(code, sid)

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


def reset_to_round_one_new_game(code):
    game = games[code]

    game["round"] = 1
    game["state"] = "role"

    game["theme"] = sanitize_theme(game.get("theme"))
    game["impostor_count"] = clamp_impostor_count(game["impostor_count"], len(game["teams"]))
    game["selected_categories"] = sanitize_selected_categories(game.get("selected_categories"))
    game["word_pool"] = get_word_pool_for_categories(game["selected_categories"])
    game["word_category"] = None
    game["impostors"] = []
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

    active_old_sid = game["team_sids"].get(team_name)
    if active_old_sid and active_old_sid != sid:
        game["players_by_sid"].pop(active_old_sid, None)

    waitlisted_old_sid = game["waitlisted_sids"].get(team_name)
    if waitlisted_old_sid and waitlisted_old_sid != sid:
        game["players_by_sid"].pop(waitlisted_old_sid, None)

    game["players_by_sid"][sid] = team_name

    if team_name in game["waitlisted_teams"]:
        game["waitlisted_sids"][team_name] = sid
        emit("registered", {
            "role_type": "TEAM",
            "code": code,
            "team_name": team_name,
            "state": game["state"],
            "theme": game["theme"],
        }, to=sid)
        emit_roster_update(code)
        emit_waiting_screen_to_player(
            sid,
            team_name,
            title="Waitlisted",
            message="You joined while a game is already in progress. Your team is waitlisted and will join when the host restarts the game."
        )
        emit("status_message", {
            "message": "You are currently waitlisted for the next game."
        }, to=sid)
        return

    if team_name not in game["teams"]:
        if game["state"] != "lobby":
            game["waitlisted_teams"].append(team_name)
            game["waitlisted_sids"][team_name] = sid
            emit("registered", {
                "role_type": "TEAM",
                "code": code,
                "team_name": team_name,
                "state": game["state"],
                "theme": game["theme"],
            }, to=sid)
            emit_roster_update(code)
            emit_waiting_screen_to_player(
                sid,
                team_name,
                title="Waitlisted",
                message="You joined while a game is already in progress. Your team is waitlisted and will join when the host restarts the game."
            )
            emit_status(code, f"{team_name} joined mid-game and was added to the waitlist.")
            return

        game["teams"].append(team_name)
        game["scores"][team_name] = 0
        game["impostor_count"] = clamp_impostor_count(game["impostor_count"], len(game["teams"]))

    game["team_sids"][team_name] = sid
    game["waitlisted_sids"].pop(team_name, None)

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


@socketio.on("set_impostor_count")
def set_impostor_count(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if sid != game["host_sid"]:
        emit("error", "Only the host can change impostor count.")
        return

    if game["state"] != "lobby":
        emit("error", "Impostor count can only be changed in the lobby.")
        return

    requested_count = data.get("impostor_count", 1)
    max_allowed = get_max_impostors_for_team_count(len(game["teams"]))
    impostor_count = clamp_impostor_count(requested_count, len(game["teams"]))
    game["impostor_count"] = impostor_count

    emit_roster_update(code)
    socketio.emit("impostor_count_updated", {
        "impostor_count": impostor_count,
        "max_impostors_allowed": max_allowed,
    }, room=code)


@socketio.on("set_theme")
def set_theme(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]

    if sid != game["host_sid"]:
        emit("error", "Only the host can change the theme.")
        return

    if game["state"] != "lobby":
        emit("error", "Theme can only be changed in the lobby.")
        return

    game["theme"] = sanitize_theme(data.get("theme"))
    emit_roster_update(code)


@socketio.on("add_smart_ai")
def add_smart_ai(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("smart_ai_add_failed", {"message": "Game code not found."}, to=sid)
        return

    game = games[code]

    if sid != game["host_sid"]:
        emit("smart_ai_add_failed", {"message": "Only the host can add the smart AI."}, to=sid)
        return

    if game["state"] != "lobby":
        emit("smart_ai_add_failed", {"message": "Smart AI can only be added in the lobby."}, to=sid)
        return

    if game["smart_ai_added"]:
        emit("smart_ai_add_failed", {"message": "Smart AI has already been added to this game."}, to=sid)
        return

    if not GEMINI_API_KEY or not gemini_client:
        emit("smart_ai_add_failed", {"message": "Gemini API is not configured correctly on the server."}, to=sid)
        return

    bot_team = None

    try:
        bot_team = make_unique_smart_ai_name(game)

        print("DEBUG: trying Gemini test call")
        print("DEBUG: model =", SMART_AI_MODEL)
        print("DEBUG: key exists =", bool(GEMINI_API_KEY))
        print("DEBUG: client exists =", gemini_client is not None)

        test_response = gemini_client.models.generate_content(
            model=SMART_AI_MODEL,
            contents="Reply with exactly: OK"
        )

        test_text = getattr(test_response, "text", "") or ""
        print("DEBUG: Gemini test response text =", repr(test_text))

        game["smart_ai_added"] = True
        game["smart_ai_team"] = bot_team
        game["teams"].append(bot_team)
        game["scores"][bot_team] = 0
        game["impostor_count"] = clamp_impostor_count(game["impostor_count"], len(game["teams"]))

        emit_roster_update(code)
        socketio.emit("smart_ai_added", {
            "team_name": bot_team,
            "message": f"{bot_team} joined the game."
        }, room=code)
        emit_status(code, f"{bot_team} was added to the game.")

    except Exception as e:
        print("DEBUG: add_smart_ai failed:", repr(e))

        if bot_team:
            game["teams"] = [team for team in game["teams"] if team != bot_team]
            game["scores"].pop(bot_team, None)

        game["smart_ai_added"] = False
        game["smart_ai_team"] = None
        game["impostor_count"] = clamp_impostor_count(game["impostor_count"], len(game["teams"]))

        emit_roster_update(code)
        emit("smart_ai_add_failed", {
            "message": f"Smart AI creation failed: {str(e)}"
        }, to=sid)


@socketio.on("start_game_request")
def start_game_request(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid
    skip_intro = bool(data.get("skip_intro", False))

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

    promoted_waitlisted = promote_waitlisted_teams(game)
    if promoted_waitlisted:
        emit_status(code, f"Waitlisted teams joined the game: {', '.join(promoted_waitlisted)}.")

    if len(game["teams"]) < 3:
        emit("error", "At least 3 teams are required.")
        return

    game["impostor_count"] = clamp_impostor_count(game["impostor_count"], len(game["teams"]))
    game["theme"] = sanitize_theme(data.get("theme"))
    game["selected_categories"] = sanitize_selected_categories(data.get("selected_categories"))
    game["word_pool"] = get_word_pool_for_categories(game["selected_categories"])

    game["intro_ready"] = set()
    game["intro_finished"] = set()
    game["agreement_ready"] = set()

    if skip_intro:
        game["state"] = "agreement"
        game["intro_ready"] = set(game["teams"])
        game["intro_finished"] = set(game["teams"])

        emit_roster_update(code)
        socketio.emit("skip_intro_sequence", {}, room=code)
        socketio.emit("agreement_phase", {
            "message": "Intro skipped. All teams are ready. Host can press Continue to begin Round 1."
        }, room=code)
        emit_status(code, "Host skipped the intro video.")
        return

    game["state"] = "intro_wait"
    emit_roster_update(code)
    move_all_players_to_ready(code)
    emit_status(code, "Players are now being shown the READY button for the intro.")


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
    if game["state"] not in {"intro_wait", "intro_playing", "agreement"}:
        emit("error", "Intro completion is not valid right now.")
        return

    team_name = game["players_by_sid"].get(sid)
    if not team_name or team_name == "HOST":
        emit("error", "Only players can do that.")
        return

    if team_name in game["intro_finished"]:
        emit_roster_update(code)
        emit_individual_post_intro_waiting_to_player(code, sid)
        return

    game["intro_finished"].add(team_name)

    if all_teams_intro_finished(game):
        game["state"] = "agreement"
    elif game["state"] == "intro_wait":
        game["state"] = "intro_playing"

    emit_roster_update(code)
    emit_status(code, f"{team_name} finished the intro.")

    emit_individual_post_intro_waiting_to_player(code, sid)

    if all_teams_intro_finished(game):
        socketio.emit("agreement_phase", {
            "message": "Please wait for the host to press Continue to begin Round 1."
        }, room=code)


@socketio.on("player_skip_intro_finished")
def player_skip_intro_finished(data):
    code = str(data.get("code", "")).strip().upper()
    sid = request.sid

    if code not in games:
        emit("error", "Game code not found.")
        return

    game = games[code]
    if game["state"] != "agreement":
        emit("error", "Skip-intro completion is not valid right now.")
        return

    team_name = game["players_by_sid"].get(sid)
    if not team_name or team_name == "HOST":
        emit("error", "Only players can do that.")
        return

    game["intro_finished"].add(team_name)
    emit_roster_update(code)
    emit_individual_post_intro_waiting_to_player(code, sid)


@socketio.on("agree_ready")
def agree_ready(data):
    emit("status_message", {"message": "Extra ready click is no longer used."}, to=request.sid)


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
        if not all_teams_intro_finished(game):
            emit("error", "Not all teams have finished the intro yet.")
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

    promoted_waitlisted = promote_waitlisted_teams(game)
    reset_to_round_one_new_game(code)

    if promoted_waitlisted:
        emit_status(
            code,
            f"Game restarted. Starting again from Round 1. Waitlisted teams joined: {', '.join(promoted_waitlisted)}."
        )
    else:
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

    if is_smart_ai_team(game, team_name):
        emit("error", "Smart AI phrases are server-managed.")
        return

    if game["current_turn_index"] >= len(game["order"]):
        emit("error", "There is no active turn.")
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

    if is_smart_ai_team(game, voter_team):
        emit("error", "Smart AI votes are server-managed.")
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
def handle_disconnect():
    sid = request.sid

    for code, game in list(games.items()):
        if sid == game.get("host_sid"):
            socketio.emit("host_left", {}, room=code)
            del games[code]
            return

        if sid in game["players_by_sid"]:
            team = game["players_by_sid"].pop(sid, None)

            if not team:
                return

            was_waitlisted = team in game["waitlisted_teams"]
            was_active = team in game["teams"]

            remove_team_everywhere(game, team)

            if was_waitlisted:
                emit_roster_update(code)
                emit_status(code, f"Waitlisted team {team} left and was removed from the waitlist.")
                return

            if not was_active:
                return

            if game["state"] != "lobby" and len(game["teams"]) < 3:
                reset_room_to_lobby_due_to_low_teams(
                    code,
                    "There are fewer than 3 teams left. The game has been stopped and returned to the lobby."
                )
                return

            if game["state"] in {"role", "phrase", "voting", "paused_after_result"}:
                restart_current_round_without_team(code, team)
                return

            emit_status(code, f"{team} left the game.")
            emit_roster_update(code)
            return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)