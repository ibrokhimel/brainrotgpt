"""Turn one model response into a sequence of separately-sent Telegram messages.

The kid texts in bursts, not paragraphs, so the model is asked to separate
messages with `|||`. Models drop format instructions roughly 1-in-20 calls, so a
sentence/newline fallback is mandatory — without it one reply in twenty arrives
as a single wall of text, which is exactly the tell this whole design exists to
avoid.
"""
import logging
import re
from dataclasses import dataclass

from telegram.error import Forbidden

DELIM = "|||"

# [sticker:💀] as a whole segment — the model's way of picking a sticker.
_STICKER_RE = re.compile(r"^\[sticker:\s*(\S+?)\s*\]$", re.IGNORECASE)
# Sentence boundary for the no-delimiter fallback.
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class Piece:
    kind: str   # "text" | "sticker"
    value: str  # message text, or the emoji for a sticker


def _clean(seg: str) -> str:
    return seg.strip().strip('"').strip("'").strip()


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Break an over-long message on word boundaries."""
    if len(text) <= max_chars:
        return [text]
    out, cur = [], ""
    for word in text.split():
        candidate = f"{cur} {word}".strip()
        if len(candidate) > max_chars and cur:
            out.append(cur)
            cur = word
        else:
            cur = candidate
        while len(cur) > max_chars:       # a single word longer than the cap
            out.append(cur[:max_chars])
            cur = cur[max_chars:]
    if cur:
        out.append(cur)
    return out


def _segments(raw: str) -> list[str]:
    if DELIM in raw:
        return raw.split(DELIM)
    parts = [ln for ln in raw.splitlines() if ln.strip()]
    if len(parts) > 1:
        return parts
    return _SENTENCE_RE.split(raw)


def parse(raw: str, *, max_msgs: int = 5, max_chars: int = 180) -> list[Piece]:
    """Split a model response into pieces, with a fallback when `|||` is absent."""
    pieces: list[Piece] = []
    for seg in _segments(raw or ""):
        seg = _clean(seg)
        if not seg:
            continue
        m = _STICKER_RE.match(seg)
        if m:
            pieces.append(Piece("sticker", m.group(1)))
            continue
        for chunk in _hard_split(seg, max_chars):
            chunk = chunk.strip().rstrip(".")
            if chunk:
                pieces.append(Piece("text", chunk))
    return pieces[:max_msgs]


logger = logging.getLogger("brainrotgpt.burst")

CHARS_PER_SEC = 14.0     # a fast thumb-typer
MAX_TYPING_S = 6.0
TYPO_CHANCE = 0.05
CORRECTION_CHANCE = 0.6


def typing_time(text: str, *, rng) -> float:
    return min(len(text) / CHARS_PER_SEC + rng.uniform(0.2, 0.8), MAX_TYPING_S)


def _typo(word: str, *, rng) -> str:
    if len(word) < 4:
        return word
    i = rng.randrange(len(word) - 1)
    return word[:i] + word[i + 1] + word[i] + word[i + 2:]


def apply_typos(pieces: list[Piece], *, rng) -> list[Piece]:
    """Occasionally fumble a word, sometimes followed by a `*correction`."""
    out: list[Piece] = []
    for p in pieces:
        if p.kind != "text" or rng.random() >= TYPO_CHANCE:
            out.append(p)
            continue
        words = p.value.split()
        if not words:
            out.append(p)
            continue
        i = rng.randrange(len(words))
        original, fumbled = words[i], _typo(words[i], rng=rng)
        if fumbled == original:
            out.append(p)
            continue
        words[i] = fumbled
        out.append(Piece("text", " ".join(words)))
        if rng.random() < CORRECTION_CHANCE:
            out.append(Piece("text", f"*{original}"))
    return out


async def send(bot, chat_id: int, pieces: list[Piece], *, rng, sleeper,
               sticker_for=None, reply_to: int | None = None) -> list[str]:
    """Send a burst at human pace. Returns the texts actually delivered.

    Every send is individually guarded: one failed message must not abort the
    rest of the burst, and must never raise into the caller.
    """
    delivered: list[str] = []
    first = True
    for piece in pieces:
        if not first:
            await sleeper(rng.uniform(0.5, 1.6))          # think gap
        reply_kw = {"reply_to_message_id": reply_to} if (first and reply_to) else {}
        try:
            if piece.kind == "sticker":
                file_id = sticker_for(piece.value) if sticker_for else None
                if not file_id:
                    continue
                await bot.send_sticker(chat_id, file_id, **reply_kw)
            else:
                await bot.send_chat_action(chat_id, "typing")
                await sleeper(typing_time(piece.value, rng=rng))
                await bot.send_message(chat_id, piece.value, **reply_kw)
                delivered.append(piece.value)
        except Forbidden:
            # The user blocked the bot — this must propagate so the caller can
            # mute the chat permanently, not be swallowed like an ordinary
            # send failure.
            raise
        except Exception as e:  # noqa: BLE001 — one bad send shouldn't kill the burst
            logger.warning("burst send failed in chat %s: %s", chat_id, e)
            continue
        first = False
    return delivered
