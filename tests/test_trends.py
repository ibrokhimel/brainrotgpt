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

    async def fake_extract(titles):
        return ["67", "tralalero tralala", "chopped"]

    monkeypatch.setattr(trends, "_fetch_reddit_titles", fake_titles)
    monkeypatch.setattr(trends, "_extract_terms", fake_extract)
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
