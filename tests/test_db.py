import pytest

import db


@pytest.fixture(autouse=True)
def fresh_db():
    db.init_db(":memory:")
    yield
    db.close()


def test_default_settings():
    s = db.get_settings(1)
    assert s["persona"] == "random"
    assert s["candidates"] == 1


def test_set_setting_persists():
    db.set_setting(1, "persona", "gym_sigma")
    assert db.get_settings(1)["persona"] == "gym_sigma"


def test_candidates_clamped():
    s = db.set_setting(1, "candidates", 999)
    assert 1 <= s["candidates"] <= db.config.MAX_CANDIDATES


def test_bad_intensity_rejected():
    with pytest.raises(ValueError):
        db.set_setting(1, "intensity", "nope")


def test_favorites_roundtrip():
    fid = db.add_favorite(1, 10, "banger reply", "doomer_prophet")
    favs = db.list_favorites(1)
    assert len(favs) == 1 and favs[0]["id"] == fid
    assert db.delete_favorite(fid, 1) is True
    assert db.list_favorites(1) == []


def test_analytics_and_leaderboard():
    db.log_generation(1, 10, "gym_sigma", "medium", "default", False, 100)
    db.log_generation(1, 10, "gym_sigma", "medium", "default", True, 50)
    db.log_generation(1, 11, "doomer_prophet", "mild", "roast", False, 30)
    lb = db.leaderboard(days=7)
    assert lb[0]["persona"] == "gym_sigma" and lb[0]["n"] == 2
    s = db.stats()
    assert s["total"] == 3 and s["regens"] == 1 and s["users"] == 2


def test_last_result_roundtrip():
    db.set_last_result(1, "the reply", "conspiracy")
    last = db.get_last_result(1)
    assert last["text"] == "the reply" and last["persona"] == "conspiracy"


def test_subscriptions():
    db.set_subscription(1, 9, True)
    assert any(s["chat_id"] == 1 for s in db.list_subscriptions())
    db.remove_subscription(1)
    assert db.list_subscriptions() == []
