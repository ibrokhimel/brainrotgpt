"""When the kid acts: reply latency, the sleep window, and the ghost ladder.

Every timing decision is a pure function of (state, now, rng) so three-day
behaviour is testable in milliseconds. Nothing here touches Telegram or the DB.
"""
import datetime as dt

SLEEP_START_H = 1   # inclusive — the kid's phone goes face-down
SLEEP_END_H = 9     # exclusive — back online

# stage -> (min seconds, max seconds) since the kid's last outbound message.
# A ping IS an outbound message, so stage 2 is timed from when stage 1 fired.
STAGE_DELAYS: dict[int, tuple[float, float]] = {
    1: (8 * 60, 25 * 60),
    2: (60 * 60, 3 * 60 * 60),
    3: (6 * 60 * 60, 12 * 60 * 60),
    4: (20 * 60 * 60, 30 * 60 * 60),
    5: (2 * 24 * 60 * 60, 3 * 24 * 60 * 60),
}
FINAL_STAGE = 5

# chattiness scales every ghost delay: clingy chases sooner, chill waits longer.
CHATTINESS_FACTOR = {"chill": 1.8, "normal": 1.0, "clingy": 0.55}


def is_asleep(ts: float) -> bool:
    return SLEEP_START_H <= dt.datetime.fromtimestamp(ts).hour < SLEEP_END_H


def defer_for_sleep(ts: float, *, rng) -> float:
    """Push a timestamp inside the sleep window to just after the kid wakes up."""
    if not is_asleep(ts):
        return ts
    when = dt.datetime.fromtimestamp(ts)
    wake = when.replace(hour=SLEEP_END_H, minute=0, second=0, microsecond=0)
    return wake.timestamp() + rng.uniform(0, 90 * 60)


def next_ping(stage: int, now: float, *, rng, chattiness: str = "normal") -> tuple[float | None, int]:
    """Schedule the next rung of the ladder. Returns (fire_at, new_stage).

    (None, FINAL_STAGE) means the ladder is exhausted — the caller sets gave_up.
    """
    new_stage = stage + 1
    if new_stage > FINAL_STAGE:
        return None, FINAL_STAGE
    lo, hi = STAGE_DELAYS[new_stage]
    factor = CHATTINESS_FACTOR.get(chattiness, 1.0)
    fire_at = now + rng.uniform(lo, hi) * factor
    return defer_for_sleep(fire_at, rng=rng), new_stage


def reply_delay(*, engaged: bool, bond: int, salty: bool, rng) -> float:
    """How long before the kid answers. Speed is itself a social signal."""
    if rng.random() < 0.05:
        base = rng.uniform(3 * 60, 15 * 60)      # genuinely busy
    elif engaged:
        base = rng.uniform(2, 10)
    else:
        base = rng.uniform(20, 90)
    if salty:
        return base * 2.5
    if bond >= 40:
        return base * 0.6
    if bond <= -20:
        return base * 2.5
    return base


def schedule_reply_at(now: float, *, engaged: bool, bond: int, salty: bool, rng) -> float:
    """Absolute time for the reply, respecting the sleep window.

    A 03:00 message is answered after 09:00 — a phone left face-down all night is
    the single clearest signal that there is a person on the other end.
    """
    at = now + reply_delay(engaged=engaged, bond=bond, salty=salty, rng=rng)
    return defer_for_sleep(at, rng=rng)


COLDOPEN_DAILY_CHANCE = 0.33
COLDOPEN_CHANCE_BY_CHATTINESS = {"chill": 0.15, "normal": 0.33, "clingy": 0.6}


def should_cold_open(state: dict, now: float, *, rng) -> bool:
    """Roughly a one-in-three chance per eligible day, and never while asleep."""
    if is_asleep(now):
        return False
    chance = COLDOPEN_CHANCE_BY_CHATTINESS.get(
        state.get("chattiness") or "normal", COLDOPEN_DAILY_CHANCE)
    # The tick runs every 60s, so scale the daily chance down to a per-tick one.
    return rng.random() < chance / (24 * 60)


def cold_open_at(now: float, *, rng) -> float:
    """A plausible near-future moment to text first."""
    return defer_for_sleep(now + rng.uniform(5 * 60, 4 * 3600), rng=rng)
