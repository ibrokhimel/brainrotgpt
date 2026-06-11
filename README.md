# BrainrotGPT 🗿📈

A personal Telegram bot: forward it a conversation, confirm, and it cooks you a
single unhinged **brainrot reply** you can paste straight back. Powered by Groq.

## How it works
1. You forward (or paste) one or more messages to the bot.
2. It buffers them and — once you stop sending — shows a preview with buttons.
3. Tap **✅ Generate** → it sends the conversation to a Groq model with the
   BrainrotGPT prompt and replies with one giant emoji-stuffed paragraph.
4. **🔄 Regenerate** rerolls; **🗑** clears the buffer.

## Setup (Windows)

1. **Create the bot token** — message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow the prompts, copy the token.
2. **Get a Groq key** — https://console.groq.com → API Keys.
3. **Fill in `.env`** — copy `.env.example` to `.env` and paste both values
   (already done if you set this up with the assistant):
   ```
   BOT_TOKEN=...
   GROQ_API_KEY=...
   GROQ_MODEL=llama-3.3-70b-versatile
   ```
4. **Install dependencies** (one-time):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
5. **Run it:**
   ```powershell
   .\.venv\Scripts\python.exe bot.py
   ```
   Leave that window open — the bot is live while it runs. Open Telegram, find
   your bot, send `/start`, and forward a convo.

> Without a virtual environment you can also just `pip install -r requirements.txt`
> and `python bot.py`.

## Commands
- `/start` – welcome + instructions
- `/done` – generate now (skip the wait)
- `/clear` – wipe the current buffer
- `/help` – quick help

## Config (in `.env`)
| Key | Default | Meaning |
|-----|---------|---------|
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Any Groq chat model id |
| `RL_COOLDOWN_S` | `8` | Seconds between generations per user |
| `RL_PER_USER_PER_MIN` | `15` | Max generations per user per minute |
| `RL_GLOBAL_PER_MIN` | `80` | Max generations across everyone per minute |

## Notes
- **Public bot:** anyone who finds it can use it; the rate limits above protect
  your Groq quota. Lock it to just yourself later by checking `user_id` in
  `on_message` / `on_button`.
- **State is in-memory:** buffers reset if you restart the bot. Fine for personal
  use; add SQLite if you want persistence.
- **Secrets:** `.env` is gitignored — never commit it or paste keys in chat.
- The brainrot prompt lives in `brainrot.py` (`SYSTEM_PROMPT`) — tweak freely.
