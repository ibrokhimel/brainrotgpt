"""BrainrotGPT: a Telegram bot that behaves like one specific teenager.

This module is intake and wiring only — it turns Telegram updates into rows in
`chat_state` and `messages`, and registers the jobs that act on them. It never
replies synchronously in a DM: a message schedules a reply and the tick in
scheduler.py delivers it, because a person who answers instantly every time
isn't a person. Groups are the exception (§10) and reply inline when summoned.

Long polling by default; optional webhook mode. All durable state is SQLite.
"""
import asyncio
import datetime
import hashlib
import logging
import random
import socket
import time

from telegram import (
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
import commands
import config
import db
import ghost
import guard
import health
import life
import scheduler
import stickers
import vision
from commands import cmd_shutup, cmd_yo, settings_kb, settings_text  # noqa: F401 — re-exported
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


def intake_fields(state: dict, now: float, *, bond: int, engaged: bool,
                  schedule: bool = True) -> dict:
    """The chat_state changes that every message from a real person makes.

    One place, because the three intake paths (text, reaction-only, photo) had
    each grown a private copy and drifted apart. The reaction path forgot to
    disarm the ghost ping the *scheduler* had armed, so a user who replies "lol"
    got pinged as if they'd ghosted; the photo path forgot to clear `gave_up`,
    which left the chat invisible to both `due_chats` and `coldopen_candidates`
    — unreachable in either direction, permanently.

    Spec §6: any message from you resets `ping_stage` and clears the pending
    action. `schedule=False` means "clear it and arm nothing" — the reaction
    path, where the reaction *is* the reply.

    Note `bond` is passed in already clamped by `apply_bond`. The give-up
    penalty is NOT re-applied here: `scheduler._do_ping` charges it once, at
    give-up. Charging it again would be charging the user for coming back.
    """
    # Sticky: an undelivered wounded reply stays owed, so the slow salty delay
    # still applies even if `gave_up` was already cleared by an earlier message.
    salty = bool(state["gave_up"]) or bool(state["salty"])
    fields = {
        "bond": bond,
        "ping_stage": 0,
        "last_user_ts": now,
        "msgs_since_notes": int(state["msgs_since_notes"] or 0) + 1,
        "next_action_kind": "reply" if schedule else None,
        # bot.py reads `life` and hands ghost the answer, so ghost.py stays a
        # pure function of (state, now, rng) with no DB behind it.
        "next_action_at": ghost.schedule_reply_at(
            now, engaged=engaged, bond=bond, salty=salty, rng=_rng,
            in_school=life.in_school_block(now)) if schedule else None,
    }
    if salty:
        fields.update(gave_up=0, salty=1)
    return fields


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

    # A low-content message sometimes earns just a reaction — and arms no ghost
    # ping, because there is nothing to chase. It must still DISARM any ping the
    # scheduler already armed: the reaction is the kid answering, not ignoring.
    if is_low_content(text) and _rng.random() < REACTION_CHANCE:
        try:
            await msg.set_reaction(_rng.choice(["💀", "🔥", "👀", "😭", "🗿"]))
        except Exception as e:  # noqa: BLE001 — reactions are cosmetic
            logger.debug("reaction failed: %s", e)
        else:
            db.update_chat_state(chat_id, **intake_fields(
                state, now, bond=apply_bond(state, text), engaged=True, schedule=False))
            return

    engaged = bool(state["last_kid_ts"] and now - state["last_kid_ts"] < 120)
    db.update_chat_state(chat_id, **intake_fields(
        state, now, bond=apply_bond(state, text), engaged=engaged))


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
    # A photo costs a multimodal call, so it needs the same quota guard a text
    # message gets — otherwise one user spamming an album drains Groq for every
    # chat. Silent refusal, like everywhere else.
    allowed, _ = limiter.check(user_id)
    if not allowed:
        return
    try:
        tg_file = await context.bot.get_file(file_id)
        data = await tg_file.download_as_bytearray()
        transcript = await vision.transcribe_image(bytes(data))
    except Exception as e:  # noqa: BLE001 — the kid never surfaces an error
        logger.warning("photo intake failed: %s", e)
        return
    # The transcript is model output describing an untrusted image; screen it
    # before it becomes conversation history the prompt builder will replay.
    ok, _ = guard.screen_input(transcript)
    if not ok:
        return
    limiter.record(user_id)
    now = time.time()
    db.add_message(chat_id, "user", f"[they sent a picture. it shows: {transcript}]")
    # A photo is a message from a person: it revives a given-up chat exactly as
    # text does. Without this the reply is scheduled on a row `due_chats` skips.
    db.update_chat_state(chat_id, **intake_fields(
        state, now, bond=apply_bond(state, ""), engaged=True))


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
        await app.bot.set_my_commands(commands.BOT_COMMANDS)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to set command menu: %s", e)
    await stickers.load(app.bot)

    app.job_queue.run_repeating(scheduler.tick, interval=60, first=10, name="tick")
    app.job_queue.run_repeating(scheduler.prune_job, interval=600, first=600, name="prune")
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
    app.add_handler(CommandHandler("start", commands.cmd_start))
    app.add_handler(CommandHandler("help", commands.cmd_help))
    app.add_handler(CommandHandler("settings", commands.cmd_settings))
    app.add_handler(CommandHandler("shutup", commands.cmd_shutup))
    app.add_handler(CommandHandler("yo", commands.cmd_yo))
    app.add_handler(CommandHandler("stats", commands.cmd_stats))
    app.add_handler(CommandHandler("trend", commands.cmd_trend))
    app.add_handler(CallbackQueryHandler(commands.on_button))
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
