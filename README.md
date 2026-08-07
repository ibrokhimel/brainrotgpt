# BrainrotGPT 🗿📈

A Telegram bot that is **Jayden**, a chronically-online 14-year-old. You don't
pick a persona or paste a convo for it to rewrite — you just text him, and he
texts back like an actual kid: in bursts, in lowercase, sometimes just an
emoji reaction, sometimes not for hours. Powered by Groq.

## How it works
1. You text him like you'd text a friend, in a private chat, a group
   (`@mention` or reply to him), or by forwarding a screenshot.
2. He replies as **several separate Telegram messages**, not one paragraph —
   with typing indicators and human-ish pacing between them, occasional
   typos with a `*correction` right after, and lowercase throughout.
3. A low-content message ("lol", "ok", "lmao") sometimes only gets an emoji
   **reaction** — no reply at all.
4. If you go quiet, he may **ping you later** ("yo", "??", "u still there") —
   escalating over hours and then days — then gives up. Come back after he's
   given up and the next reply reads a little wounded about it.
5. On a good day, with enough rapport, he'll **text you first**, unprompted —
   a cold open, about a meme he's into, his day, or something you told him.
6. He's **one person** across every chat, not a bot-per-user: a mood that
   drifts every 6–24h, a daily "what's going on with me" that's the same
   story for everyone that day, and a per-chat **bond** score plus **notes**
   that make him actually remember you specifically.
7. He can look at photos you send (read via a Groq vision model) and, if
   you've set up a sticker pack, send stickers back inside a burst.
8. He sleeps 1am–9am server time — no replies or pings go out until he wakes.

## Features
- 🧠 **One identity** — Jayden, 14 — not a style picker; `brainrot.PERSONAS`
  is reused as a **mood wheel** (headspace, not a cast)
- 💬 **Bursty replies** — split across multiple messages, paced with typing
  indicators, jittered typos + corrections
- 🩹 **Bond** — a per-chat relationship score that shapes tone
  (stranger/friend/annoyed) and how fast he answers
- 👻 **Ghost ladder** — chases you across hours then days if you go quiet,
  then gives up; forgiving-but-salty if you come back
- 🔔 **Cold opens** — sometimes texts first, gated on bond + inactivity
- 😴 **Sleep window** — no outbound activity 1am–9am server time
- 🙂 **Reactions** — low-content messages sometimes just get an emoji, no reply
- 🖼 **Stickers** — from an owner-configured pack, re-read daily
- 📅 **Shared daily life** — one LLM call a day, same story told to every chat
- 📝 **Memory** — distilled notes about each person, refreshed every ~15 messages
- 🔥 **Live slang/memes** — auto-refreshed daily (Reddit + Know Your Meme),
  owner-curated with `/trend`
- 👥 **Groups** — replies only on `@mention` or reply-to-him, never proactively
- 🔌 **Inline mode** (`@yourbot <text>`) — a separate, simpler generator
  (`brainrot.py`), unrelated to the kid's memory/bond/mood
- 💰 **Outbound budget** — caps LLM calls nobody asked for (pings, cold
  opens, notes, daily life); real replies are never budgeted
- 🗄️ **SQLite-driven scheduling** — `chat_state.next_action_at` is the
  source of truth, so a restart never drops a pending ping
- 🛡️ Rate limits, content-safety screen, prompt-injection framing, health
  endpoint, webhook mode

## Commands
| Command | What it does |
|---|---|
| `/start` | wake the bot up / welcome message |
| `/help` | how this thing works |
| `/settings` | mood (reroll) · chattiness (chill/normal/clingy) · mute — with buttons |
| `/shutup` | mute the kid in this chat |
| `/yo` | unmute and reset the ghost ladder |
| `/trend` | owner-only: `list \| add <t> \| ban <t> \| remove <t> \| refresh` live slang/memes |
| `/stats` | owner-only: usage stats |

Inline mode (`@yourbot <text>` in any chat) also still works — it's a
separate one-off generator, not the kid, and doesn't share his memory or bond.

## Setup (Windows)
1. **Bot token** — message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. **Groq key** — https://console.groq.com → API Keys.
3. **`.env`** — copy `.env.example` to `.env` and fill in both values (see all options there).
4. **Install + run:**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe bot.py
   ```
   Leave the window open — the bot is live while it runs.

> For inline mode, enable it in @BotFather (`/setinline`). For group
> `@mention`/reply-to to work, turn **Group Privacy** off in @BotFather
> (`/setprivacy` → Disable) so the bot can see messages that don't mention it.

## Setting up stickers
1. Create a sticker pack with [@Stickers](https://t.me/Stickers) on Telegram
   (or use one you already own).
2. Open the pack and copy its share link — it looks like
   `t.me/addstickers/<name>` — and take just the `<name>` part.
3. Put that in `STICKER_PACK_NAME` in `.env`.
4. Each sticker already carries its own emoji in Telegram (that's how the
   model picks one to send) — no manual tagging needed.
5. The pack is **re-read once a day**, so adding new stickers to it in
   Telegram makes them available to the kid without a redeploy.

## Run with Docker
```bash
cp .env.example .env   # fill it in
docker compose up -d --build
```
The SQLite DB persists in `./data`. Set `HEALTH_PORT` and expose it for uptime checks.

## Webhook vs polling
Default is long polling (no server needed). To use webhooks on a hosted box,
set `WEBHOOK_URL` (and `WEBHOOK_PORT`/`WEBHOOK_SECRET`) in `.env`.

## Configuration
All knobs live in `.env` (documented in `.env.example`): models + fallback +
vision model, the kid's proactive behaviour (ghost ladder, cold opens,
outbound budget), stickers, school hours / daily-life refresh, live trends,
rate limits, access control (`OWNER_IDS`, `PRIVATE_MODE`), persistence,
health/webhook/logging.

## Development
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
```
CI runs ruff + pytest on push (`.github/workflows/ci.yml`).

## Architecture
| File | Role |
|---|---|
| `bot.py` | Telegram entry point: message intake (text/photo/group), startup/shutdown lifecycle, single-instance lock, webhook/polling |
| `commands.py` | slash commands, the `/settings` keyboard, and its button handler — split out of `bot.py` to keep it under the line cap |
| `chat_engine.py` | the kid's identity, system prompt, and generation (`reply`/`ping`/`cold_open`) |
| `scheduler.py` | the kid's own clock: the 60s tick, burst delivery, and daily jobs (life refresh, sticker reload, trend refresh) — SQLite is the source of truth, not the in-memory JobQueue |
| `ghost.py` | pure timing logic: sleep window, reply latency, the ping ladder, cold-open eligibility — no Telegram or DB calls, so it's trivially testable |
| `burst.py` | splits a model response into separately-sent messages; paced sending with typing indicators and typos |
| `memory.py` | the recent conversation window plus distilled per-chat notes |
| `life.py` | the kid's shared daily life — one LLM call a day, same story for every chat |
| `stickers.py` | loads the owner's sticker pack, picks by emoji, avoids recent repeats |
| `trends.py` | live slang/meme refresh from Reddit + Know Your Meme |
| `budget.py` | the daily cap on LLM calls the user didn't ask for |
| `guard.py` | access control, content screen, injection framing, token trim |
| `db.py` | SQLite: chat state, kid state, message history, live trends, generation stats |
| `brainrot.py` | the old generator — now only powers inline mode |
| `vision.py` | screenshot/photo → transcript (Groq vision) |
| `rate_limit.py` | per-user cooldown + per-user/global caps (seeded from DB) |
| `health.py` | stdlib `/healthz` endpoint |
| `config.py` | env/config loading |

## Notes
- **Public bot:** anyone who finds it can talk to Jayden; rate limits protect
  your Groq quota. Lock it to yourself with `OWNER_IDS=<your id>` +
  `PRIVATE_MODE=true`.
- **State:** everything durable — chat state, bond, mood, notes, message
  history, scheduling — lives in SQLite (`chat_state.next_action_at` is the
  single source of truth for what fires next, so a restart never drops a
  pending reply or ghost ping).
- **Secrets:** `.env` is gitignored — never commit it.
- The kid's identity and prompt live in `chat_engine.py` (`IDENTITY` +
  `HOW_YOU_TEXT` + `build_system_prompt`). See `WISHLIST.md` for what's
  deliberately out of scope for now.
