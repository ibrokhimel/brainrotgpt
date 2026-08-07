import pytest

import db


@pytest.fixture(autouse=True)
def fresh_db():
    db.init_db(":memory:")
    yield
    db.close()


def test_analytics_and_leaderboard():
    db.log_generation(1, 10, "gym_sigma", "medium", "default", False, 100)
    db.log_generation(1, 10, "gym_sigma", "medium", "default", True, 50)
    db.log_generation(1, 11, "doomer_prophet", "mild", "roast", False, 30)
    lb = db.leaderboard(days=7)
    assert lb[0]["persona"] == "gym_sigma" and lb[0]["n"] == 2
    s = db.stats()
    assert s["total"] == 3 and s["regens"] == 1 and s["users"] == 2


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
