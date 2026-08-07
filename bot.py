"""BrainrotGPT Telegram bot.

Forward/paste a convo (or a screenshot), tweak the style, and get a short
brainrot reply you can paste straight back (length is adjustable in /settings).
Long polling by default; optional webhook mode. Persists settings to SQLite.
"""
import asyncio
import datetime
import hashlib
import logging
import random
import socket
import time

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
import chat_engine
import config
import db
import ghost
import guard
import health
import life
import scheduler
import stickers
import trends
import vision
from rate_limit import RateLimiter
from scheduler import BOND_GAVE_UP, BOND_GHOST_STAGE, pings_remaining  # noqa: F401 — re-exported

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    handlers=(
        [logging.FileHandler(config.LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
        if config.LOG_FILE else None
    ),
)
logger = logging.getLogger("brainrotgpt")

limiter = RateLimiter(
    cooldown_s=config.RL_COOLDOWN_S,
    per_user_per_min=config.RL_PER_USER_PER_MIN,
    global_per_min=config.RL_GLOBAL_PER_MIN,
)

TG_LIMIT = 4096          # Telegram max message length
MSG_CAP = 3900           # leave room for footer + buttons in the controls message
DEBOUNCE_S = 1.5         # wait after the last message before showing the confirm card

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
    BotCommand("settings", "mood · chattiness · mute 🎭"),
    BotCommand("shutup", "mute the kid 🤐"),
    BotCommand("yo", "unmute the kid 🗿"),
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


def reply_to_bot(msg, bot_id: int) -> bool:
    """Was this message a reply to one of the bot's own messages?"""
    r = getattr(msg, "reply_to_message", None)
    return bool(r and getattr(r, "from_user", None) and r.from_user.id == bot_id)


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


# --- Keyboards ------------------------------------------------------------
#
# The user doesn't pick the kid — there's exactly one. /settings only turns
# the three dials chat_engine.py actually reads off chat_state: mood (reroll
# now), chattiness, and mute.

def settings_text(chat_id: int) -> str:
    s = db.get_chat_state(chat_id)
    mood = brainrot.PERSONA_BY_KEY.get(s["mood"], ("", s["mood"], ""))[1]
    status = "muted 🔇" if s["muted"] else "around 🟢"
    return (f"{chat_engine.KID_NAME} rn 🗿\n\n"
            f"mood: {mood}\nchattiness: {s['chattiness']}\nstatus: {status}")


def settings_kb(chat_id: int) -> InlineKeyboardMarkup:
    s = db.get_chat_state(chat_id)
    rows = [[InlineKeyboardButton("🎲 new mood", callback_data="kid:mood")]]
    rows.append([InlineKeyboardButton("💬 chattiness", callback_data="noop")])
    rows.append([
        InlineKeyboardButton(("• " if s["chattiness"] == c else "") + c,
                             callback_data=f"kid:chat:{c}")
        for c in db.CHATTINESS
    ])
    rows.append([InlineKeyboardButton(
        "🔊 unmute" if s["muted"] else "🔇 mute",
        callback_data="kid:mute:0" if s["muted"] else "kid:mute:1")])
    return InlineKeyboardMarkup(rows)


# --- Commands -------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(settings_text(cid), reply_markup=settings_kb(cid))


async def cmd_shutup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.update_chat_state(chat_id, muted=1, next_action_at=None, next_action_kind=None)
    await update.message.reply_text("aight bet 🤐")


async def cmd_yo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.update_chat_state(chat_id, muted=0, gave_up=0, ping_stage=0)
    await update.message.reply_text("im back 🗿")


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


# --- Message intake --------------------------------------------------------

BOND_PER_MESSAGE = 1
BOND_LONG_MESSAGE = 3
LONG_MESSAGE_CHARS = 200
LOW_CONTENT = {"lol", "ok", "okay", "k", "lmao", "yeah", "yea", "no", "nah", "haha", "true"}
REACTION_CHANCE = 0.35  # a low-content message sometimes just earns an emoji, not a reply

_rng = random.Random()


def apply_bond(state: dict, text: str) -> int:
    delta = BOND_LONG_MESSAGE if len(text) >= LONG_MESSAGE_CHARS else BOND_PER_MESSAGE
    return max(-100, min(100, int(state.get("bond") or 0) + delta))


def is_low_content(text: str) -> bool:
    stripped = text.strip().lower()
    return len(stripped) <= 4 or stripped in LOW_CONTENT


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A real person texted. Record it, reset the ghost ladder, schedule a reply."""
    msg = update.message
    if msg is None:
        return
    text = msg.text or msg.caption
    if not text:
        return
    chat_id = update.effective_chat.id
    user_id = msg.from_user.id if msg.from_user else 0
    if not guard.is_allowed_user(user_id):
        return
    ok, _ = guard.screen_input(text)
    if not ok:
        return
    # Protects the Groq quota from a chatty/abusive user. A refusal is silent —
    # a person who's tapped out just doesn't answer; the bot never explains.
    allowed, _ = limiter.check(user_id)
    if not allowed:
        return

    now = time.time()
    state = db.get_chat_state(chat_id)
    if state["muted"]:
        return
    db.add_message(chat_id, "user", text)
    limiter.record(user_id)

    # A low-content message sometimes earns just a reaction — and crucially arms
    # no ghost ping, because there is nothing to chase.
    if is_low_content(text) and _rng.random() < REACTION_CHANCE:
        try:
            await msg.set_reaction(_rng.choice(["💀", "🔥", "👀", "😭", "🗿"]))
        except Exception as e:  # noqa: BLE001 — reactions are cosmetic
            logger.debug("reaction failed: %s", e)
        else:
            db.update_chat_state(chat_id, last_user_ts=now, bond=apply_bond(state, text))
            return

    engaged = bool(state["last_kid_ts"] and now - state["last_kid_ts"] < 120)
    salty = bool(state["gave_up"])          # they're back after being given up on
    fields = {
        "bond": apply_bond(state, text) + (BOND_GAVE_UP if salty else 0),
        "ping_stage": 0,
        "last_user_ts": now,
        "msgs_since_notes": int(state["msgs_since_notes"] or 0) + 1,
        "next_action_kind": "reply",
        "next_action_at": ghost.schedule_reply_at(
            now, engaged=engaged, bond=int(state["bond"] or 0),
            salty=bool(state["salty"]), rng=_rng),
    }
    if salty:
        fields.update(gave_up=0, salty=1)
    db.update_chat_state(chat_id, **fields)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The kid looks at the picture and reacts to it in a burst — same
    scheduling path as a text message. No status message: a person doesn't
    narrate that they're looking at your photo."""
    msg = update.message
    chat_id = update.effective_chat.id
    user_id = msg.from_user.id if msg.from_user else 0
    if not guard.is_allowed_user(user_id):
        return
    file_id = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
    if not file_id:
        return
    state = db.get_chat_state(chat_id)
    if state["muted"]:
        return
    try:
        tg_file = await context.bot.get_file(file_id)
        data = await tg_file.download_as_bytearray()
        transcript = await vision.transcribe_image(bytes(data))
    except Exception as e:  # noqa: BLE001 — the kid never surfaces an error
        logger.warning("photo intake failed: %s", e)
        return
    now = time.time()
    db.add_message(chat_id, "user", f"[they sent a picture. it shows: {transcript}]")
    db.update_chat_state(chat_id, last_user_ts=now, next_action_kind="reply",
                         next_action_at=ghost.schedule_reply_at(
                             now, engaged=True, bond=int(state["bond"] or 0),
                             salty=False, rng=_rng))


GROUP_MAX_MESSAGES = 2


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """In groups: reply only on @mention or a reply to the bot, capped, and
    never proactive — unprompted messages in a group read as spam.

    Needs privacy mode OFF in BotFather (/setprivacy → Disable) to receive
    messages that don't @mention the bot.
    """
    msg = update.message
    if msg is None or not (msg.text or msg.caption):
        return
    me = context.bot
    if msg.from_user and msg.from_user.id == me.id:
        return  # never buffer or react to our own messages
    user_id = msg.from_user.id if msg.from_user else 0
    if not guard.is_allowed_user(user_id):
        return
    chat_id = update.effective_chat.id
    mentioned, leftover = parse_mention(msg, me.username, me.id)
    summoned = mentioned or reply_to_bot(msg, me.id)
    text = leftover if mentioned else (msg.text or msg.caption)
    if not text:
        return
    ok, _ = guard.screen_input(text)
    if not ok:
        return
    db.add_message(chat_id, "user", text)
    if not summoned:
        return
    state = db.get_chat_state(chat_id)
    if state["muted"]:
        return
    pieces = (await chat_engine.reply(chat_id, state, rng=_rng))[:GROUP_MAX_MESSAGES]
    await scheduler.deliver(context.bot, chat_id, pieces, state, reply_to=msg.message_id)


# --- Buttons --------------------------------------------------------------

async def handle_settings_cb(query, chat_id, data):
    await query.answer()
    if data == "kid:mood":
        mood = _rng.choice(brainrot.PERSONAS)[0]
        db.update_chat_state(chat_id, mood=mood, mood_set_at=time.time())
    elif data.startswith("kid:chat:"):
        value = data.split(":", 2)[2]
        if value in db.CHATTINESS:
            db.update_chat_state(chat_id, chattiness=value)
    elif data.startswith("kid:mute:"):
        muted = int(data.split(":", 2)[2])
        fields = {"muted": muted}
        if muted:
            fields["next_action_at"] = None
            fields["next_action_kind"] = None
        db.update_chat_state(chat_id, **fields)
    await query.edit_message_text(settings_text(chat_id), reply_markup=settings_kb(chat_id))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id

    if data == "noop":
        await query.answer()
        return

    if data.startswith("kid:"):
        await handle_settings_cb(query, chat_id, data)


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


# --- Lifecycle ------------------------------------------------------------
#
# The kid's own schedule (the 60s tick, burst delivery, daily jobs) lives in
# scheduler.py — see that module's docstring for why. bot.py only registers it.

async def on_startup(app: Application):
    db.init_db()
    limiter.seed(db.recent_generation_times(60))
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to set command menu: %s", e)
    await stickers.load(app.bot)

    app.job_queue.run_repeating(scheduler.tick, interval=60, first=10, name="tick")
    # No more in-memory intake buffers to evict (bot.py has no `sessions` dict
    # any more) — this job now exists purely to prune old rows out of `messages`.
    app.job_queue.run_repeating(
        scheduler.cleanup_sessions, interval=600, first=600, data={})
    app.job_queue.run_daily(
        scheduler.life_refresh_job,
        time=datetime.time(hour=config.LIFE_REFRESH_HOUR % 24),
        name="life_refresh",
    )
    app.job_queue.run_daily(
        scheduler.sticker_reload_job, time=datetime.time(hour=4), name="sticker_reload")
    if config.TREND_FETCH_ENABLED:
        app.job_queue.run_daily(
            scheduler.trend_refresh_job,
            time=datetime.time(hour=config.TREND_FETCH_HOUR % 24),
            name="trend_refresh",
        )
        if db.count_trends(source="auto") == 0:  # seed shortly after first boot
            app.job_queue.run_once(scheduler.trend_refresh_job, when=120, name="trend_refresh_seed")
    if not life.current():
        app.job_queue.run_once(scheduler.life_refresh_job, when=30, name="life_seed")

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
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("shutup", cmd_shutup))
    app.add_handler(CommandHandler("yo", cmd_yo))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("trend", cmd_trend))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
        on_user_message))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION), on_group_message))
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
