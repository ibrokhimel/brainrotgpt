"""What the kid remembers: the recent conversation, plus a distilled notes blob.

The window is verbatim and cheap. The notes are a slow, occasional summary — your
name, what you do, what you keep complaining about — which is what makes a
stage-3 ghost ping able to say "still thinkin about ur thing btw".
"""
import logging
import time

from groq import AsyncGroq

import budget
import config
import db

logger = logging.getLogger("brainrotgpt.memory")

NOTES_EVERY = 15          # distil after this many messages
NOTES_MAX_CHARS = 600     # hard cap so notes can't grow into the prompt

_clients = [AsyncGroq(api_key=k) for k in config.GROQ_KEYS]

_PROMPT = (
    "Below is a chat between a teenager and someone they text. Write a SHORT "
    "third-person note (max 80 words) recording only durable facts about the "
    "OTHER person: their name, what they do, what they keep bringing up, running "
    "jokes. No greetings, no commentary, no speculation. If there is nothing "
    "worth recording, reply with the single word NONE.\n\nEXISTING NOTES:\n{notes}"
    "\n\nCHAT:\n{chat}"
)


def transcript(chat_id: int, limit: int = 20) -> str:
    """Render the recent window for the prompt. 'them' is the user, 'me' the kid."""
    rows = db.recent_messages(chat_id, limit=limit)
    return "\n".join(
        f"{'me' if r['role'] == 'kid' else 'them'}: {r['text']}" for r in rows
    )


def should_distill(state: dict) -> bool:
    return int(state.get("msgs_since_notes") or 0) >= NOTES_EVERY


async def _ask(prompt: str) -> str:
    last_err: Exception | None = None
    for client in _clients:
        try:
            resp = await client.chat.completions.create(
                model=config.GROQ_FALLBACK_MODEL,   # cheap model: this is bookkeeping
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err or RuntimeError("no groq client")


async def distill(chat_id: int, state: dict) -> str:
    """Rewrite the chat's notes. Always resets the counter, even on failure."""
    old = state.get("notes") or ""
    notes = old
    if budget.can_spend(time.time()):
        try:
            raw = await _ask(_PROMPT.format(notes=old or "(none)", chat=transcript(chat_id, 40)))
            budget.spend(time.time())
            if raw and raw.strip().upper() != "NONE":
                notes = raw[:NOTES_MAX_CHARS]
        except Exception as e:  # noqa: BLE001 — memory is a nicety, never a blocker
            logger.warning("notes distillation failed for chat %s: %s", chat_id, e)
    db.update_chat_state(chat_id, notes=notes, msgs_since_notes=0)
    return notes
