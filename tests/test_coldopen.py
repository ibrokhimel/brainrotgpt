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


def test_should_cold_open_is_false_while_asleep():
    state = {"chattiness": "normal"}
    assert not ghost.should_cold_open(state, _at(10, 3), rng=random.Random(0))


def test_should_cold_open_fires_sometimes_when_awake():
    state = {"chattiness": "normal"}
    # Per-tick chance is intentionally tiny (~0.33/1440, see ghost.should_cold_open)
    # so it doesn't fire hundreds of times a day. 300 trials undersamples that —
    # expected hits ~0.07, so it's ~93% likely to see zero. Use enough trials for
    # a reliable "fires sometimes" signal without touching the probability itself.
    fired = sum(ghost.should_cold_open(state, _at(10, 14), rng=random.Random(i))
                for i in range(50_000))
    assert 0 < fired < 50_000


def test_cold_open_at_lands_in_waking_hours():
    for i in range(50):
        ts = ghost.cold_open_at(_at(10, 14), rng=random.Random(i))
        assert not ghost.is_asleep(ts)
        assert ts > _at(10, 14)
