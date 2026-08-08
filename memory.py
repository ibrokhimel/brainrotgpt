"""What the kid remembers: the recent conversation, an accumulating fact list,
and a rolling notes blob.

The window is verbatim and cheap. Facts are atomic one-liners that pile up in
`db.facts` and never get rewritten away — that is the durable half. The notes
blob is the latest pass's read on the person. Together they are what makes a
stage-3 ghost ping able to say "still thinkin about ur thing btw".
"""
import logging
import re
import time

from groq import AsyncGroq

import budget
import config
import db

logger = logging.getLogger("brainrotgpt.memory")

# Six, not fifteen. Real exchanges are short and bursty: at fifteen a chat could
# run for days before the kid was allowed to notice anything about you, and in
# production every chat_state.notes was still empty after 35 messages.
NOTES_EVERY = 6           # distil after this many messages
NOTES_MAX_CHARS = 600     # hard cap so notes can't grow into the prompt
FACT_MAX_CHARS = 160      # one fact is one line, not a paragraph
FACTS_PER_PASS = 8        # most a single distillation may contribute

# How far back to push the next attempt when the model answered NONE. NOT a full
# cycle: NONE means "nothing yet", which on a thin early chat is the correct
# answer and will stop being correct after a few more messages.
NONE_BACKOFF = 3

_clients = [AsyncGroq(api_key=k) for k in config.GROQ_KEYS]

# One fact per line, not a paragraph: a paragraph can only be stored whole and
# rewritten whole, so a later pass silently drops what an earlier one caught.
# The bar for "worth recording" is deliberately low — this is a 14-year-old
# remembering their friend, not a CRM qualifying a lead.
_PROMPT = (
    "Below is a chat between a teenager and someone they text. List what is known "
    "about the OTHER person, ONE SHORT FACT PER LINE. Max 12 words per line. No "
    "bullets, no numbering, no headings, no commentary.\n"
    "Record anything a friend would remember: who they are, what they do, what they "
    "like or hate, what they keep complaining about, plans they mentioned, running "
    "jokes, how they talk. Small things count. Prefer several small facts over one "
    "long one.\n"
    "Include facts from EXISTING NOTES that still hold. Never speculate and never "
    "record anything about the teenager. Only if the chat says literally nothing "
    "about the other person, reply with the single word NONE.\n\n"
    "EXISTING NOTES:\n{notes}\n\nCHAT:\n{chat}"
)

# The model is told not to use bullets; it will anyway.
_BULLET = re.compile(r"^\s*(?:[-*•·–]|\d+[.)])\s*")


def transcript(chat_id: int, limit: int = 40) -> str:
    """Render the recent window for the prompt. 'them' is the user, 'me' the kid."""
    rows = db.recent_messages(chat_id, limit=limit)
    return "\n".join(
        f"{'me' if r['role'] == 'kid' else 'them'}: {r['text']}" for r in rows
    )


def last_kid_message(chat_id: int, limit: int = 10) -> str:
    """The kid's own most recent line, or "" if it hasn't spoken yet.

    The ghost ping needs this to be told what NOT to say again. Handing the
    model the transcript is not enough — it has to be pointed at the specific
    sentence it is being asked to diverge from.
    """
    for row in reversed(db.recent_messages(chat_id, limit=limit)):
        if row["role"] == "kid":
            return row["text"]
    return ""


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
                max_tokens=260,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err or RuntimeError("no groq client")


def facts_from(raw: str) -> list[str]:
    """Split a distillation into atomic, independently storable facts."""
    out: list[str] = []
    for line in (raw or "").splitlines():
        line = _BULLET.sub("", line).strip()
        if not line or line.upper().strip(".!") == "NONE":
            continue
        out.append(line[:FACT_MAX_CHARS])
        if len(out) >= FACTS_PER_PASS:
            break
    return out


async def distill(chat_id: int, state: dict) -> str:
    """Refresh what the kid knows about a chat: append facts, rewrite the notes.

    The two failure-ish outcomes are kept apart on purpose, because conflating
    them is what left memory empty in production. A model FAILURE resets the
    counter fully, so a broken model is not retried on every single message. A
    NONE is not a failure — the model worked and there was genuinely nothing to
    record yet, which on a thin early chat is the right answer and stops being
    right a few messages later. Resetting for that threw the counter away and
    pushed the next attempt a whole cycle out, so a chat could stay memoryless
    forever; NONE now backs off by a few messages only.
    """
    old = state.get("notes") or ""
    notes, since = old, 0
    if budget.can_spend(time.time()):
        try:
            raw = await _ask(_PROMPT.format(notes=old or "(none)", chat=transcript(chat_id)))
            budget.spend(time.time())
            facts = facts_from(raw)
            if facts:
                for fact in facts:
                    db.add_fact(chat_id, fact)
                notes = "\n".join(facts)[:NOTES_MAX_CHARS]
            else:
                since = max(0, NOTES_EVERY - NONE_BACKOFF)
        except Exception as e:  # noqa: BLE001 — memory is a nicety, never a blocker
            logger.warning("notes distillation failed for chat %s: %s", chat_id, e)
    db.update_chat_state(chat_id, notes=notes, msgs_since_notes=since)
    return notes
