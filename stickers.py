"""Sending stickers from the owner's own pack.

Every sticker in a Telegram pack carries an associated emoji, so the pack labels
itself — no manual tagging. The set is re-read daily, which means adding stickers
in Telegram makes them available to the kid with no redeploy.
"""
import logging
from collections import defaultdict, deque
from dataclasses import dataclass

import config

logger = logging.getLogger("brainrotgpt.stickers")

NO_REPEAT_WINDOW = 10   # don't resend the same file_id within this many sends


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


async def load(bot) -> int:
    """Read the configured pack into the cache. Never raises."""
    reset()
    if not config.STICKER_PACK_NAME:
        return 0
    try:
        pack = await bot.get_sticker_set(config.STICKER_PACK_NAME)
    except Exception as e:  # noqa: BLE001 — a missing pack must not break the bot
        logger.warning("sticker pack %r failed to load: %s", config.STICKER_PACK_NAME, e)
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
