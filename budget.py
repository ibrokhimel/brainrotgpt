"""A global daily cap on LLM calls the user did not ask for.

Ghost pings and cold opens scale with the number of chats, and when they exhaust
the Groq key the symptom is "the bot went quiet" rather than a visible error.
Replies to real users are deliberately NOT budgeted — they come out of a
separate, unbudgeted path.
"""
import datetime as dt

import config
import db

_DAY_KEY = "outbound_budget_day"
_COUNT_KEY = "outbound_budget_count"


def _today(now: float) -> str:
    return dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def _spent(now: float) -> int:
    if db.get_kid_state(_DAY_KEY) != _today(now):
        return 0
    try:
        return int(db.get_kid_state(_COUNT_KEY, "0"))
    except ValueError:
        return 0


def remaining(now: float) -> int:
    if config.OUTBOUND_DAILY_BUDGET <= 0:
        return config.OUTBOUND_DAILY_BUDGET or 0
    return max(0, config.OUTBOUND_DAILY_BUDGET - _spent(now))


def can_spend(now: float) -> bool:
    if config.OUTBOUND_DAILY_BUDGET <= 0:
        return True
    return _spent(now) < config.OUTBOUND_DAILY_BUDGET


def spend(now: float, n: int = 1) -> None:
    day = _today(now)
    count = _spent(now) + n
    db.set_kid_state(_DAY_KEY, day)
    db.set_kid_state(_COUNT_KEY, str(count))
