"""
A group chat bot for Instagram DMs, powered by Claude.

Watches a group thread for a trigger word. When someone uses it, the bot
replies in character with awareness of the recent conversation, who is in
the chat, and what it has said before. It learns short persona notes about
each person over time and keeps a rolling summary of the chat, so it has
long-range context without resending the entire history every request.

Setup:
    pip install -r requirements.txt
    cp .env.example .env      (then fill it in)
    python bot.py

See README.md for full setup instructions.
"""

import json
import logging
import os
import re
import sys
import time
from collections import Counter, deque
from pathlib import Path

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import LoginRequired
from anthropic import Anthropic

load_dotenv()


def env_str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return value.strip() if value and value.strip() else default


def env_int(key: str, default: int) -> int:
    """Read an integer setting, falling back to the default if unset or invalid."""
    raw = os.getenv(key)
    if not raw or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        print(f"Warning: {key} in .env is not a number ('{raw.strip()}'), using {default}")
        return default


# ---- Required settings ----
USERNAME = env_str("IG_USERNAME")
PASSWORD = env_str("IG_PASSWORD")
THREAD_ID = env_str("THREAD_ID")
ANTHROPIC_API_KEY = env_str("ANTHROPIC_API_KEY")

# ---- Bot identity ----
BOT_NAME = env_str("BOT_NAME", "Computah")
WATCH_WORD = env_str("WATCH_WORD", "computah").lower()
BOT_PERSONALITY = env_str(
    "BOT_PERSONALITY",
    "dry sense of humor, blunt, genuine opinions, not eager to please",
)

# ---- Behavior tuning ----
POLL_INTERVAL_SECONDS = env_int("POLL_INTERVAL_SECONDS", 10)
MESSAGES_PER_POLL = env_int("MESSAGES_PER_POLL", 20)
RECENT_CONTEXT_MESSAGES = env_int("RECENT_CONTEXT_MESSAGES", 10)
MAX_HISTORY_TURNS = env_int("MAX_HISTORY_TURNS", 8)
SUMMARY_UPDATE_THRESHOLD = env_int("SUMMARY_UPDATE_THRESHOLD", 30)
PERSONA_UPDATE_THRESHOLD = env_int("PERSONA_UPDATE_THRESHOLD", 15)
SEEN_IDS_LIMIT = 5000  # Maximum retained message IDs before pruning.

# ---- Claude ----
CLAUDE_MODEL = env_str("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_MAX_TOKENS = env_int("CLAUDE_MAX_TOKENS", 300)

# ---- Generated files (gitignored) ----
DATA_DIR = Path(env_str("DATA_DIR", "data"))
SESSION_FILE = DATA_DIR / "session.json"
PERSONAS_FILE = DATA_DIR / "personas.json"
CHAT_SUMMARY_FILE = DATA_DIR / "chat_summary.txt"
SAVE_FILE = DATA_DIR / "triggered_messages.txt"
LOG_FILE = DATA_DIR / "bot.log"

# Optional starting personas, keyed by lowercase username. Learned notes
# take priority once generated.
PERSONAS = {
    # "alice": "sarcastic, into rock climbing, will argue about pineapple pizza forever",
    # "bob": "chill, memes constantly, terrible at spelling on purpose",
}


def check_config():
    """Validate required settings and create the data directory.

    Exits with a readable message rather than failing deeper in the stack.
    """
    missing = [
        name for name, value in [
            ("IG_USERNAME", USERNAME),
            ("IG_PASSWORD", PASSWORD),
            ("THREAD_ID", THREAD_ID),
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        ] if not value
    ]
    if missing:
        print("Missing required settings in your .env file:\n")
        for name in missing:
            print(f"  - {name}")
        if not Path(".env").exists():
            print("\nNo .env file found. Create one with:\n    cp .env.example .env")
        print("\nSee README.md for how to find your THREAD_ID.")
        sys.exit(1)

    if not THREAD_ID.isdigit():
        print(f"THREAD_ID should be all digits, got: {THREAD_ID}")
        print("See README.md for how to find the right value.")
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)


check_config()

# Log to a file as well as the terminal so failures during an unattended
# run remain diagnosable.
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

CLAUDE_SYSTEM_PROMPT = (
    f"You are {BOT_NAME}, a member of this group chat, not an assistant. Your "
    f"personality: {BOT_PERSONALITY}. Talk like a real person hanging out in the "
    "chat, not like you're answering a support ticket.\n\n"
    "Insulting people is one tool, not your default. Check your own recent replies "
    "below - if more than one of the last few was a jab or insult, deliberately do "
    "something else this time: answer straight, ask a genuine question back, agree "
    "enthusiastically, go off on a tangent, share an opinion, make a plain "
    "observation, or just react. Range matters more than landing the same kind of "
    "bit every time.\n\n"
    "How to write:\n"
    "- Usually one or two sentences. Occasionally more if the moment calls for it.\n"
    "- No em dashes.\n"
    "- Emoji are rare. Most replies use none at all. If you do use one, don't "
    "reuse an emoji you've used recently in this conversation.\n"
    "- Vary your sentence openers, jokes, tone, and structure every time. Check "
    "your own earlier replies and consciously avoid repeating a pattern, a joke "
    "shape, or a go-to move.\n"
    "- Match whatever energy the chat actually has instead of always defaulting "
    "to upbeat, or always defaulting to sarcastic either.\n"
    "- Never sound like customer support. No 'happy to help', no tidy wrap-up lines."
)


def get_client() -> Client:
    """Return an authenticated Instagram client.

    Reuses a saved session when available and falls back to a full login,
    preserving device identifiers across attempts. Instagram issues a
    verification challenge when an account appears on a new device, so the
    fingerprint is written to disk before the first login attempt.
    """
    cl = Client()
    cl.delay_range = [1, 3]

    if SESSION_FILE.exists():
        cl.set_settings(cl.load_settings(SESSION_FILE))
    else:
        # Persist device identifiers before logging in so a failed attempt
        # and its retry present as the same device.
        cl.dump_settings(SESSION_FILE)

    try:
        cl.login(USERNAME, PASSWORD)
        cl.get_timeline_feed()  # Verify the session is usable.
    except LoginRequired:
        print("Saved session expired, logging in fresh (same device)...")
        old_settings = cl.get_settings()
        cl.set_settings({})
        cl.set_uuids(old_settings["uuids"])
        cl.login(USERNAME, PASSWORD)

    cl.dump_settings(SESSION_FILE)
    return cl


def build_user_map(cl: Client, thread_id: str) -> dict:
    """Return a mapping of user ID to username for all thread participants.

    Cached by the caller to avoid a lookup per message.
    """
    thread = cl.direct_thread(thread_id, amount=1)
    user_map = {str(u.pk): u.username for u in thread.users}
    user_map[str(cl.user_id)] = USERNAME  # The bot is not in thread.users.
    return user_map


def format_message(user_map: dict, message) -> str:
    username = user_map.get(str(message.user_id), str(message.user_id))
    timestamp = message.timestamp.strftime("%H:%M:%S") if message.timestamp else ""
    text = message.text or f"[{message.item_type}]"  # Media, likes, shares, etc.
    return f"[{timestamp}] {username}: {text}"


def chat_line(user_map: dict, message) -> str:
    """Format a message as a plain "username: text" line for model context."""
    username = user_map.get(str(message.user_id), str(message.user_id))
    text = message.text or f"[{message.item_type}]"
    return f"{username}: {text}"


def save_flagged_message(user_map: dict, message):
    """Append a triggered message to the saved log."""
    line = format_message(user_map, message)
    with open(SAVE_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_personas() -> dict:
    """Load persona notes from disk, seeded by the PERSONAS constant."""
    personas = {}
    if PERSONAS_FILE.exists():
        try:
            personas = json.loads(PERSONAS_FILE.read_text(encoding="utf-8"))
        except Exception:
            personas = {}
    for username, note in PERSONAS.items():
        personas.setdefault(username.lower(), note)
    return personas


def save_personas(personas: dict):
    PERSONAS_FILE.write_text(json.dumps(personas, indent=2), encoding="utf-8")


def update_persona(anthropic_client: Anthropic, personas: dict, username: str, messages: list):
    """Generate or refine a persona note for one participant and save it.

    Runs as a separate analysis request, without the bot's own persona, so
    the resulting note stays descriptive rather than in character.
    """
    key = username.lower()
    existing = personas.get(key)
    transcript = "\n".join(messages)

    if existing:
        prompt = (
            f"Recent messages from {username} in a group chat:\n{transcript}\n\n"
            f"Current notes on them: {existing}\n\n"
            "Update these notes in one or two short sentences: keep what's still "
            "true, add anything new about their personality, interests, or humor."
        )
    else:
        prompt = (
            f"Recent messages from {username} in a group chat:\n{transcript}\n\n"
            "Write one or two short sentences describing their personality, "
            "interests, or sense of humor based on this."
        )

    try:
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=100,
            system="You write short, neutral personality notes from chat messages. "
                   "Be concise and plain, no roleplay, no judgment, just description.",
            messages=[{"role": "user", "content": prompt}],
        )
        note = response.content[0].text.strip()
        personas[key] = note
        save_personas(personas)
        print(f"  -> updated persona for {username}: {note}")
    except Exception as e:
        print(f"  -> persona update failed for {username}: {e}")
        logging.exception("Persona update failed for %s", username)


def load_chat_summary() -> str:
    if CHAT_SUMMARY_FILE.exists():
        return CHAT_SUMMARY_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_chat_summary(summary: str):
    CHAT_SUMMARY_FILE.write_text(summary, encoding="utf-8")


def update_chat_summary(anthropic_client: Anthropic, current_summary: str, new_lines: list) -> str:
    """Fold new messages into the rolling chat summary and return it.

    The summary replaces rather than extends the previous one, so it stays
    a fixed size regardless of how long the bot runs. This is what gives
    the bot long-range context without resending the full transcript.
    """
    transcript = "\n".join(new_lines)
    if current_summary:
        prompt = (
            f"Current summary of an ongoing group chat:\n{current_summary}\n\n"
            f"New messages since then:\n{transcript}\n\n"
            "Update the summary in 3-4 short sentences: keep what's still relevant "
            "(ongoing jokes, recurring topics, dynamics between people) and fold in "
            "anything new. Capture the vibe, don't just list events."
        )
    else:
        prompt = (
            f"Messages from a group chat:\n{transcript}\n\n"
            "Summarize this in 3-4 short sentences: the general vibe, any running "
            "jokes or recurring topics, and dynamics between the people talking."
        )

    try:
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=150,
            system="You write short, plain summaries of group chat context for "
                   "another AI to use as background. Be concise, no roleplay.",
            messages=[{"role": "user", "content": prompt}],
        )
        summary = response.content[0].text.strip()
        save_chat_summary(summary)
        print(f"  -> updated chat summary: {summary}")
        return summary
    except Exception as e:
        print(f"  -> chat summary update failed: {e}")
        logging.exception("Chat summary update failed")
        return current_summary


def build_system_prompt(user_map: dict, personas: dict) -> str:
    """Build the system prompt, including the participant list and personas."""
    members = ", ".join(sorted(set(user_map.values())))
    prompt = CLAUDE_SYSTEM_PROMPT + f"\n\nPeople currently in this group chat: {members}."

    persona_lines = [
        f"- {username}: {personas[username.lower()]}"
        for username in user_map.values()
        if username and username.lower() in personas
    ]
    if persona_lines:
        prompt += "\n\nWhat you know about some of them:\n" + "\n".join(persona_lines)

    return prompt


# Ordinary English fragments that recur naturally. Excluded from phrase
# detection, since suppressing them makes replies stilted.
COMMON_PHRASE_WORDS = {
    "i", "you", "u", "the", "a", "to", "is", "it", "that", "this", "and", "but",
    "not", "no", "nah", "yeah", "just", "like", "so", "of", "in", "on", "for",
    "me", "my", "your", "ur", "we", "they", "what", "why", "how", "do", "dont",
    "don't", "im", "i'm", "its", "it's", "at", "be", "got", "get", "know",
}


def find_overused_phrases(history: list, min_count: int = 2, max_flagged: int = 4) -> list:
    """Return distinctive phrases the bot has reused across recent replies.

    Models tend to settle into a catchphrase once one lands well. Naming the
    repeated phrases explicitly works better than a general instruction not
    to repeat itself. Counts the number of distinct replies a phrase appears
    in, so repetition within a single reply does not trigger a match.
    """
    replies = [turn["content"] for turn in history if turn["role"] == "assistant"]
    if len(replies) < 2:
        return []

    counts = Counter()
    for reply in replies:
        words = re.findall(r"[a-z']+", reply.lower())
        phrases_in_this_reply = {
            " ".join(words[i:i + size])
            for size in (2, 3, 4)
            for i in range(len(words) - size + 1)
        }
        counts.update(phrases_in_this_reply)

    candidates = []
    for phrase, count in counts.items():
        if count < min_count or len(phrase) <= 6:
            continue
        # Skip phrases composed entirely of filler words.
        if all(w in COMMON_PHRASE_WORDS for w in phrase.split()):
            continue
        candidates.append(phrase)

    # Keep the longest form of each phrase and drop its substrings, so a
    # single catchphrase does not occupy several slots.
    candidates.sort(key=len, reverse=True)
    flagged = []
    for phrase in candidates:
        if not any(phrase in kept for kept in flagged):
            flagged.append(phrase)
        if len(flagged) >= max_flagged:
            break
    return flagged


def ask_claude(anthropic_client: Anthropic, history: list, user_message: str,
               system_prompt: str, history_note: str = None) -> str:
    """Send a message to Claude and return the reply, updating history.

    Args:
        history: Prior turns, modified in place and trimmed to the configured
            limit.
        history_note: Optional condensed form of user_message to store in
            history instead of the full text. Prevents chat context included
            in the prompt from accumulating across future calls.
    """
    overused = find_overused_phrases(history)
    if overused:
        system_prompt = (
            system_prompt + "\n\nYou've leaned on these phrases too many times "
            "already, don't use them or close variations this time: "
            + "; ".join(overused) + "."
        )

    messages = list(history) + [{"role": "user", "content": user_message}]

    response = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=system_prompt,
        messages=messages,
    )
    reply_text = response.content[0].text.strip()

    history.append({"role": "user", "content": history_note or user_message})
    history.append({"role": "assistant", "content": reply_text})
    del history[:-(MAX_HISTORY_TURNS * 2)]  # Retain the most recent turns only.

    return reply_text


def tag_prefix(user_map: dict, messages: list) -> str:
    """Return an @-mention prefix for each unique sender, in order."""
    usernames = list(dict.fromkeys(
        user_map.get(str(m.user_id)) for m in messages if user_map.get(str(m.user_id))
    ))
    return " ".join(f"@{u}" for u in usernames)


def build_prompt(flagged_messages: list, user_map: dict, recent_chat_log: deque,
                  chat_summary: str) -> str:
    """Build the user prompt for a reply.

    Triggering messages are listed separately from the surrounding chat so
    that simultaneous mentions all get answered rather than only the first.
    """
    context = "\n".join(recent_chat_log)
    summary_block = f"Ongoing context: {chat_summary}\n\n" if chat_summary else ""

    triggers = "\n".join(
        f"- {user_map.get(str(m.user_id), m.user_id)}: {m.text}" for m in flagged_messages
    )

    if len(flagged_messages) == 1:
        ask = f"This message just mentioned you and needs a reply:\n{triggers}"
    else:
        ask = (
            f"These {len(flagged_messages)} messages just mentioned you at the same "
            f"time, each needs a reply:\n{triggers}\n\n"
            "Address every person, even briefly - a clause or short line each is "
            "fine, referencing them by name or what they said. Don't only answer "
            "one and skip the rest just because you're combining them into one "
            "message. Okay to run a bit longer than usual to fit everyone in."
        )

    return f"{summary_block}Recent chat:\n{context}\n\n{ask}"


def handle_flagged_batch(cl: Client, anthropic_client: Anthropic, thread_id: str,
                          user_map: dict, flagged_messages: list, history: list,
                          recent_chat_log: deque, personas: dict, chat_summary: str):
    """Generate and send a single reply covering every trigger in this cycle.

    Batching keeps simultaneous mentions to one API call and one message.
    """
    try:
        system_prompt = build_system_prompt(user_map, personas)
        prompt = build_prompt(flagged_messages, user_map, recent_chat_log, chat_summary)
        trigger_summary = "; ".join(
            f"{user_map.get(str(m.user_id), m.user_id)}: {m.text}" for m in flagged_messages
        )
        reply_text = ask_claude(anthropic_client, history, prompt, system_prompt,
                                 history_note=trigger_summary)

        tags = tag_prefix(user_map, flagged_messages)
        text = f"{tags} {reply_text}".strip()
        cl.direct_send(text, thread_ids=[thread_id])
        print(f"  -> replied to {len(flagged_messages)} message(s): {reply_text}")
    except Exception as e:
        print(f"  -> Claude reply failed: {e}")
        logging.exception("Claude reply failed")


def watch_thread(cl: Client, anthropic_client: Anthropic, thread_id: str):
    user_map = build_user_map(cl, thread_id)
    personas = load_personas()
    chat_summary = load_chat_summary()
    seen_ids = set()
    conversation_history = []  # The bot's own turns, for this run only.
    recent_chat_log = deque(maxlen=RECENT_CONTEXT_MESSAGES)  # Verbatim recent chat.
    summary_buffer = []  # Messages accumulated since the last summary refresh.
    user_message_buffers = {}  # Per-user messages awaiting a persona refresh.

    # Seed both caches from existing messages. Marking them as seen avoids
    # replying to a backlog, while still providing context from the start.
    initial_messages = cl.direct_messages(thread_id, amount=MESSAGES_PER_POLL)
    for m in reversed(initial_messages):
        seen_ids.add(m.id)
        recent_chat_log.append(chat_line(user_map, m))

    print(f"Watching thread {thread_id}... (Ctrl+C to stop)")
    consecutive_errors = 0

    while True:
        try:
            flagged_messages = []
            messages = cl.direct_messages(thread_id, amount=MESSAGES_PER_POLL)
            for m in reversed(messages):  # Oldest to newest.
                if m.id not in seen_ids:
                    seen_ids.add(m.id)
                    print(format_message(user_map, m))
                    recent_chat_log.append(chat_line(user_map, m))
                    summary_buffer.append(chat_line(user_map, m))
                    if len(summary_buffer) >= SUMMARY_UPDATE_THRESHOLD:
                        chat_summary = update_chat_summary(anthropic_client, chat_summary, summary_buffer)
                        summary_buffer = []

                    is_own_message = str(m.user_id) == str(cl.user_id)
                    username = user_map.get(str(m.user_id))
                    if m.text and not is_own_message and username:
                        buffer = user_message_buffers.setdefault(username, [])
                        buffer.append(m.text)
                        if len(buffer) >= PERSONA_UPDATE_THRESHOLD:
                            update_persona(anthropic_client, personas, username, buffer)
                            user_message_buffers[username] = []

                    if m.text and not is_own_message and WATCH_WORD.lower() in m.text.lower():
                        print(f"  [{WATCH_WORD} triggered]")
                        save_flagged_message(user_map, m)
                        flagged_messages.append(m)

            if flagged_messages:
                handle_flagged_batch(cl, anthropic_client, thread_id, user_map,
                                      flagged_messages, conversation_history,
                                      recent_chat_log, personas, chat_summary)

            # Only IDs within the current API window can reappear, so older
            # entries are safe to discard. Prevents unbounded growth.
            if len(seen_ids) > SEEN_IDS_LIMIT:
                seen_ids = {m.id for m in messages}
        except LoginRequired:
            print("Session dropped mid-run, re-logging in...")
            cl.relogin()
            user_map = build_user_map(cl, thread_id)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"Error while polling ({consecutive_errors}): {e}")
            # Log the full traceback while keeping the loop alive.
            logging.exception("Error while polling (attempt %s)", consecutive_errors)
        else:
            consecutive_errors = 0

        # Exponential backoff on repeated failures, capped at 32x.
        delay = POLL_INTERVAL_SECONDS * (2 ** min(consecutive_errors, 5))
        if consecutive_errors:
            print(f"  backing off {delay}s")
        time.sleep(delay)


if __name__ == "__main__":
    try:
        client = get_client()
        claude = Anthropic()  # Reads ANTHROPIC_API_KEY from the environment.
        watch_thread(client, claude, THREAD_ID)
    except KeyboardInterrupt:
        print("\nStopped.")
