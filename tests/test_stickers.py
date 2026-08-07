import asyncio
import random

import config
import stickers


class FakeSticker:
    def __init__(self, fid, emoji):
        self.file_id, self.emoji = fid, emoji


class FakeSet:
    def __init__(self, items):
        self.stickers = [FakeSticker(f, e) for f, e in items]


class FakeBot:
    def __init__(self, items, fail=False):
        self._items, self._fail = items, fail

    async def get_sticker_set(self, name):
        if self._fail:
            raise RuntimeError("pack not found")
        return FakeSet(self._items)


def _run(coro):
    return asyncio.run(coro)


def _load(items, fail=False):
    stickers.reset()
    config.STICKER_PACK_NAME = "testpack"
    return _run(stickers.load(FakeBot(items, fail=fail)))


def test_load_indexes_by_emoji():
    assert _load([("a", "💀"), ("b", "💀"), ("c", "🗿")]) == 3
    assert set(stickers.available_emoji()) == {"💀", "🗿"}
    assert stickers.enabled()


def test_pick_returns_a_file_id_for_a_known_emoji():
    _load([("a", "💀"), ("c", "🗿")])
    assert stickers.pick(1, "💀", rng=random.Random(0)) == "a"


def test_pick_returns_none_for_an_unknown_emoji():
    _load([("a", "💀")])
    assert stickers.pick(1, "🦄", rng=random.Random(0)) is None


def test_no_repeat_guard_avoids_the_recent_sticker():
    _load([("a", "💀"), ("b", "💀")])
    rng = random.Random(0)
    first = stickers.pick(1, "💀", rng=rng)
    second = stickers.pick(1, "💀", rng=rng)
    assert first != second


def test_no_repeat_guard_is_per_chat():
    _load([("a", "💀"), ("b", "💀")])
    rng = random.Random(0)
    first = stickers.pick(1, "💀", rng=rng)
    assert stickers.pick(2, "💀", rng=rng) in {"a", "b"}
    assert first in {"a", "b"}


def test_pick_still_returns_something_when_all_are_recent():
    _load([("a", "💀")])
    rng = random.Random(0)
    assert stickers.pick(1, "💀", rng=rng) == "a"
    assert stickers.pick(1, "💀", rng=rng) == "a"   # exhausted, reuse rather than fail


def test_pick_random_returns_a_pack_member():
    _load([("a", "💀"), ("c", "🗿")])
    assert stickers.pick_random(1, rng=random.Random(0)) in {"a", "c"}


def test_failed_load_disables_stickers_without_raising():
    assert _load([], fail=True) == 0
    assert not stickers.enabled()
    assert stickers.pick(1, "💀", rng=random.Random(0)) is None
    assert stickers.available_emoji() == []


def test_empty_pack_name_disables_the_feature():
    stickers.reset()
    config.STICKER_PACK_NAME = ""
    assert _run(stickers.load(FakeBot([("a", "💀")]))) == 0
    assert not stickers.enabled()
