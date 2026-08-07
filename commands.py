"""Slash commands, the /settings UI, and the callback handler that services it.

Split out of bot.py purely to keep that file under the project's line cap —
this module owns nothing bot.py doesn't already delegate to it: the commands
users type, the settings keyboard they open, and the button taps that drive
it. Message intake (on_user_message/on_photo/on_group_message) and process
lifecycle stay in bot.py. Like scheduler.py, this module never imports bot —
bot.py imports and registers these handlers, not the other way around.
"""
import random
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import brainrot
import chat_engine
import db
import guard
import stickers
import trends

WELCOME = (
    "yo 🗿 i'm jayden\n\n"
    "just text me like you would anyone else, i'll hit you back "
    "(might take me a sec, i'm not just sitting here)\n\n"
    "/settings — mood · chattiness · mute\n"
    "/help for more"
)

# Commands shown in Telegram's "/" menu (set via set_my_commands on startup).
# /trend is owner-only, so it's intentionally left out of the public menu.
BOT_COMMANDS = [
    BotCommand("start", "wake the bot up 🗿"),
    BotCommand("settings", "mood · chattiness · mute 🎭"),
    BotCommand("shutup", "mute the kid 🤐"),
    BotCommand("yo", "unmute the kid 🗿"),
    BotCommand("help", "how this thing works ❓"),
]

HELP = (
    "jayden 🗿\n\n"
    "just text me normally, no special format needed. i reply in a few "
    "separate messages usually, not one big paragraph — might take a sec\n\n"
    "leave me on read too long and don't be shocked if i hit you up again 👀\n\n"
    "send a screenshot and i'll react to it\n\n"
    "in groups: @ mention me (and turn my privacy mode OFF in BotFather so "
    "i can actually see the chat)\n\n"
    "i sleep like 1am-9am so don't expect much then 😴\n\n"
    "/settings — mood · chattiness · mute\n"
    "/shutup — make me stop texting you\n"
    "/yo — bring me back"
)

_rng = random.Random()


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


def _extract_pack_name(raw: str) -> str:
    """Pull a bare pack short-name out of whatever the owner pasted.

    Accepts a bare name ("mypack") or a full invite link Telegram hands out
    ("https://t.me/addstickers/mypack", with or without scheme/query string).
    """
    raw = raw.strip()
    if "addstickers/" in raw:
        raw = raw.split("addstickers/", 1)[1]
    return raw.split("?", 1)[0].strip("/ \t")


async def cmd_stickers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only sticker pack control: /stickers [<pack_name_or_link> | off].

    Stickers are sent from the owner's own Telegram pack. This lets the owner
    swap it at runtime instead of editing STICKER_PACK_NAME in .env and
    restarting — see stickers.load() for the kid_state-over-config resolution
    order this writes into.
    """
    if not guard.is_owner(update.effective_user.id):
        await update.message.reply_text("owner only 🔒")
        return
    args = context.args or []

    if not args:
        # Arm the capture window every time — the owner can just send a
        # sticker from the pack next instead of typing/pasting a name.
        stickers.arm_capture()
        prompt = "send me a sticker from the pack and i'll read it 🗿"
        s = stickers.status()
        if not s["pack_name"]:
            await update.message.reply_text(f"stickers are off 🚫\n{prompt}")
            return
        await update.message.reply_text(
            f"pack: {s['pack_name']}\n{s['count']} sticker(s) · {s['emoji_count']} emoji\n\n{prompt}"
        )
        return

    stickers.disarm_capture()  # a typed name/off resolves the pending prompt

    if args[0].lower() == "off":
        db.set_kid_state(stickers.STICKER_PACK_KEY, "")
        await stickers.load(context.bot)
        await update.message.reply_text("stickers off 🚫")
        return

    pack_name = _extract_pack_name(args[0])
    if not pack_name:
        await update.message.reply_text("usage: /stickers [<pack_name_or_link> | off]")
        return

    db.set_kid_state(stickers.STICKER_PACK_KEY, pack_name)
    count = await stickers.load(context.bot)
    if count:
        await update.message.reply_text(f"pack set ✅ {pack_name} — loaded {count} sticker(s)")
    else:
        await update.message.reply_text(
            f"couldn't load pack {pack_name} 😭 (bad name, or it's empty) — stickers off for now"
        )


async def try_capture_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If an owner-armed /stickers capture is pending and unexpired, read the
    pack off this sticker and load it — instead of the sticker being treated
    as an ordinary message. See stickers.capture_pending() for the window.

    Only a *successful* load disarms the flag. On failure (no set_name, or
    the pack doesn't load) it stays armed so the owner can just send a
    different sticker instead of re-typing /stickers — the CAPTURE_WINDOW_S
    expiry is what bounds it, not a single-attempt limit. Returns whether
    this sticker was consumed as a capture attempt; bot.on_sticker falls
    through to ordinary intake when this returns False (not owner, or
    nothing pending).
    """
    msg = update.message
    user_id = msg.from_user.id if msg.from_user else 0
    if not (guard.is_owner(user_id) and stickers.capture_pending()):
        return False

    set_name = msg.sticker.set_name
    if not set_name:
        await update.message.reply_text(
            "that one's not part of a pack i can read 🤷 — still waiting, try another sticker from it"
        )
        return True

    db.set_kid_state(stickers.STICKER_PACK_KEY, set_name)
    count = await stickers.load(context.bot)
    if count:
        stickers.disarm_capture()
        await update.message.reply_text(f"pack set ✅ {set_name} — loaded {count} sticker(s)")
    else:
        await update.message.reply_text(
            f"couldn't load pack {set_name} 😭 (bad name, or it's empty) — still waiting, try another sticker"
        )
    return True


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
        return

    # Always answer, even for callback data we no longer understand. Live users
    # have v2 messages in scrollback with set:* / v:* buttons; an unanswered
    # callback leaves their client spinning until it times out. Upgrade-day
    # symptom, two lines.
    await query.answer()
