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
    assert s["length"] == db.config.DEFAULT_LENGTH


def test_length_persists():
    db.set_setting(1, "length", "max")
    assert db.get_settings(1)["length"] == "max"


def test_bad_length_rejected():
    with pytest.raises(ValueError):
        db.set_setting(1, "length", "nope")


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


# --- trends ---------------------------------------------------------------

def test_trend_add_and_list():
    assert db.add_trend("67", source="manual") is True
    assert db.add_trend("67", source="manual") is False  # dup
    assert db.add_trend("SIX seven", source="auto") is True
    terms = {t["term"] for t in db.list_trends()}
    assert "67" in terms and "SIX seven" in terms


def test_trend_dedup_is_case_insensitive():
    db.add_trend("Skibidi", source="auto")
    assert db.add_trend("skibidi", source="auto") is False
    assert db.count_trends() == 1


def test_trend_ban_hides_and_blocks_auto():
    db.add_trend("chopped", source="auto")
    db.ban_trend("chopped")
    assert "chopped" not in {t["term"] for t in db.list_trends()}          # hidden
    assert "chopped" in db.banned_trend_terms()
    assert db.add_trend("chopped", source="auto") is False                  # blocked
    # a manual add un-bans it
    assert db.add_trend("chopped", source="manual") is True
    assert "chopped" in {t["term"] for t in db.list_trends()}


def test_trend_remove_and_gen_list():
    db.add_trend("mewing", source="manual")
    assert "mewing" in db.trend_terms_for_generation()
    assert db.remove_trend("mewing") is True
    assert db.remove_trend("mewing") is False
    assert "mewing" not in db.trend_terms_for_generation()
