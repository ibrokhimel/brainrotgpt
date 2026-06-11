"""BrainrotGPT Telegram bot.

Forward (or paste) a conversation, confirm, and get one giant brainrot reply
you can paste straight back. Runs on long polling — no server needed.
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from brainrot import BrainrotError, generate
from rate_limit import RateLimiter

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("brainrotgpt")

# In-memory per-chat state: chat_id -> {"buffer": [ {"sender": str|None, "text": str} ]}
sessions: dict[int, dict] = {}

limiter = RateLimiter(
    cooldown_s=config.RL_COOLDOWN_S,
    per_user_per_min=config.RL_PER_USER_PER_MIN,
    global_per_min=config.RL_GLOBAL_PER_MIN,
)

TG_LIMIT = 4096          # Telegram max message length
DEBOUNCE_S = 1.5         # wait this long after the last message before confirming

WELCOME = (
    "yo welcome to BrainrotGPT 🗿📈\n\n"
    "forward me a convo (or just paste it) and i'll cook you a single unhinged "
    "brainrot reply you can paste straight back 😭🙏\n\n"
    "how it works:\n"
    "1️⃣ forward/paste the messages\n"
    "2️⃣ i'll show what i caught + a ✅ Generate button\n"
    "3️⃣ tap Generate → receive maximum aura 📈🗿\n\n"
    "commands: /done (generate now) · /clear (wipe) · /help"
)

HELP = (
    "BrainrotGPT 🗿\n\n"
    "• forward one or more messages (or paste text) — i'll buffer them\n"
    "• i show a preview + buttons once you stop sending\n"
    "• ✅ Generate cooks the reply · 🔄 Regenerate rerolls · 🗑 clears\n\n"
    "commands: /done · /clear · /help"
)


def get_session(chat_id: int) -> dict:
    return sessions.setdefault(chat_id, {"buffer": []})


def sender_name(msg) -> str:
    """Best-effort original sender name for a forwarded message."""
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
    # Legacy fields (older clients / privacy settings)
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


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Generate", callback_data="gen")],
            [
                InlineKeyboardButton("➕ Add more", callback_data="add"),
                InlineKeyboardButton("🗑 Clear", callback_data="clr"),
            ],
        ]
    )


def result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Regenerate", callback_data="regen"),
                InlineKeyboardButton("🗑 New", callback_data="new"),
            ]
        ]
    )


# --- Commands -------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_session(update.effective_chat.id)["buffer"].clear()
    await update.message.reply_text("cleared 🗑️ send a fresh convo whenever 🙏")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for job in context.job_queue.get_jobs_by_name(f"confirm_{chat_id}"):
        job.schedule_removal()
    await show_confirm(context.bot, chat_id)


# --- Message intake + debounce -------------------------------------------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    text = msg.text or msg.caption
    if not text:
        await msg.reply_text("send me text or forwarded text messages 🙏 (no media yet)")
        return

    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    session["buffer"].append(
        {"sender": sender_name(msg) if is_forwarded(msg) else None, "text": text}
    )
    schedule_confirm(context, chat_id)


def schedule_confirm(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    name = f"confirm_{chat_id}"
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    context.job_queue.run_once(send_confirm_job, DEBOUNCE_S, chat_id=chat_id, name=name)


async def send_confirm_job(context: ContextTypes.DEFAULT_TYPE):
    await show_confirm(context.bot, context.job.chat_id)


async def show_confirm(bot, chat_id: int):
    session = get_session(chat_id)
    if not session["buffer"]:
        return
    n = len(session["buffer"])
    preview = build_preview(session["buffer"])
    await bot.send_message(
        chat_id,
        f"got {n} message(s) 📥\n\n{preview}\n\nready to cook the brainrot reply? 🍳🗿",
        reply_markup=confirm_keyboard(),
    )


# --- Buttons --------------------------------------------------------------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    user_id = query.from_user.id
    session = get_session(chat_id)

    if data == "add":
        await query.answer()
        await query.edit_message_text("aight, forward/paste more then hit /done 👍")
        return

    if data in ("clr", "new"):
        await query.answer()
        session["buffer"].clear()
        await query.edit_message_text("cleared 🗑️ send a fresh convo whenever 🙏")
        return

    if data in ("gen", "regen"):
        if not session["buffer"]:
            await query.answer("buffer's empty, send a convo first 😅", show_alert=True)
            return
        allowed, reason = limiter.check(user_id)
        if not allowed:
            await query.answer(reason, show_alert=True)
            return
        await query.answer()
        await do_generate(context, query, chat_id, user_id, session)


COOKING_FRAMES = [
    "cooking the brainrot reply 🍳🗿",
    "aura farming the response 📈🗿",
    "consulting the skibidi council 🚽👑",
    "calculating the Fanum Tax 🍕📊",
    "John Pork is on the phone 📞🐷",
    "channeling maximum rizz 😭🙏",
    "escaping the shadow realm 🌌",
    "running interdimensional audit 👁️📈",
]
_DOTS = ["", ".", "..", "..."]
ANIM_INTERVAL = 0.8  # seconds between frames


async def animate(bot, chat_id, message_id, stop: asyncio.Event, last: str):
    """Loop editing one message with cycling brainrot frames until stop is set."""
    i = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=ANIM_INTERVAL)
            break  # stop was set — generation finished
        except asyncio.TimeoutError:
            pass
        i += 1
        frame = COOKING_FRAMES[(i // len(_DOTS)) % len(COOKING_FRAMES)]
        text = f"{frame}{_DOTS[i % len(_DOTS)]}"
        if text == last:
            continue
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
            last = text
        except Exception:
            pass  # ignore "not modified" / flood / deleted message


async def do_generate(context, query, chat_id, user_id, session):
    transcript = build_transcript(session["buffer"])
    bot = context.bot
    message_id = query.message.message_id

    # turn the confirm card into the first status frame (also drops the buttons)
    first_frame = COOKING_FRAMES[0]
    try:
        await query.edit_message_text(first_frame)
    except Exception:
        pass
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    limiter.record(user_id)

    # animate the status message while Groq cooks
    stop = asyncio.Event()
    anim = asyncio.create_task(animate(bot, chat_id, message_id, stop, first_frame))
    try:
        reply = await generate(transcript)
    except BrainrotError as e:
        stop.set()
        await anim
        logger.warning("generation failed: %s", e)
        err = f"bro the kitchen exploded 😭🍳💀 ({e})\ntry again with /done"
        try:
            await bot.edit_message_text(err, chat_id=chat_id, message_id=message_id)
        except Exception:
            await bot.send_message(chat_id, err)
        return
    stop.set()
    await anim

    # replace the status message with the result, splitting if over the limit
    chunks = split_text(reply)
    first_markup = result_keyboard() if len(chunks) == 1 else None
    try:
        await bot.edit_message_text(
            chunks[0], chat_id=chat_id, message_id=message_id, reply_markup=first_markup
        )
    except Exception:
        await bot.send_message(chat_id, chunks[0], reply_markup=first_markup)
    for i, chunk in enumerate(chunks[1:], start=1):
        markup = result_keyboard() if i == len(chunks) - 1 else None
        await bot.send_message(chat_id, chunk, reply_markup=markup)


# --- Entry point ----------------------------------------------------------

def main():
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler(["clear", "cancel"], cmd_clear))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("BrainrotGPT starting on long polling… (model=%s)", config.GROQ_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
