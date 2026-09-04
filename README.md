# Group Chat Bot

An Instagram group chat bot powered by Claude. Say its name, it replies in
character.

Unlike a plain "message in, message out" wrapper, it keeps track of who is in
the chat, learns short persona notes about each person over time, maintains a
rolling summary of the conversation, and actively avoids repeating its own
catchphrases.

```
[18:02] alex: computah how many people are in here
  [computah triggered]
  -> replied to 1 message(s): four of you, and three are currently trying to break me
[18:02] computah_bot: @alex four of you, and three are currently trying to break me
```

## Warning: read this first

- This uses **instagrapi**, an unofficial reverse-engineered Instagram API.
  It is not endorsed by Instagram and automating an account can get it
  restricted or banned. **Use a throwaway account, not your main one.**
- Only run this in group chats where **everyone knows a bot is present**.
  The bot reads all messages in the thread and sends personality profiles of
  participants to an external API. Get consent from your group first.
- Costs real money. Every trigger is an Anthropic API call, plus occasional
  background calls for personas and summaries. Haiku is cheap but not free.

## Setup

**1. Requirements:** Python 3.9+, an Instagram account, and an
[Anthropic API key](https://console.anthropic.com/settings/keys).

**2. Install:**

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
pip install -r requirements.txt
```

**3. Configure:**

```bash
cp .env.example .env
```

Open `.env` and fill in `IG_USERNAME`, `IG_PASSWORD`, `ANTHROPIC_API_KEY`,
and `THREAD_ID`. Everything else has sensible defaults.

**4. Run:**

```bash
python bot.py
```

The bot only reacts to messages sent *after* it starts, so it will not reply
to a backlog on first launch.

## Finding your THREAD_ID

Every group chat has a numeric ID. To find yours, run this once with your
credentials in `.env`:

```python
from dotenv import load_dotenv
from instagrapi import Client
import os

load_dotenv()
cl = Client()
cl.login(os.getenv("IG_USERNAME"), os.getenv("IG_PASSWORD"))

for t in cl.direct_threads(amount=20):
    if t.is_group:
        print(f"{t.id}  {t.thread_title or [u.username for u in t.users]}")
```

Copy the ID for the chat you want into `.env`. It does not change over the
life of the group, so you only need to do this once.

## Configuration

All settings live in `.env`. The interesting ones:

| Setting | Default | What it does |
|---|---|---|
| `BOT_NAME` | `Computah` | What the bot calls itself |
| `WATCH_WORD` | `computah` | The word that summons it |
| `BOT_PERSONALITY` | dry, blunt... | One line describing its character |
| `POLL_INTERVAL_SECONDS` | `10` | Seconds between checks |
| `RECENT_CONTEXT_MESSAGES` | `10` | Messages seen word-for-word |
| `MAX_HISTORY_TURNS` | `8` | Own past replies remembered |
| `CLAUDE_MODEL` | Haiku | Swap for a bigger model if you want |

To completely reskin the bot, change `BOT_NAME`, `WATCH_WORD`, and
`BOT_PERSONALITY`. No code changes needed.

## How it works

**Polling.** Checks the thread every few seconds for new messages. Not
real-time push, but simple and reliable. Instagram's private API does expose
an MQTT realtime channel if you want to go further.

**Context, cheaply.** Sending the whole chat history on every reply gets
expensive fast. Instead the bot sends the last ~10 messages verbatim plus a
3-4 sentence rolling summary that gets regenerated every 30 messages. A few
sentences covering hours of chat costs far less than the raw transcript.

**Learned personas.** After someone sends 15 messages, a background call
writes a short neutral note about them, saved to `data/personas.json` and
refined over time. That is why the bot eventually knows who is who.

**Anti-repetition.** LLMs latch onto a phrase that landed once and reuse it
until it is stale. Before each reply, the bot scans its own recent replies
for 2-4 word phrases appearing in multiple messages, filters out ordinary
English, and explicitly instructs itself not to reuse the survivors.

**Batching.** If several people trigger it in the same poll cycle, it makes
one API call and sends one reply addressing everyone, rather than firing off
a separate message per person.

**Session reuse.** Instagram challenges accounts that appear to log in from
a new device every time. The bot saves its device fingerprint before its
first login attempt and reuses it, which avoids most checkpoint prompts.

## Troubleshooting

**`ChallengeRequired`.** Instagram wants manual verification. Log into the
account in the official app or on instagram.com, complete the prompt, wait
15-30 minutes, then retry. This cannot be resolved from code.

**`Your version of Instagram is out of date`.** instagrapi is stale.
Run `pip install --upgrade instagrapi`.

**Bot replies to the wrong person, or ignores someone.** Check that
`WATCH_WORD` is not a common substring of ordinary words in your chat.

**Bot seems stuck or silent.** Check `data/bot.log`. Every error is logged
there with a full traceback, even ones that only printed one line to the
terminal.

**Repeated errors.** The bot backs off exponentially rather than hammering
a rate-limited API, so recovery may take a minute. This is intentional.

## Data and privacy

Everything written to `data/` is gitignored. That directory contains your
Instagram session token, learned notes about real people, and saved chat
messages. Do not commit it, and be aware that message content is sent to
Anthropic's API for processing.

To reset the bot's memory, delete `data/personas.json` and
`data/chat_summary.txt`. To force a fresh login, delete `data/session.json`.

## License

MIT
