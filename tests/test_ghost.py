import datetime as dt
import pathlib
import random

import ghost
import scheduler


def _at(hour, minute=0, day=8):
    return dt.datetime(2026, 8, day, hour, minute).timestamp()


def test_is_asleep_inside_window():
    assert ghost.is_asleep(_at(3))
    assert ghost.is_asleep(_at(1))
    assert not ghost.is_asleep(_at(9))
    assert not ghost.is_asleep(_at(23))


def test_defer_for_sleep_moves_a_3am_time_past_9am():
    out = ghost.defer_for_sleep(_at(3), rng=random.Random(0))
    hour = dt.datetime.fromtimestamp(out).hour
    assert 9 <= hour <= 10
    assert out > _at(3)


def test_defer_for_sleep_leaves_waking_hours_untouched():
    ts = _at(14)
    assert ghost.defer_for_sleep(ts, rng=random.Random(0)) == ts


def test_defer_for_sleep_on_a_late_night_rolls_to_next_morning():
    # 01:30 is already inside the window on the same calendar day
    out = ghost.defer_for_sleep(_at(1, 30), rng=random.Random(0))
    assert dt.datetime.fromtimestamp(out).day == 8
    assert dt.datetime.fromtimestamp(out).hour >= 9


def test_next_ping_advances_the_stage():
    fire_at, stage = ghost.next_ping(0, _at(12), rng=random.Random(0))
    assert stage == 1
    assert fire_at is not None


def test_next_ping_delay_grows_with_stage():
    rng = random.Random(0)
    now = _at(12)
    d1 = ghost.next_ping(0, now, rng=rng)[0] - now
    d4 = ghost.next_ping(3, now, rng=rng)[0] - now
    assert d4 > d1


def test_next_ping_stage_five_is_terminal():
    assert ghost.next_ping(5, _at(12), rng=random.Random(0)) == (None, 5)


def test_next_ping_defers_out_of_the_sleep_window():
    # a stage-1 ping fired at 00:50 would land ~01:10, inside the window
    fire_at, _ = ghost.next_ping(0, _at(0, 50), rng=random.Random(0))
    assert not ghost.is_asleep(fire_at)


def test_clingy_pings_sooner_than_chill():
    now = _at(12)
    rng_a, rng_b = random.Random(3), random.Random(3)
    clingy = ghost.next_ping(0, now, rng=rng_a, chattiness="clingy")[0]
    chill = ghost.next_ping(0, now, rng=rng_b, chattiness="chill")[0]
    assert clingy < chill


def test_reply_delay_engaged_is_faster_than_cold():
    rng_a, rng_b = random.Random(1), random.Random(1)
    engaged = ghost.reply_delay(engaged=True, bond=0, salty=False, rng=rng_a)
    cold = ghost.reply_delay(engaged=False, bond=0, salty=False, rng=rng_b)
    assert engaged < cold


def test_high_bond_replies_faster_than_low_bond():
    rng_a, rng_b = random.Random(5), random.Random(5)
    warm = ghost.reply_delay(engaged=True, bond=80, salty=False, rng=rng_a)
    cold = ghost.reply_delay(engaged=True, bond=-50, salty=False, rng=rng_b)
    assert warm < cold


def test_salty_replies_slowest():
    rng_a, rng_b = random.Random(5), random.Random(5)
    salty = ghost.reply_delay(engaged=True, bond=80, salty=True, rng=rng_a)
    normal = ghost.reply_delay(engaged=True, bond=80, salty=False, rng=rng_b)
    assert salty > normal


# --- Fix 2: engaged replies at conversation speed ---------------------------

class _FixedRNG:
    """A stub whose uniform() picks an end of the range, so the branch bounds
    are testable directly rather than through a seeded distribution."""

    def __init__(self, end="lo", r=0.5):
        self._end = end
        self._r = r        # 0.5 keeps us out of the 5% genuinely-busy branch

    def random(self):
        return self._r

    def uniform(self, lo, hi):
        return lo if self._end == "lo" else hi

    def choice(self, seq):
        return seq[0]


def test_an_engaged_reply_lands_at_conversation_speed():
    """2-10s was slow enough that a live back-and-forth still read as a queue:
    every turn, the other person waits out a pause that a real teenager holding
    their phone would not take."""
    fast = ghost.reply_delay(engaged=True, bond=0, salty=False, rng=_FixedRNG("lo"))
    slow = ghost.reply_delay(engaged=True, bond=0, salty=False, rng=_FixedRNG("hi"))
    assert 1 <= fast <= 2
    assert 5 <= slow <= 7


def test_a_cold_reply_is_seconds_not_a_minute_and_a_half():
    """Revised: this previously pinned 20-90s as deliberate. It is not -- "a
    moment before answering" is a couple of seconds, and the top of that range
    on its own overshot the fast-path ceiling, so every slow cold reply fell
    back to the 60s tick and cost up to 150s door to door."""
    fast = ghost.reply_delay(engaged=False, bond=0, salty=False, rng=_FixedRNG("lo"))
    slow = ghost.reply_delay(engaged=False, bond=0, salty=False, rng=_FixedRNG("hi"))
    assert (fast, slow) == (4, 15)


def test_the_genuinely_busy_branch_is_texture_not_an_outage():
    """The mechanism is kept -- occasionally the kid IS doing something else --
    but 3-15 minutes is not texture, it is indistinguishable from the bot being
    broken."""
    busy = ghost.reply_delay(engaged=True, bond=0, salty=False, rng=_FixedRNG("hi", r=0.0))
    assert 40 <= busy <= 90
    assert ghost.BUSY_CHANCE <= 0.02


def test_no_reply_delay_can_land_outside_the_fast_path_ceiling():
    """The invariant that broke silently. Nothing connected reply_delay's
    ranges to scheduler.FAST_PATH_MAX_S, so the cold branch overshot the
    ceiling on its own and the fast path almost never fired. Live: "morning"
    computed 65.3s, over the 55s ceiling, fell through to the tick, and was
    still pending 13.4s overdue at +78s.

    Every reply-path branch, every multiplier, and both composed.
    """
    worst = 0.0
    for seed in range(400):
        rng = random.Random(seed)
        for engaged in (True, False):
            for salty in (False, True):
                for bond in (-50, 0, 80):
                    for in_school in (False, True):
                        worst = max(worst, ghost.reply_delay(
                            engaged=engaged, bond=bond, salty=salty, rng=rng,
                            in_school=in_school))
    assert worst <= scheduler.FAST_PATH_MAX_S


def test_the_fast_path_ceiling_is_derived_from_the_worst_case_delay():
    """A hand-picked ceiling is what let the two drift apart in the first
    place. FAST_PATH_MAX_S is now computed from the slowest delay ghost can
    return, so widening a range cannot silently strand replies on the tick."""
    assert scheduler.FAST_PATH_MAX_S >= ghost.MAX_REPLY_DELAY_S
    assert ghost.MAX_REPLY_DELAY_S == (
        ghost.BUSY_RANGE[1] * ghost.SCHOOL_DELAY_FACTOR * ghost.SLOW_FACTOR)


def test_the_engaged_window_covers_several_minutes_of_conversation():
    """Someone who texted you four minutes ago is still holding their phone.
    120s classified them as a cold open and charged them the 20-90s delay."""
    assert 5 * 60 <= ghost.ENGAGED_WINDOW_S <= 15 * 60


def test_schedule_reply_at_defers_a_3am_message_to_the_morning():
    out = ghost.schedule_reply_at(_at(3), engaged=True, bond=0, salty=False,
                                  rng=random.Random(0))
    assert not ghost.is_asleep(out)
    assert dt.datetime.fromtimestamp(out).hour >= 9


# --- spec 1/4: the weekday school block ------------------------------------

def test_school_block_slows_the_reply():
    """Spec 4: on weekdays the school block slows and shortens replies rather
    than deferring them entirely -- a kid texts in class, just badly."""
    rng_a, rng_b = random.Random(11), random.Random(11)
    in_class = ghost.reply_delay(engaged=True, bond=0, salty=False, rng=rng_a, in_school=True)
    free = ghost.reply_delay(engaged=True, bond=0, salty=False, rng=rng_b, in_school=False)
    assert in_class > free


def test_school_block_composes_with_the_salty_multiplier():
    """Both slowdowns apply -- school is not a replacement for salty."""
    rng_a, rng_b = random.Random(11), random.Random(11)
    both = ghost.reply_delay(engaged=True, bond=0, salty=True, rng=rng_a, in_school=True)
    salty_only = ghost.reply_delay(engaged=True, bond=0, salty=True, rng=rng_b, in_school=False)
    assert both > salty_only


def test_schedule_reply_at_passes_the_school_block_through():
    rng_a, rng_b = random.Random(11), random.Random(11)
    at = _at(12)
    in_class = ghost.schedule_reply_at(at, engaged=True, bond=0, salty=False,
                                       rng=rng_a, in_school=True)
    free = ghost.schedule_reply_at(at, engaged=True, bond=0, salty=False,
                                   rng=rng_b, in_school=False)
    assert in_class > free


def test_ghost_stays_pure_and_does_not_import_life_or_db():
    """ghost.py is a pure function of (state, now, rng). Importing life would
    drag db and a Groq client in behind it and make every timing test need a
    database -- which is why in_school arrives as a keyword argument instead."""
    src = pathlib.Path(ghost.__file__).read_text()
    assert "import life" not in src
    assert "import db" not in src
