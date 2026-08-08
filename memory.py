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
    "Below are messages ONE person sent to a teenager. Every line is that person's "
    "own words. The teenager's replies are NOT included.\n"
    "List what this person has stated about themselves, ONE SHORT FACT PER LINE. "
    "Max 12 words per line. No bullets, no numbering, no headings, no commentary.\n"
    "Write EVERY line in the THIRD PERSON about them: \"their name is walter\", "
    "\"works in IT and hates it\". Never write \"I\" or \"my\" and never quote them — "
    "you are describing them to someone else, not repeating their words.\n"
    "Record anything a friend would remember: who they are, what they do, what they "
    "like or hate, what they keep complaining about, plans they mentioned, running "
    "jokes, how they talk. Small things count. Prefer several small facts over one "
    "long one.\n"
    "Record ONLY what the messages below actually say. Never invent a habit, a "
    "routine, a mood or a personality, and never turn an emoji or a sticker into a "
    "character trait. Anything the TEENAGER said, claimed, asked about, or is going "
    "through — their school, their chores, their phone, their day — is not a fact "
    "about this person, even if it came up in the conversation.\n"
    "Include facts from EXISTING NOTES that still hold, and drop any that these "
    "messages contradict. If the messages say nothing durable about the person, "
    "reply with the single word NONE.\n\n"
    "EXISTING NOTES:\n{notes}\n\nMESSAGES FROM THEM:\n{chat}"
)

# The model is told not to use bullets; it will anyway.
_BULLET = re.compile(r"^\s*(?:[-*•·–]|\d+[.)])\s*")


def transcript(chat_id: int, limit: int = 40) -> str:
    """Render the recent window for the prompt. 'them' is the user, 'me' the kid.

    Both sides, because generating a reply needs to see the conversation. NOT
    for the extractor — see `user_transcript`.
    """
    rows = db.recent_messages(chat_id, limit=limit)
    return "\n".join(
        f"{'me' if r['role'] == 'kid' else 'them'}: {r['text']}" for r in rows
    )


def user_transcript(chat_id: int, limit: int = 40) -> str:
    """Only the other person's own lines — what the extractor is allowed to read.

    The first version handed the extractor the full rendered `transcript`, and
    since the kid sends two or three messages per turn against the user's one,
    that window was roughly 80% bot output. Everything it stored was therefore a
    summary of the kid's own messages filed as a fact about the person: the
    kid's sigma mood became "they are disciplined", the kid's day-state became
    "they have a laundry task", a 💪 the kid sent became "they are strong". Those
    facts then came back as WHAT YOU KNOW ABOUT THEM and the kid acted on them,
    so the hallucinations compounded on themselves every cycle.

    Filtering at the source is the fix. Instructing the model to ignore the
    kid's lines would leave them one bad inference away from being recorded.
    """
    rows = db.recent_messages(chat_id, limit=limit, role="user")
    return "\n".join(f"them: {r['text']}" for r in rows)


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
    chat = user_transcript(chat_id)
    if not chat:
        # Nothing from them at all — a cold open they never answered. There is
        # nothing to know, and asking anyway is how the kid's own monologue used
        # to become facts about them. Same shape as a NONE: try again shortly.
        db.update_chat_state(chat_id, notes=notes,
                             msgs_since_notes=max(0, NOTES_EVERY - NONE_BACKOFF))
        return notes
    if budget.can_spend(time.time()):
        try:
            raw = await _ask(_PROMPT.format(notes=old or "(none)", chat=chat))
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
