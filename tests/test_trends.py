import asyncio

import pytest

import brainrot
import db
import trends


@pytest.fixture(autouse=True)
def fresh_db():
    db.init_db(":memory:")
    yield
    db.close()


def test_is_safe_blocks_nsfw_and_slurs():
    assert trends._is_safe("six seven") is True
    assert trends._is_safe("rizz") is True
    assert trends._is_safe("porn star") is False
    assert trends._is_safe("kys grindset") is False


def test_parse_terms_cleans_and_dedups():
    raw = "67, Italian brainrot, 1. tralalero tralala\n- mewing, mewing, this is a whole long sentence that is way too long to be a term"
    out = trends._parse_terms(raw)
    assert "67" in out
    assert "Italian brainrot" in out
    assert "tralalero tralala" in out
    assert out.count("mewing") == 1  # deduped
    # the long sentence is dropped
    assert all(len(t) <= 40 for t in out)


def test_parse_terms_filters_unsafe():
    out = trends._parse_terms("rizz, porn, sigma")
    assert "rizz" in out and "sigma" in out
    assert "porn" not in out


def test_refresh_stores_terms(monkeypatch):
    async def fake_titles(*a, **k):
        return ["67 is everywhere", "the tralalero tralala arc"]

    async def fake_kym(limit=25):
        return []

    async def fake_extract(titles):
        return [{"term": "67", "blurb": ""}, {"term": "tralalero tralala", "blurb": ""}, {"term": "chopped", "blurb": ""}]

    monkeypatch.setattr(trends, "_fetch_reddit_titles", fake_titles)
    monkeypatch.setattr(trends, "_fetch_kym_titles", fake_kym)
    monkeypatch.setattr(trends, "_extract_items", fake_extract)
    db.ban_trend("chopped")  # banned terms must be skipped by the fetcher

    added = asyncio.run(trends.refresh())
    assert added == 2
    live = set(db.trend_terms_for_generation())
    assert {"67", "tralalero tralala"} <= live
    assert "chopped" not in live


def test_refresh_disabled_returns_zero(monkeypatch):
    monkeypatch.setattr(trends.config, "TREND_FETCH_ENABLED", False)
    assert asyncio.run(trends.refresh()) == 0


def test_vocab_sample_blends_live_trends():
    # seed enough distinct live trends that the blend must pull some in
    for t in ["zzlivetrend1", "zzlivetrend2", "zzlivetrend3", "zzlivetrend4"]:
        db.add_trend(t, source="manual")
    sample = brainrot._vocab_sample(k=8, live_k=2)
    assert len(sample) == 8
    assert any(s.startswith("zzlivetrend") for s in sample)  # at least one live term


def test_parse_items_splits_term_and_blurb():
    raw = "67 :: a number people yell for no reason\nchopped :: means ugly or bad"
    items = trends._parse_items(raw)
    assert items[0] == {"term": "67", "blurb": "a number people yell for no reason"}
    assert items[1]["term"] == "chopped"


def test_parse_items_keeps_terms_without_a_blurb():
    items = trends._parse_items("gyatt\nrizz :: charisma")
    assert {"term": "gyatt", "blurb": ""} in items


def test_parse_items_drops_unsafe_terms():
    items = trends._parse_items("porn stuff :: bad\nrizz :: charisma")
    assert [i["term"] for i in items] == ["rizz"]


def test_parse_items_drops_overlong_blurbs():
    items = trends._parse_items("rizz :: " + "x" * 400)
    assert len(items[0]["blurb"]) <= trends.MAX_BLURB


def test_parse_items_dedupes_case_insensitively():
    items = trends._parse_items("Rizz :: a\nrizz :: b")
    assert len(items) == 1


def test_refresh_stores_memes_with_blurbs(tmp_path, monkeypatch):
    import asyncio

    import db
    db.close()
    db.init_db(str(tmp_path / "tr.db"))

    async def fake_titles(subs, per=25, timeout=10.0):
        return ["what does 67 mean"]

    async def fake_kym(limit=25):
        return ["Skibidi Toilet"]

    async def fake_extract(titles):
        return [{"term": "67", "blurb": "a number people yell"}]

    monkeypatch.setattr(trends, "_fetch_reddit_titles", fake_titles)
    monkeypatch.setattr(trends, "_fetch_kym_titles", fake_kym)
    monkeypatch.setattr(trends, "_extract_items", fake_extract)
    added = asyncio.run(trends.refresh())
    assert added == 1
    memes = db.trend_memes_for_generation()
    assert memes[0]["term"] == "67"
    assert memes[0]["blurb"] == "a number people yell"


def test_refresh_survives_a_dead_kym(tmp_path, monkeypatch):
    import asyncio

    import db
    db.close()
    db.init_db(str(tmp_path / "tr2.db"))

    async def fake_titles(subs, per=25, timeout=10.0):
        return ["what does 67 mean"]

    async def dead_kym(limit=25):
        raise RuntimeError("kym down")

    async def fake_extract(titles):
        return [{"term": "67", "blurb": "a number"}]

    monkeypatch.setattr(trends, "_fetch_reddit_titles", fake_titles)
    monkeypatch.setattr(trends, "_fetch_kym_titles", dead_kym)
    monkeypatch.setattr(trends, "_extract_items", fake_extract)
    assert asyncio.run(trends.refresh()) == 1
