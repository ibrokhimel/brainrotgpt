"""BrainrotGPT Telegram bot.

Forward/paste a convo (or a screenshot), tweak the style, and get a short
brainrot reply you can paste straight back (length is adjustable in /settings).
Long polling by default; optional webhook mode. Persists settings to SQLite.
"""
import asyncio
import datetime
import hashlib
import logging
import socket
import time
from collections import deque

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import brainrot
import config
import db
import guard
import health
import trends
import vision
from brainrot import PERSONA_BY_KEY, PERSONAS
from rate_limit import RateLimiter

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    handlers=(
        [logging.FileHandler(config.LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
        if config.LOG_FILE else None
    ),
)
logger = logging.getLogger("brainrotgpt")

# In-memory per-chat state (transient input staging + last candidates).
# chat_id -> {"buffer": [...], "candidates": [...], "cand_idx": int, "ts": float}
sessions: dict[int, dict] = {}

limiter = RateLimiter(
    cooldown_s=config.RL_COOLDOWN_S,
    per_user_per_min=config.RL_PER_USER_PER_MIN,
    global_per_min=config.RL_GLOBAL_PER_MIN,
)

TG_LIMIT = 4096          # Telegram max message length
MSG_CAP = 3900           # leave room for footer + buttons in the controls message
DEBOUNCE_S = 1.5         # wait after the last message before showing the confirm card
SESSION_TTL_S = 1800     # evict idle buffers after 30 min

INTENSITY_LABELS = {"mild": "🌶 Mild", "medium": "🔥 Medium", "unhinged": "☢️ Unhinged"}
LENGTH_LABELS = {"short": "💬 Short", "medium": "📄 Medium", "long": "📜 Long", "max": "🧱 Max"}
TONE_LABELS = {
    "default": "😐 Default", "roast": "🔥 Roast", "cope": "😤 Cope",
    "hype": "🚀 Hype", "deny": "🙅 Deny", "gaslight": "🌀 Gaslight",
}
# Languages ordered by Telegram's biggest user bases (Uzbek/English/Russian
# pinned up front), then ranked by country: India, Indonesia, Brazil, Iran,
# Egypt/Gulf, LatAm, Ukraine, Turkey.
LANGS = [
    ("auto", "🌐 Auto"), ("Uzbek", "🇺🇿 Uzbek"), ("English", "🇬🇧 English"),
    ("Russian", "🇷🇺 Russian"), ("Hindi", "🇮🇳 Hindi"), ("Indonesian", "🇮🇩 Indonesian"),
    ("Portuguese", "🇧🇷 Portuguese"), ("Persian", "🇮🇷 Persian"), ("Arabic", "🇸🇦 Arabic"),
    ("Spanish", "🇪🇸 Spanish"), ("Ukrainian", "🇺🇦 Ukrainian"), ("Turkish", "🇹🇷 Turkish"),
]

WELCOME = (
    "yo welcome to BrainrotGPT 🗿📈\n\n"
    "forward me a convo, paste it, or send a screenshot 📸 and i'll cook you a "
    "single unhinged brainrot reply you can paste straight back 😭🙏\n\n"
    "how it works:\n"
    "1️⃣ forward/paste the messages (or a screenshot)\n"
    "2️⃣ i show what i caught + a ✅ Generate button\n"
    "3️⃣ tap Generate → receive maximum aura 📈🗿\n\n"
    "🎭 /settings — style, length, intensity, tone, language, best-of-N\n"
    "commands: /done · /clear · /settings · /saved · /last · /leaderboard · /daily · /help"
)

# Commands shown in Telegram's "/" menu (set via set_my_commands on startup).
# stats is owner-only, so it's intentionally left out of the public menu.
BOT_COMMANDS = [
    BotCommand("start", "wake the bot up 🗿"),
    BotCommand("settings", "style · length · intensity · tone · language 🎭"),
    BotCommand("persona", "quick style picker 🎭"),
    BotCommand("clear", "wipe the current convo 🗑"),
    BotCommand("help", "how this thing works ❓"),
]

HELP = (
    "BrainrotGPT 🗿\n\n"
    "• forward/paste messages OR send a screenshot — i'll buffer them\n\n"
    "🎭 /settings — style / length / intensity / tone / language / best-of-N\n"
    "/persona — quick style picker\n"
    "in groups: add me + @mention me and i'll cook off the recent chat. turn my "
    "privacy mode OFF in BotFather so i can read messages 👀\n"
    "inline (any chat): @yourbot <paste the text> — inline can only see what you type\n\n"
    "commands: /clear · /help"
)


def get_session(chat_id: int) -> dict:
    s = sessions.setdefault(chat_id, {"buffer": [], "candidates": [], "cand_idx": 0})
    s["ts"] = time.time()
    return s


def group_history(chat_id: int) -> deque:
    """Rolling buffer of recent group messages (lives on the session so it's
    evicted by the same idle cleanup). Powers @mention auto-context in groups."""
    s = get_session(chat_id)
    dq = s.get("history")
    if dq is None:
        dq = deque(maxlen=config.GROUP_HISTORY_SIZE)
        s["history"] = dq
    return dq


def parse_mention(msg, bot_username: str | None, bot_id: int) -> tuple[bool, str]:
    """Return (was the bot @mentioned, the message text with that mention removed)."""
    text = msg.text or msg.caption or ""
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    uname = f"@{bot_username}".lower() if bot_username else None
    mentioned = False
    spans: list[tuple[int, int]] = []
    for e in entities:
        if e.type == "mention" and uname and text[e.offset:e.offset + e.length].lower() == uname:
            mentioned = True
            spans.append((e.offset, e.offset + e.length))
        elif e.type == "text_mention" and getattr(e, "user", None) and e.user.id == bot_id:
            mentioned = True
            spans.append((e.offset, e.offset + e.length))
    leftover = text
    for start, end in sorted(spans, reverse=True):
        leftover = leftover[:start] + leftover[end:]
    return mentioned, leftover.strip()


# --- Forward-origin helpers (unchanged) -----------------------------------

def sender_name(msg) -> str:
    fo = getattr(msg, "forward_origin", None)
    if fo is not None:
        su = getattr(fo, "sender_user", None)
        if su is not None:
            return su.full_name
        sun = getattr(fo, "sender_user_name", None)
        if sun:
            return sun
        sc = getattr(fo, "sender_chat", None)
        if sc is not None:
            return sc.title or "Chat"
        ch = getattr(fo, "chat", None)
        if ch is not None:
            return ch.title or "Channel"
    ff = getattr(msg, "forward_from", None)
    if ff is not None:
        return ff.full_name
    fsn = getattr(msg, "forward_sender_name", None)
    if fsn:
        return fsn
    return "Them"


def is_forwarded(msg) -> bool:
    return bool(
        getattr(msg, "forward_origin", None)
        or getattr(msg, "forward_from", None)
        or getattr(msg, "forward_sender_name", None)
    )


def build_transcript(buffer: list[dict]) -> str:
    lines = []
    for m in buffer:
        if m["sender"]:
            lines.append(f'{m["sender"]}: {m["text"]}')
        else:
            lines.append(m["text"])
    return "\n".join(lines)


def build_preview(buffer: list[dict]) -> str:
    parts = []
    for m in buffer:
        t = m["text"].replace("\n", " ")
        if len(t) > 120:
            t = t[:117] + "..."
        prefix = f'{m["sender"]}: ' if m["sender"] else ""
        parts.append(f"• {prefix}{t}")
    preview = "\n".join(parts)
    if len(preview) > 1500:
        preview = preview[:1500] + "\n…"
    return preview


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    return chunks


def persona_label_of(key: str) -> str:
    if key == "random":
        return "🎲 Random"
    return PERSONA_BY_KEY[key][1] if key in PERSONA_BY_KEY else key


# --- Keyboards ------------------------------------------------------------

def settings_text(chat_id: int) -> str:
    s = db.get_settings(chat_id)
    return (
        "⚙️ settings — tap to change\n\n"
        f"🎭 style: {persona_label_of(s['persona'])}\n"
        f"📏 length: {LENGTH_LABELS.get(s['length'], s['length'])}\n"
        f"🎚 intensity: {INTENSITY_LABELS.get(s['intensity'], s['intensity'])}\n"
        f"🎯 tone: {TONE_LABELS.get(s['tone'], s['tone'])}\n"
        f"🌐 language: {s['language']}\n"
        f"🎲 best-of: {s['candidates']}"
    )


def settings_kb(chat_id: int) -> InlineKeyboardMarkup:
    s = db.get_settings(chat_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🎭 {persona_label_of(s['persona'])}", callback_data="s:p"),
            InlineKeyboardButton(f"📏 {LENGTH_LABELS.get(s['length'], s['length'])}", callback_data="s:len"),
        ],
        [
            InlineKeyboardButton(INTENSITY_LABELS.get(s["intensity"], s["intensity"]), callback_data="s:i"),
            InlineKeyboardButton(TONE_LABELS.get(s["tone"], s["tone"]), callback_data="s:t"),
        ],
        [
            InlineKeyboardButton(f"🌐 {s['language']}", callback_data="s:l"),
            InlineKeyboardButton(f"🎲 Best-of {s['candidates']}", callback_data="s:c"),
        ],
        [InlineKeyboardButton("⬅️ Done", callback_data="s:done")],
    ])


def persona_kb() -> InlineKeyboardMarkup:
    opts = [("random", "🎲 Random")] + [(k, label) for k, label, _ in PERSONAS]
    rows, row = [], []
    for k, label in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"v:p:{k}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="s:m")])
    return InlineKeyboardMarkup(rows)


def _simple_kb(field: str, options: list[tuple[str, str]], columns: int = 2) -> InlineKeyboardMarkup:
    rows, row = [], []
    for value, label in options:
        row.append(InlineKeyboardButton(label, callback_data=f"v:{field}:{value}"))
        if len(row) == columns:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="s:m")])
    return InlineKeyboardMarkup(rows)


def lang_kb():
    return _simple_kb("l", [(v, lbl) for v, lbl in LANGS], columns=3)


# --- Commands -------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_chat.id)
    s["buffer"] = []
    s["prev_buffer"] = []
    s["candidates"] = []
    s["generated"] = False
    await update.message.reply_text("cleared 🗑️ send a fresh convo whenever 🙏")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(settings_text(cid), reply_markup=settings_kb(cid))


async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎭 pick a style:", reply_markup=persona_kb())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not guard.is_owner(update.effective_user.id):
        await update.message.reply_text("owner only 🔒")
        return
    s = db.stats()
    await update.message.reply_text(
        "📈 stats\n\n"
        f"total generations: {s['total']}\n"
        f"last 24h: {s['last_24h']}\n"
        f"unique users: {s['users']}\n"
        f"regenerates: {s['regens']}"
    )


async def cmd_trend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only live-trends curation: /trend [list|add <t>|ban <t>|remove <t>|refresh].

    Trends are mixed into every generated reply, so this is gated to owners.
    """
    if not guard.is_owner(update.effective_user.id):
        await update.message.reply_text("owner only 🔒")
        return
    args = context.args or []
    sub = args[0].lower() if args else "list"
    rest = " ".join(args[1:]).strip()

    if sub in ("list", "ls"):
        rows = db.list_trends(limit=40)
        auto = db.count_trends(source="auto")
        if not rows:
            await update.message.reply_text(
                "no live trends yet 📭\nadd one: /trend add 67 · pull some: /trend refresh"
            )
            return
        lines = [f"🔥 live trends ({len(rows)} shown · {auto} auto) — mixed into replies:\n"]
        for r in rows:
            lines.append(f"{'🤖' if r['source'] == 'auto' else '✍️'} {r['term']}")
        lines.append("\n/trend add <t> · ban <t> · remove <t> · refresh")
        await update.message.reply_text("\n".join(lines))
        return

    if sub == "add":
        if not rest:
            await update.message.reply_text("usage: /trend add <term>")
            return
        ok = db.add_trend(rest, source="manual")
        await update.message.reply_text(f"added ✅ {rest}" if ok else f"already live / banned 🤔 {rest}")
        return

    if sub in ("ban", "block"):
        if not rest:
            await update.message.reply_text("usage: /trend ban <term>")
            return
        db.ban_trend(rest)
        await update.message.reply_text(f"banned 🚫 {rest} (hidden + won't auto-readd)")
        return

    if sub in ("remove", "rm", "del", "delete"):
        if not rest:
            await update.message.reply_text("usage: /trend remove <term>")
            return
        ok = db.remove_trend(rest)
        await update.message.reply_text(f"removed 🗑 {rest}" if ok else f"not found 🤷 {rest}")
        return

    if sub == "refresh":
        await update.message.reply_text("pulling fresh trends 🔄… (best-effort)")
        try:
            n = await trends.refresh()
        except Exception as e:  # noqa: BLE001
            await update.message.reply_text(f"refresh failed 😭 ({str(e)[:80]})")
            return
        await update.message.reply_text(f"done ✅ +{n} new — see /trend list")
        return

    await update.message.reply_text(
        "usage: /trend [list | add <t> | ban <t> | remove <t> | refresh]"
    )


# --- Message intake + debounce -------------------------------------------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    text = msg.text or msg.caption
    if not text:
        await msg.reply_text("send me text, a screenshot 📸, or forwarded messages 🙏")
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    session["buffer"].append(
        {"sender": sender_name(msg) if is_forwarded(msg) else None, "text": text}
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = update.effective_chat.id
    file_id = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
    if not file_id:
        return
    status = await msg.reply_text("reading the screenshot 👀📸…")
    try:
        tg_file = await context.bot.get_file(file_id)
        data = await tg_file.download_as_bytearray()
        transcript = await vision.transcribe_image(bytes(data))
    except vision.VisionError as e:
        await status.edit_text(f"couldn't read that one 😭 ({e})\ntry pasting the text instead 🙏")
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("photo intake failed: %s", e)
        await status.edit_text("couldn't read that image 😭 try pasting the text instead 🙏")
        return
    session = get_session(chat_id)
    session["buffer"].append({"sender": None, "text": transcript})
    await status.edit_text("got the screenshot ✅")


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """In groups: buffer recent messages so there's context for a later task to
    wire up @mention/reply handling against.

    Needs privacy mode OFF in BotFather (/setprivacy → Disable) to receive
    messages that don't @mention the bot.
    """
    msg = update.message
    if msg is None or not (msg.text or msg.caption):
        return
    me = context.bot
    if msg.from_user and msg.from_user.id == me.id:
        return  # never buffer or react to our own messages
    chat_id = update.effective_chat.id
    text = msg.text or msg.caption
    sender = msg.from_user.full_name if msg.from_user else "Them"
    group_history(chat_id).append({"sender": sender, "text": text})


# --- Buttons --------------------------------------------------------------

async def handle_settings_cb(query, chat_id, data):
    await query.answer()
    if data == "s:done":
        await query.edit_message_text("settings saved ✅ send a convo whenever 🙏")
        return
    if data == "s:m":
        await query.edit_message_text(settings_text(chat_id), reply_markup=settings_kb(chat_id))
        return
    if data == "s:p":
        await query.edit_message_text("🎭 pick a style:", reply_markup=persona_kb())
        return
    if data == "s:l":
        await query.edit_message_text("🌐 pick a language:", reply_markup=lang_kb())
        return
    if data.startswith("v:"):
        _, field, value = data.split(":", 2)
        keymap = {
            "p": "persona", "i": "intensity", "len": "length",
            "t": "tone", "l": "language", "c": "candidates",
        }
        key = keymap.get(field)
        if key:
            try:
                db.set_setting(chat_id, key, int(value) if field == "c" else value)
            except Exception as e:  # noqa: BLE001
                logger.warning("bad setting %s=%s: %s", key, value, e)
        await query.edit_message_text(settings_text(chat_id), reply_markup=settings_kb(chat_id))
        return


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    session = get_session(chat_id)

    if data == "noop":
        await query.answer()
        return

    if data.startswith("s:") or data.startswith("v:"):
        await handle_settings_cb(query, chat_id, data)
        return

    if data == "add":
        await query.answer()
        await query.edit_message_text("aight, forward/paste more then hit /done 👍")
        return

    if data in ("clr", "new"):
        await query.answer()
        session["buffer"] = []
        session["prev_buffer"] = []
        session["candidates"] = []
        session["generated"] = False
        await query.edit_message_text("cleared 🗑️ send a fresh convo whenever 🙏")
        return


# --- Inline mode ----------------------------------------------------------

def _article(rid, title, message, description=None):
    return InlineQueryResultArticle(
        id=rid,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message[:4096]),
    )


async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = (iq.query or "").strip()
    user_id = iq.from_user.id
    if not q:
        await iq.answer(
            [_article("hint", "type a message to brainrot…", "type something after the bot name 🗿")],
            cache_time=5,
        )
        return
    if not guard.is_allowed_user(user_id):
        await iq.answer([_article("locked", "this bot is private 🔒", "this bot is private 🔒")], cache_time=5)
        return
    allowed, reason = limiter.check(user_id)
    if not allowed:
        await iq.answer([_article("rl", "slow down a sec ⏳", reason or "slow down ⏳")], cache_time=2)
        return
    settings = {"persona": "random", "intensity": "mild", "length": "short",
                "tone": "default", "language": "auto", "candidates": 1}
    try:
        res = await asyncio.wait_for(brainrot.generate(q, settings), timeout=9)
        limiter.record(user_id)
        await iq.answer(
            [_article("r", "🗿 brainrot reply ready — tap to send", res.text, description=res.text[:80])],
            cache_time=0,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("inline failed: %s", e)
        await iq.answer(
            [_article("err", "couldn't cook in time 😭", "open the bot and use /done 🙏")],
            cache_time=0,
        )


# --- Scheduled jobs -------------------------------------------------------

async def cleanup_sessions(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    stale = [cid for cid, s in sessions.items() if now - s.get("ts", now) > SESSION_TTL_S]
    for cid in stale:
        sessions.pop(cid, None)
    if stale:
        logger.info("cleaned %d idle session(s)", len(stale))


async def trend_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled best-effort pull of fresh slang into the live trends table."""
    try:
        n = await trends.refresh()
        logger.info("scheduled trend refresh added %d term(s)", n)
    except Exception as e:  # noqa: BLE001 — never let the job crash the queue
        logger.warning("trend refresh job failed: %s", e)


# --- Lifecycle ------------------------------------------------------------

async def on_startup(app: Application):
    db.init_db()
    limiter.seed(db.recent_generation_times(60))
    app.job_queue.run_repeating(cleanup_sessions, interval=600, first=600)
    if config.TREND_FETCH_ENABLED:
        app.job_queue.run_daily(
            trend_refresh_job,
            time=datetime.time(hour=config.TREND_FETCH_HOUR % 24),
            name="trend_refresh",
        )
        if db.count_trends(source="auto") == 0:  # seed shortly after first boot
            app.job_queue.run_once(trend_refresh_job, when=120, name="trend_refresh_seed")
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to set command menu: %s", e)
    logger.info("startup complete (model=%s, fallback=%s)", config.GROQ_MODEL, config.GROQ_FALLBACK_MODEL)


async def on_shutdown(app: Application):
    db.close()


# --- Error handling + single-instance guard -------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Central error handler so failures are logged cleanly (the alternative is
    PTB's 'No error handlers are registered' + a full traceback per error)."""
    err = context.error
    if isinstance(err, Conflict):
        logger.error(
            "getUpdates 409 conflict — another instance is polling this bot token. "
            "Only ONE instance may run. Stop the duplicate (see SINGLE_INSTANCE_LOCK)."
        )
        return
    logger.error("unhandled error while processing an update: %r", err, exc_info=err)


# Held for the whole process lifetime; the OS frees it on exit (no stale locks).
_instance_lock_sock: socket.socket | None = None


def acquire_single_instance_lock() -> bool:
    """Best-effort single-instance guard: bind a localhost port derived from the
    bot token. Another running instance of the SAME bot already holds it, so the
    bind fails and we return False. Auto-released when the process dies."""
    global _instance_lock_sock
    digest = hashlib.sha256(config.BOT_TOKEN.encode()).digest()
    port = 49152 + (int.from_bytes(digest[:2], "big") % 10000)  # stable high port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))  # no SO_REUSEADDR → exclusive bind
    except OSError:
        s.close()
        return False
    s.listen(1)
    _instance_lock_sock = s
    return True


# --- Entry point ----------------------------------------------------------

def main():
    if config.SINGLE_INSTANCE_LOCK and not acquire_single_instance_lock():
        logger.error(
            "another BrainrotGPT instance is already running for this bot token — "
            "exiting to avoid a getUpdates 409 conflict. "
            "(set SINGLE_INSTANCE_LOCK=false to override.)"
        )
        raise SystemExit(1)

    health.start_health_server(config.HEALTH_PORT)

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["clear", "cancel"], cmd_clear))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("persona", cmd_persona))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("trend", cmd_trend))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE), on_photo
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_message
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, on_group_message
    ))
    app.add_error_handler(on_error)

    if config.WEBHOOK_URL:
        logger.info("BrainrotGPT starting on webhook %s", config.WEBHOOK_URL)
        app.run_webhook(
            listen=config.WEBHOOK_LISTEN,
            port=config.WEBHOOK_PORT,
            url_path=config.BOT_TOKEN,
            webhook_url=f"{config.WEBHOOK_URL.rstrip('/')}/{config.BOT_TOKEN}",
            secret_token=config.WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        logger.info("BrainrotGPT starting on long polling…")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
