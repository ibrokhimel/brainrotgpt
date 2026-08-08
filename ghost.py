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

# Spec §4: on weekdays the school block slows replies rather than deferring
# them — a kid texts in class, just badly. `in_school` arrives as a keyword
# argument rather than being read here, because importing `life` would drag
# `db` and a Groq client in behind it and cost this module its purity.
SCHOOL_DELAY_FACTOR = 2.0

# How long after the kid's own last message a chat still counts as a live
# back-and-forth. Two minutes was far too literal: someone who texted you four
# minutes ago is still holding their phone, and treating them as first contact
# charged an active conversation the 20-90s cold delay every few turns.
# bot.py reads this rather than owning the number — the whole point of this
# module is that reply timing lives in one pure place.
ENGAGED_WINDOW_S = 8 * 60

# The three reply branches, as named constants so scheduler.py can derive its
# fast-path ceiling from them instead of picking a number by hand.
ENGAGED_RANGE = (1.5, 6)     # mid-conversation
COLD_RANGE = (4, 15)         # first message after a silence
BUSY_RANGE = (40, 90)        # the kid is genuinely doing something else
BUSY_CHANCE = 0.02

SLOW_FACTOR = 2.5            # salty, or a bond down in the annoyed register
FAST_FACTOR = 0.6            # a high bond

# The slowest reply_delay can possibly come back: the busy branch, in school,
# and salty, all composed. scheduler.FAST_PATH_MAX_S is derived from this so
# the two can never be chosen independently again — that drift is exactly how
# the fast path stopped firing. Note this is the pre-sleep-window figure;
# schedule_reply_at can still defer a 3am message to 9am, and that deferral is
# supposed to land on the tick.
MAX_REPLY_DELAY_S = BUSY_RANGE[1] * SCHOOL_DELAY_FACTOR * SLOW_FACTOR


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


def reply_delay(*, engaged: bool, bond: int, salty: bool, rng,
                in_school: bool = False) -> float:
    """How long before the kid answers. Speed is itself a social signal."""
    if rng.random() < BUSY_CHANCE:
        # Texture: occasionally the kid really is doing something else. This
        # was a 5% chance of 3-15 minutes, which is not texture — a fifteen
        # minute silence is indistinguishable from the bot being broken. The
        # mechanism stays; the magnitude does not.
        base = rng.uniform(*BUSY_RANGE)
    elif engaged:
        # Mid-conversation. Paired with the in-process fast path in scheduler.py
        # this is the whole difference between a back-and-forth and a queue —
        # on the 60s tick alone the number here barely mattered.
        base = rng.uniform(*ENGAGED_RANGE)
    else:
        # First contact after a silence. Still the slowest ordinary branch, so
        # the pause still reads as "was doing something else" — but a moment is
        # a couple of seconds, not the minute and a half this used to be.
        base = rng.uniform(*COLD_RANGE)
    if in_school:
        base *= SCHOOL_DELAY_FACTOR   # phone under the desk; it composes with the rest
    if salty:
        return base * SLOW_FACTOR
    if bond >= 40:
        return base * FAST_FACTOR
    if bond <= -20:
        return base * SLOW_FACTOR
    return base


def schedule_reply_at(now: float, *, engaged: bool, bond: int, salty: bool, rng,
                      in_school: bool = False) -> float:
    """Absolute time for the reply, respecting the sleep window.

    A 03:00 message is answered after 09:00 — a phone left face-down all night is
    the single clearest signal that there is a person on the other end.
    """
    at = now + reply_delay(engaged=engaged, bond=bond, salty=salty, rng=rng,
                           in_school=in_school)
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
