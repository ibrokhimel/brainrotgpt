# BrainrotGPT 🗿📈

A Telegram bot: forward it a conversation (or a **screenshot**), pick a style,
and it cooks you a single unhinged **brainrot reply** you can paste straight
back. Powered by Groq.

## How it works
1. Forward/paste one or more messages — or send a **screenshot** 📸 (read via a
   Groq vision model).
2. It buffers them and, once you stop sending, shows a preview with buttons.
3. Tap **✅ Generate** → one giant emoji-stuffed paragraph in the chosen style.
4. **🔄 Regenerate** rerolls (and actively diverges from the last one),
   **⭐ Save** keeps a banger, **🖼 Share** renders a card, **◀ ▶** flips through
   best-of-N candidates.
5. Send a **new** message after a reply and it auto-starts a fresh convo (no
   leftover context) — the old one is stashed behind a **🔗 Merge with previous**
   button in case you wanted to combine them.

## Why replies don't feel samey
Each generation rotates a **persona** (12 registers — gym sigma, doomer prophet,
courtroom, nature doc, …), samples a **random subset** of brainrot vocab, varies
a structural **opener**, and jitters temperature/seed. Regenerate avoids the
previous persona and reply. Tune it all per-chat with `/settings`.

## Features
- 🎭 **12 personas** + `/settings` to pin one or keep it `🎲 Random`
- 🎚 **Intensity** (mild/medium/unhinged) and 🎯 **tone** (roast/cope/hype/deny/gaslight)
- 🌐 **Language** matching (auto or pick one)
- 🎲 **Best-of-N** candidates you swipe through with ◀ ▶
- 📸 **Screenshot intake** (vision OCR → transcript)
- ⭐ **Favorites** (`/saved`) and 🖼 **share cards** (watermarked PNG)
- 📊 **Leaderboard** (`/leaderboard`) and owner **analytics** (`/stats`)
- 🌅 **Daily brainrot** (`/daily`)
- 👥 **Group mode** (`/brainrot` reply) and **inline mode** (`@yourbot <text>`)
- 🛡️ Rate limits, content-safety screen, prompt-injection framing, token budget
- ♻️ Retry + fallback model, SQLite persistence, health endpoint, webhook mode

## Commands
| Command | What it does |
|---|---|
| `/start`, `/help` | welcome + help |
| `/done` | generate now (skip the debounce) |
| `/clear` | wipe the current buffer |
| `/settings` | style · intensity · tone · language · best-of-N |
| `/persona` | quick style picker |
| `/last` | resend the last reply |
| `/saved` | your saved favorites (with 🗑 delete) |
| `/leaderboard` | top styles this week |
| `/daily` | toggle a daily brainrot post |
| `/stats` | analytics (owner-only) |
| `/brainrot` | group mode: reply to a message with it |

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

> For inline mode, enable it in @BotFather (`/setinline`). For `/brainrot` to see
> replied-to messages in groups, turn **Group Privacy** off in @BotFather.

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
vision model, generation defaults, rate limits, access control (`OWNER_IDS`,
`PRIVATE_MODE`), persistence, daily hour, health/webhook/logging.

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
| `bot.py` | Telegram handlers, menus, buttons, jobs, entry point |
| `brainrot.py` | prompt assembly, personas/vocab/openers, generate / best-of-N / stream |
| `vision.py` | screenshot → transcript (Groq vision) |
| `guard.py` | access control, content screen, injection framing, token trim |
| `db.py` | SQLite: settings, favorites, analytics, subscriptions |
| `share_card.py` | watermarked PNG share cards (Pillow) |
| `rate_limit.py` | per-user cooldown + per-user/global caps (seeded from DB) |
| `health.py` | stdlib `/healthz` endpoint |
| `config.py` | env/config loading |

## Notes
- **Public bot:** anyone who finds it can use it; rate limits protect your Groq
  quota. Lock it to yourself with `OWNER_IDS=<your id>` + `PRIVATE_MODE=true`.
- **State:** durable bits (settings/favorites/analytics) live in SQLite; the
  in-progress message buffer is in memory by design and cleared after 30 min idle.
- **Secrets:** `.env` is gitignored — never commit it.
- The brainrot prompt lives in `brainrot.py` (`BASE_RULES` + `PERSONAS`/`VOCAB`/
  `OPENERS`). Add a persona to `PERSONAS` to expand the range. See `WISHLIST.md`.
