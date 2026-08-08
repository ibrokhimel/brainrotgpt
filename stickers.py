"""Sending stickers from the owner's own packs.

Every sticker in a Telegram pack carries an associated emoji, so a pack labels
itself — no manual tagging. Several packs can be configured at once; they merge
into one emoji index, so the kid picks by what a sticker MEANS and never by
which pack it came from. The sets are re-read daily, which means adding
stickers in Telegram makes them available to the kid with no redeploy.
"""
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import config
import db

logger = logging.getLogger("brainrotgpt.stickers")

NO_REPEAT_WINDOW = 10   # don't resend the same file_id within this many sends

# kid_state key the /stickers command persists its override under, holding a
# JSON list of pack names. Unset means "no override yet" (fall back to
# config.STICKER_PACK_NAME); set to an empty list means an explicit /stickers
# off, which disables the feature even if the .env has a default configured.
#
# The key predates multi-pack support and older installs still have a bare
# pack name under it, so _decode() reads one as a one-element list. That is
# cheaper and less fragile than a migration, and self-heals on the next write.
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
_counts: dict[str, int] = {}   # pack name -> how many of its stickers loaded
_recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=NO_REPEAT_WINDOW))


def reset() -> None:
    """Drop the cache. Used by tests and by a failed reload."""
    _by_emoji.clear()
    _all.clear()
    _counts.clear()
    _recent.clear()


def enabled() -> bool:
    return bool(_all)


def available_emoji() -> list[str]:
    return sorted(_by_emoji)


def _decode(raw: str) -> list[str]:
    """kid_state's stored value -> pack names. Never raises on junk."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if not raw.startswith("["):
        return [raw]                      # legacy single bare name
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("sticker pack list is not valid JSON: %r", raw)
        return []
    if not isinstance(data, list):
        return []
    return [n for n in (str(x).strip() for x in data) if n]


def _store(names: list[str]) -> None:
    db.set_kid_state(STICKER_PACK_KEY, json.dumps(names))


def pack_names() -> list[str]:
    """The packs load() would read next, in order. Empty means disabled.

    kid_state["sticker_pack"] overrides config.STICKER_PACK_NAME once the
    owner has run /stickers. Unset falls back to the .env default; an
    explicitly stored empty list (via /stickers off) disables the feature
    outright, even with a .env default configured.
    """
    stored = db.get_kid_state(STICKER_PACK_KEY, default=None)
    if stored is not None:
        return _decode(stored)
    return [config.STICKER_PACK_NAME] if config.STICKER_PACK_NAME else []


def add_pack(name: str) -> bool:
    """Append a pack. False (and no write) if it's already configured."""
    name = name.strip()
    names = pack_names()
    if not name or name in names:
        return False
    _store(names + [name])
    return True


def remove_pack(name: str) -> bool:
    """Drop one pack, keeping the rest. False if it wasn't configured."""
    name = name.strip()
    names = pack_names()
    if name not in names:
        return False
    _store([n for n in names if n != name])
    return True


def clear_packs() -> None:
    """/stickers off — forget every pack, and stay off despite a .env default."""
    _store([])


def pack_count(name: str) -> int:
    """How many stickers this pack contributed to the last load(). 0 if it
    failed, was empty, or isn't configured."""
    return _counts.get(name, 0)


def status() -> dict:
    """Snapshot for the /stickers report: every configured pack with the count
    it contributed, plus the merged totals."""
    return {
        "packs": [(n, _counts.get(n, 0)) for n in pack_names()],
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
    """Read every configured pack into one merged cache. Never raises.

    Packs are independent: one that 404s or has been deleted is logged and
    skipped, and the rest still load. Losing one pack must never cost the
    owner the others.
    """
    reset()
    for pack_name in pack_names():
        _counts[pack_name] = await _load_one(bot, pack_name)
    logger.info("loaded %d sticker(s) across %d emoji from %d pack(s)",
                len(_all), len(_by_emoji), len(_counts))
    return len(_all)


async def _load_one(bot, pack_name: str) -> int:
    """Merge one pack into the cache. Returns how many stickers it added."""
    try:
        pack = await bot.get_sticker_set(pack_name)
    except Exception as e:  # noqa: BLE001 — a missing pack must not break the bot
        logger.warning("sticker pack %r failed to load: %s", pack_name, e)
        return 0
    n = 0
    for s in getattr(pack, "stickers", []):
        emoji = (getattr(s, "emoji", "") or "").strip()
        if not emoji:
            continue
        _by_emoji.setdefault(emoji, []).append(s.file_id)
        _all.append(s.file_id)
        n += 1
    return n


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
