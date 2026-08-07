import datetime as dt
import random

import db
import ghost


def _at(day, hour=12):
    return dt.datetime(2026, 8, day, hour).timestamp()


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "co.db"))


DAY = 24 * 3600


def test_candidates_require_bond_recency_and_quiet(tmp_path):
    _fresh(tmp_path)
    now = _at(10)
    # eligible
    db.update_chat_state(1, bond=20, last_user_ts=now - 2 * DAY, last_kid_ts=now - 2 * DAY)
    # bond too low
    db.update_chat_state(2, bond=1, last_user_ts=now - 2 * DAY, last_kid_ts=now - 2 * DAY)
    # inactive too long
    db.update_chat_state(3, bond=20, last_user_ts=now - 30 * DAY, last_kid_ts=now - 30 * DAY)
    # kid spoke too recently
    db.update_chat_state(4, bond=20, last_user_ts=now - 2 * DAY, last_kid_ts=now - 600)
    ids = [c["chat_id"] for c in db.coldopen_candidates(
        now, min_bond=10, active_within_s=7 * DAY, quiet_for_s=18 * 3600)]
    assert ids == [1]


def test_candidates_exclude_muted_gaveup_and_already_scheduled(tmp_path):
    _fresh(tmp_path)
    now = _at(10)
    for cid in (1, 2, 3):
        db.update_chat_state(cid, bond=20, last_user_ts=now - 2 * DAY, last_kid_ts=now - 2 * DAY)
    db.update_chat_state(1, muted=1)
    db.update_chat_state(2, gave_up=1)
    db.update_chat_state(3, next_action_at=now + 500)
    assert db.coldopen_candidates(now, min_bond=10, active_within_s=7 * DAY,
                                  quiet_for_s=18 * 3600) == []


class _FixedRng:
    """rng stub with a fixed random() — makes the probability gate deterministic."""
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value

    def uniform(self, a, b):
        return a + (b - a) * self.value


def test_should_cold_open_is_false_while_asleep():
    # Fixed at 0.0 (a roll that would otherwise always fire) proves the sleep
    # guard wins regardless of the roll — stronger than a real rng, which could
    # pass here for the wrong reason (rolling above threshold, not sleep).
    state = {"chattiness": "normal"}
    assert not ghost.should_cold_open(state, _at(10, 3), rng=_FixedRng(0.0))


def test_should_cold_open_fires_when_the_roll_is_below_the_threshold():
    assert ghost.should_cold_open({"chattiness": "normal"}, _at(10, 14), rng=_FixedRng(0.0))


def test_should_cold_open_does_not_fire_when_the_roll_is_above_the_threshold():
    assert not ghost.should_cold_open({"chattiness": "normal"}, _at(10, 14), rng=_FixedRng(0.5))


def test_clingy_has_a_higher_cold_open_threshold_than_chill():
    # a roll that fires for clingy must not fire for chill
    roll = _FixedRng(ghost.COLDOPEN_CHANCE_BY_CHATTINESS["chill"] / (24 * 60) * 1.5)
    assert ghost.should_cold_open({"chattiness": "clingy"}, _at(10, 14), rng=roll)
    assert not ghost.should_cold_open({"chattiness": "chill"}, _at(10, 14), rng=roll)


def test_cold_open_at_lands_in_waking_hours():
    for i in range(50):
        ts = ghost.cold_open_at(_at(10, 14), rng=random.Random(i))
        assert not ghost.is_asleep(ts)
        assert ts > _at(10, 14)
