import datetime as dt
import random

import ghost


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


def test_schedule_reply_at_defers_a_3am_message_to_the_morning():
    out = ghost.schedule_reply_at(_at(3), engaged=True, bond=0, salty=False,
                                  rng=random.Random(0))
    assert not ghost.is_asleep(out)
    assert dt.datetime.fromtimestamp(out).hour >= 9
