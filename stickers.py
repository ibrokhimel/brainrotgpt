"""Sending stickers from the owner's own pack.

Every sticker in a Telegram pack carries an associated emoji, so the pack labels
itself — no manual tagging. The set is re-read daily, which means adding stickers
in Telegram makes them available to the kid with no redeploy.
"""
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import config
import db

logger = logging.getLogger("brainrotgpt.stickers")

NO_REPEAT_WINDOW = 10   # don't resend the same file_id within this many sends

# kid_state key the /stickers command persists its override under. Unset means
# "no override yet" (fall back to config.STICKER_PACK_NAME); set to "" means
# an explicit /stickers off, which disables the feature even if the .env has
# a default configured.
STICKER_PACK_KEY = "sticker_pack"

# kid_state key for the /stickers "send me a sticker and i'll read it" prompt.
# Holds the epoch time it was armed, "" when idle. A bare flag with no expiry
# would let a /stickers typed and forgotten silently hijack a sticker sent
# hours later, so a capture only counts within CAPTURE_WINDOW_S of being armed.
AWAITING_STICKER_KEY = "awaiting_sticker_at"
CAPTURE_WINDOW_S = 300  # 5 minutes


@dataclass(frozen=True)
class Sticker:
    file_id: str
    emoji: str


_by_emoji: dict[str, list[str]] = {}
_all: list[str] = []
_recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=NO_REPEAT_WINDOW))


def reset() -> None:
    """Drop the cache. Used by tests and by a failed reload."""
    _by_emoji.clear()
    _all.clear()
    _recent.clear()


def enabled() -> bool:
    return bool(_all)


def available_emoji() -> list[str]:
    return sorted(_by_emoji)


def current_pack_name() -> str | None:
    """The pack name load() would use next. None means stickers are disabled.

    kid_state["sticker_pack"] overrides config.STICKER_PACK_NAME when the
    owner has run /stickers. Unset falls back to the .env default; explicitly
    set to "" (via /stickers off) disables the feature outright, even with a
    .env default configured.
    """
    stored = db.get_kid_state(STICKER_PACK_KEY, default=None)
    if stored is not None:
        return stored or None
    return config.STICKER_PACK_NAME or None


def status() -> dict:
    """Snapshot for the /stickers report: configured pack, and what's loaded."""
    return {
        "pack_name": current_pack_name(),
        "count": len(_all),
        "emoji_count": len(_by_emoji),
    }


def arm_capture() -> None:
    """Mark that the next sticker from the owner should be read as the pack."""
    db.set_kid_state(AWAITING_STICKER_KEY, str(time.time()))


def disarm_capture() -> None:
    db.set_kid_state(AWAITING_STICKER_KEY, "")


def capture_pending() -> bool:
    """Whether an armed /stickers capture is still within its window."""
    raw = db.get_kid_state(AWAITING_STICKER_KEY, default="")
    if not raw:
        return False
    try:
        armed_at = float(raw)
    except ValueError:
        return False
    return time.time() - armed_at < CAPTURE_WINDOW_S


async def load(bot) -> int:
    """Read the configured pack into the cache. Never raises."""
    reset()
    pack_name = current_pack_name()
    if not pack_name:
        return 0
    try:
        pack = await bot.get_sticker_set(pack_name)
    except Exception as e:  # noqa: BLE001 — a missing pack must not break the bot
        logger.warning("sticker pack %r failed to load: %s", pack_name, e)
        return 0
    for s in getattr(pack, "stickers", []):
        emoji = (getattr(s, "emoji", "") or "").strip()
        if not emoji:
            continue
        _by_emoji.setdefault(emoji, []).append(s.file_id)
        _all.append(s.file_id)
    logger.info("loaded %d sticker(s) across %d emoji", len(_all), len(_by_emoji))
    return len(_all)


def _choose(candidates: list[str], chat_id: int, *, rng) -> str | None:
    if not candidates:
        return None
    recent = _recent[chat_id]
    fresh = [c for c in candidates if c not in recent] or candidates
    picked = rng.choice(fresh)
    recent.append(picked)
    return picked


def pick(chat_id: int, emoji: str, *, rng) -> str | None:
    """A file_id for this emoji, avoiding recent repeats. None if unknown."""
    return _choose(_by_emoji.get(emoji, []), chat_id, rng=rng)


def pick_random(chat_id: int, *, rng) -> str | None:
    return _choose(_all, chat_id, rng=rng)
