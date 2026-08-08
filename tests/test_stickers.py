import asyncio
import json
import random
import time

import pytest

import config
import db
import stickers


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "stickers.db"))
    yield
    db.close()


class FakeSticker:
    def __init__(self, fid, emoji):
        self.file_id, self.emoji = fid, emoji


class FakeSet:
    def __init__(self, items):
        self.stickers = [FakeSticker(f, e) for f, e in items]


class FakeBot:
    def __init__(self, items, fail=False):
        self._items, self._fail = items, fail
        self.requested = None

    async def get_sticker_set(self, name):
        self.requested = name
        if self._fail:
            raise RuntimeError("pack not found")
        return FakeSet(self._items)


class MultiPackBot:
    """A bot whose packs differ. `packs` maps pack name -> [(file_id, emoji)],
    and a name that isn't in the map raises the way Telegram would."""

    def __init__(self, packs):
        self._packs = packs
        self.requested = []

    async def get_sticker_set(self, name):
        self.requested.append(name)
        if name not in self._packs:
            raise RuntimeError(f"pack {name} not found")
        return FakeSet(self._packs[name])


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


# --- Resolution order: kid_state override beats the .env default -----------

def test_load_prefers_kid_state_pack_over_config():
    config.STICKER_PACK_NAME = "envpack"
    db.set_kid_state(stickers.STICKER_PACK_KEY, json.dumps(["dbpack"]))
    stickers.reset()
    bot = FakeBot([("a", "💀")])
    assert _run(stickers.load(bot)) == 1
    assert bot.requested == "dbpack"


def test_adding_a_pack_keeps_the_env_default_rather_than_replacing_it():
    """The first /stickers add must not silently drop the .env pack — the
    owner asked for one MORE pack, not for a swap."""
    config.STICKER_PACK_NAME = "envpack"
    assert stickers.add_pack("dbpack") is True
    assert stickers.pack_names() == ["envpack", "dbpack"]


def test_a_legacy_bare_pack_name_in_kid_state_still_loads():
    """Deployments upgraded in place have a bare name under this key, written
    before it held a list. It must keep working without a migration step."""
    config.STICKER_PACK_NAME = ""
    db.set_kid_state(stickers.STICKER_PACK_KEY, "legacypack")
    stickers.reset()
    assert stickers.pack_names() == ["legacypack"]
    bot = FakeBot([("a", "💀")])
    assert _run(stickers.load(bot)) == 1
    assert bot.requested == "legacypack"


def test_load_falls_back_to_config_when_kid_state_unset():
    config.STICKER_PACK_NAME = "envpack"
    stickers.reset()
    bot = FakeBot([("a", "💀")])
    assert _run(stickers.load(bot)) == 1
    assert bot.requested == "envpack"


def test_load_disabled_when_kid_state_explicitly_off_even_with_a_config_default():
    config.STICKER_PACK_NAME = "envpack"
    db.set_kid_state(stickers.STICKER_PACK_KEY, "")  # explicit /stickers off
    stickers.reset()
    bot = FakeBot([("a", "💀")])
    assert _run(stickers.load(bot)) == 0
    assert not stickers.enabled()
    assert bot.requested is None  # never even asked Telegram


# --- Several packs at once -------------------------------------------------

def _packs(*names):
    config.STICKER_PACK_NAME = ""
    for n in names:
        stickers.add_pack(n)
    stickers.reset()


def test_two_packs_merge_into_one_emoji_index():
    _packs("one", "two")
    bot = MultiPackBot({"one": [("a", "💀"), ("b", "🗿")], "two": [("c", "💀"), ("d", "🔥")]})
    assert _run(stickers.load(bot)) == 4
    assert bot.requested == ["one", "two"]
    assert set(stickers.available_emoji()) == {"💀", "🗿", "🔥"}
    # the shared emoji draws from both packs, not just the first one loaded
    rng = random.Random(0)
    drawn = {stickers.pick(1, "💀", rng=rng) for _ in range(6)}
    assert drawn == {"a", "c"}


def test_removing_one_pack_leaves_the_others_stickers_available():
    _packs("one", "two")
    bot = MultiPackBot({"one": [("a", "🗿")], "two": [("c", "🔥")]})
    _run(stickers.load(bot))

    assert stickers.remove_pack("one") is True
    assert stickers.pack_names() == ["two"]
    _run(stickers.load(MultiPackBot({"two": [("c", "🔥")]})))
    assert stickers.available_emoji() == ["🔥"]
    assert stickers.pick(1, "🔥", rng=random.Random(0)) == "c"
    assert stickers.pick(1, "🗿", rng=random.Random(0)) is None


def test_removing_a_pack_that_is_not_in_the_list_reports_it():
    _packs("one")
    assert stickers.remove_pack("nope") is False
    assert stickers.pack_names() == ["one"]


def test_a_broken_pack_does_not_stop_a_good_one_loading():
    """The whole point of a list: one bad name must cost you that pack, not
    every pack."""
    _packs("broken", "good")
    bot = MultiPackBot({"good": [("a", "💀"), ("b", "🗿")]})
    assert _run(stickers.load(bot)) == 2
    assert bot.requested == ["broken", "good"]   # it did try the broken one
    assert stickers.enabled()
    assert stickers.pack_count("good") == 2
    assert stickers.pack_count("broken") == 0


def test_adding_a_pack_already_in_the_list_is_a_no_op():
    _packs("one")
    assert stickers.add_pack("one") is False
    assert stickers.pack_names() == ["one"]
    assert stickers.add_pack("two") is True
    assert stickers.pack_names() == ["one", "two"]


def test_clear_packs_removes_every_pack():
    _packs("one", "two")
    _run(stickers.load(MultiPackBot({"one": [("a", "💀")], "two": [("c", "🔥")]})))
    assert stickers.enabled()

    stickers.clear_packs()
    assert stickers.pack_names() == []
    assert _run(stickers.load(MultiPackBot({"one": [("a", "💀")]}))) == 0
    assert not stickers.enabled()


def test_clear_packs_beats_a_config_default():
    """Same contract /stickers off always had: an explicit clear disables the
    feature even when .env still names a pack."""
    stickers.add_pack("one")
    stickers.clear_packs()
    config.STICKER_PACK_NAME = "envpack"
    assert stickers.pack_names() == []


def test_status_reports_per_pack_counts_and_the_total():
    _packs("one", "two")
    _run(stickers.load(MultiPackBot({"one": [("a", "💀"), ("b", "🗿")], "two": [("c", "🔥")]})))
    s = stickers.status()
    assert s["packs"] == [("one", 2), ("two", 1)]
    assert s["count"] == 3
    assert s["emoji_count"] == 3


# --- Sticker-capture flag: /stickers's "send me a sticker" prompt window ---

def test_capture_is_not_pending_by_default():
    assert not stickers.capture_pending()


def test_arm_capture_makes_it_pending():
    stickers.arm_capture()
    assert stickers.capture_pending()


def test_disarm_capture_clears_it():
    stickers.arm_capture()
    stickers.disarm_capture()
    assert not stickers.capture_pending()


def test_capture_pending_is_false_once_the_window_has_elapsed():
    # Written directly rather than via arm_capture() + a real sleep, so the
    # test fails for the right reason if the expiry check is ever removed.
    db.set_kid_state(stickers.AWAITING_STICKER_KEY, str(time.time() - stickers.CAPTURE_WINDOW_S - 1))
    assert not stickers.capture_pending()
