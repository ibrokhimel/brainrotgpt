"""Turn one model response into a sequence of separately-sent Telegram messages.

The kid texts in bursts, not paragraphs, so the model is asked to separate
messages with `|||`. Models drop format instructions roughly 1-in-20 calls, so a
sentence/newline fallback is mandatory — without it one reply in twenty arrives
as a single wall of text, which is exactly the tell this whole design exists to
avoid.
"""
import re
from dataclasses import dataclass

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
