"""The kid's shared daily life.

A single character with N independent chats is still N clones. One LLM call a day
decides what is going on with the kid today — grounded, sick, new game, exams —
stored globally and injected into every chat. Two people talking to it on the
same day hear about the same thing. That is what makes it one person.
"""
import datetime as dt
import logging
import time

from groq import AsyncGroq

import budget
import config
import db

logger = logging.getLogger("brainrotgpt.life")

_STATE_KEY = "day_state"
_DATE_KEY = "day_date"

_clients = [AsyncGroq(api_key=k) for k in config.GROQ_KEYS]

_PROMPT = (
    "Invent ONE mundane thing going on in a 14-year-old's life today — e.g. their "
    "phone got taken away, they're sick, a test tomorrow, a new game, grounded, "
    "fell out with a friend. Reply with ONE short lowercase clause, max 12 words, "
    "no punctuation at the end, nothing else. Keep it ordinary and school-aged. "
    "Nothing dark, medical, sexual, or involving harm."
)


def current() -> str:
    return db.get_kid_state(_STATE_KEY, "")


def in_school_block(ts: float) -> bool:
    when = dt.datetime.fromtimestamp(ts)
    if when.weekday() >= 5:            # Saturday / Sunday
        return False
    return config.SCHOOL_START_HOUR <= when.hour < config.SCHOOL_END_HOUR


async def _ask(prompt: str) -> str:
    last_err: Exception | None = None
    for client in _clients:
        try:
            resp = await client.chat.completions.create(
                model=config.GROQ_FALLBACK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=1.1,
                max_tokens=40,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err or RuntimeError("no groq client")


async def refresh() -> str:
    """Regenerate today's life state. On failure, yesterday's state carries over."""
    today = dt.datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")
    if not budget.can_spend(time.time()):
        return current()
    try:
        state = (await _ask(_PROMPT)).strip().strip('"').lower()[:120]
        budget.spend(time.time())
        if state:
            db.set_kid_state(_STATE_KEY, state)
            db.set_kid_state(_DATE_KEY, today)
    except Exception as e:  # noqa: BLE001 — never a blocker
        logger.warning("life refresh failed: %s", e)
    return current()
