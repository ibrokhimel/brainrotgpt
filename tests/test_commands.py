import asyncio
import time

import pytest

import commands
import config
import db
import stickers

OWNER = 111
NOT_OWNER = 222


@pytest.fixture(autouse=True)
def fresh_state(tmp_path):
    db.init_db(str(tmp_path / "commands.db"))
    stickers.reset()
    config.STICKER_PACK_NAME = ""
    yield
    db.close()


def _run(coro):
    return asyncio.run(coro)


def _owner_only(monkeypatch):
    monkeypatch.setattr(config, "OWNER_IDS", {OWNER})


# --- fakes, just enough surface for cmd_stickers ---------------------------

class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, from_user=None, sticker=None):
        self.from_user = from_user
        self.sticker = sticker
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, uid, sticker=None):
        self.effective_user = _FakeUser(uid)
        self.message = _FakeMessage(from_user=_FakeUser(uid), sticker=sticker)


class FakeStickerObj:
    def __init__(self, set_name="pack1", emoji="💀"):
        self.set_name = set_name
        self.emoji = emoji
        self.file_id = "sid1"


class FakeSticker:
    def __init__(self, fid, emoji):
        self.file_id, self.emoji = fid, emoji


class FakeSet:
    def __init__(self, items):
        self.stickers = [FakeSticker(f, e) for f, e in items]


class FakeBot:
    def __init__(self, items=None, fail=False):
        self._items = items if items is not None else [("a", "💀")]
        self._fail = fail
        self.requested = None

    async def get_sticker_set(self, name):
        self.requested = name
        if self._fail:
            raise RuntimeError("pack not found")
        return FakeSet(self._items)


class MultiPackBot:
    """Distinct packs by name; an unknown name raises the way Telegram would."""

    def __init__(self, packs):
        self._packs = packs
        self.requested = []

    async def get_sticker_set(self, name):
        self.requested.append(name)
        if name not in self._packs:
            raise RuntimeError(f"pack {name} not found")
        return FakeSet(self._packs[name])


class _FakeContext:
    def __init__(self, args=None, bot_obj=None):
        self.args = args or []
        self.bot = bot_obj or FakeBot()


# --- /stickers --------------------------------------------------------------

def test_non_owner_is_refused_and_the_pack_is_unchanged(monkeypatch):
    _owner_only(monkeypatch)
    db.set_kid_state(stickers.STICKER_PACK_KEY, "original")
    update = _FakeUpdate(NOT_OWNER)
    ctx = _FakeContext(args=["newpack"])

    _run(commands.cmd_stickers(update, ctx))

    assert "owner only" in update.message.replies[0]
    assert db.get_kid_state(stickers.STICKER_PACK_KEY) == "original"
    assert ctx.bot.requested is None  # never touched Telegram


def test_setting_a_valid_pack_stores_it_and_reloads(monkeypatch):
    _owner_only(monkeypatch)
    update = _FakeUpdate(OWNER)
    ctx = _FakeContext(args=["mypack"], bot_obj=FakeBot([("a", "💀"), ("b", "🗿")]))

    _run(commands.cmd_stickers(update, ctx))

    assert stickers.pack_names() == ["mypack"]
    assert ctx.bot.requested == "mypack"
    assert stickers.enabled()
    assert "2" in update.message.replies[0]


def test_a_t_me_addstickers_url_is_accepted_and_the_bare_name_extracted(monkeypatch):
    _owner_only(monkeypatch)
    update = _FakeUpdate(OWNER)
    ctx = _FakeContext(
        args=["https://t.me/addstickers/mypack"], bot_obj=FakeBot([("a", "💀")])
    )

    _run(commands.cmd_stickers(update, ctx))

    assert stickers.pack_names() == ["mypack"]
    assert ctx.bot.requested == "mypack"


def test_off_disables_stickers(monkeypatch):
    _owner_only(monkeypatch)
    stickers.add_pack("mypack")
    _run(stickers.load(FakeBot([("a", "💀")])))
    assert stickers.enabled()

    update = _FakeUpdate(OWNER)
    ctx = _FakeContext(args=["off"])
    _run(commands.cmd_stickers(update, ctx))

    assert not stickers.enabled()
    assert stickers.pack_names() == []


def test_a_pack_that_fails_to_load_reports_failure_and_leaves_stickers_disabled(monkeypatch):
    _owner_only(monkeypatch)
    update = _FakeUpdate(OWNER)
    ctx = _FakeContext(args=["badpack"], bot_obj=FakeBot(fail=True))

    _run(commands.cmd_stickers(update, ctx))  # must not raise

    assert not stickers.enabled()
    reply = update.message.replies[0].lower()
    assert "couldn't" in reply or "fail" in reply


def test_report_shows_current_pack_count_and_emoji(monkeypatch):
    _owner_only(monkeypatch)
    stickers.add_pack("mypack")
    _run(stickers.load(FakeBot([("a", "💀"), ("b", "💀"), ("c", "🗿")])))

    update = _FakeUpdate(OWNER)
    ctx = _FakeContext(args=[])
    _run(commands.cmd_stickers(update, ctx))

    reply = update.message.replies[0]
    assert "mypack" in reply
    assert "3" in reply  # sticker count
    assert "2" in reply  # distinct emoji count


def test_report_when_no_pack_is_configured(monkeypatch):
    _owner_only(monkeypatch)
    update = _FakeUpdate(OWNER)
    ctx = _FakeContext(args=[])

    _run(commands.cmd_stickers(update, ctx))

    reply = update.message.replies[0].lower()
    assert "off" in reply


def test_no_arg_report_arms_the_capture_and_prompts_for_a_sticker(monkeypatch):
    _owner_only(monkeypatch)
    update = _FakeUpdate(OWNER)
    ctx = _FakeContext(args=[])

    _run(commands.cmd_stickers(update, ctx))

    assert stickers.capture_pending()
    assert "sticker" in update.message.replies[0].lower()


# --- /stickers add · remove: several packs at once --------------------------

def _cmd(monkeypatch, args, bot_obj):
    _owner_only(monkeypatch)
    update = _FakeUpdate(OWNER)
    ctx = _FakeContext(args=args, bot_obj=bot_obj)
    _run(commands.cmd_stickers(update, ctx))
    return update.message.replies[0]


def test_add_appends_to_the_list_rather_than_replacing_it(monkeypatch):
    stickers.add_pack("one")
    bot = MultiPackBot({"one": [("a", "💀")], "two": [("c", "🔥")]})

    reply = _cmd(monkeypatch, ["add", "two"], bot)

    assert stickers.pack_names() == ["one", "two"]
    assert set(stickers.available_emoji()) == {"💀", "🔥"}
    assert "two" in reply


def test_add_accepts_a_t_me_link_too(monkeypatch):
    _cmd(monkeypatch, ["add", "https://t.me/addstickers/two"],
         MultiPackBot({"two": [("c", "🔥")]}))
    assert stickers.pack_names() == ["two"]


def test_adding_a_pack_already_in_the_list_says_so_instead_of_duplicating(monkeypatch):
    stickers.add_pack("one")
    _run(stickers.load(MultiPackBot({"one": [("a", "💀")]})))

    reply = _cmd(monkeypatch, ["add", "one"], MultiPackBot({"one": [("a", "💀")]}))

    assert stickers.pack_names() == ["one"]
    assert "already" in reply.lower()


def test_a_pack_that_fails_to_load_is_not_left_in_the_list(monkeypatch):
    """A name that 404s would otherwise sit in the list forever, costing an
    API call and a warning on every reload."""
    stickers.add_pack("one")
    bot = MultiPackBot({"one": [("a", "💀")]})

    reply = _cmd(monkeypatch, ["add", "nope"], bot)

    assert stickers.pack_names() == ["one"]
    assert stickers.enabled()          # the good pack survived the bad add
    assert "couldn't" in reply.lower()


def test_remove_drops_one_pack_and_keeps_the_rest(monkeypatch):
    stickers.add_pack("one")
    stickers.add_pack("two")
    bot = MultiPackBot({"one": [("a", "💀")], "two": [("c", "🔥")]})
    _run(stickers.load(bot))

    reply = _cmd(monkeypatch, ["remove", "one"], bot)

    assert stickers.pack_names() == ["two"]
    assert stickers.available_emoji() == ["🔥"]   # reloaded without the removed pack
    assert "one" in reply


def test_removing_a_pack_that_is_not_configured_says_so(monkeypatch):
    stickers.add_pack("one")
    reply = _cmd(monkeypatch, ["remove", "nope"], MultiPackBot({"one": [("a", "💀")]}))
    assert stickers.pack_names() == ["one"]
    assert "nope" in reply


def test_off_clears_every_pack(monkeypatch):
    stickers.add_pack("one")
    stickers.add_pack("two")
    bot = MultiPackBot({"one": [("a", "💀")], "two": [("c", "🔥")]})
    _run(stickers.load(bot))

    _cmd(monkeypatch, ["off"], bot)

    assert stickers.pack_names() == []
    assert not stickers.enabled()


def test_the_report_lists_every_pack_with_its_own_count(monkeypatch):
    stickers.add_pack("one")
    stickers.add_pack("two")
    _run(stickers.load(MultiPackBot({"one": [("a", "💀"), ("b", "🗿")], "two": [("c", "🔥")]})))

    reply = _cmd(monkeypatch, [], MultiPackBot({}))

    assert "one" in reply and "two" in reply
    assert "3" in reply                       # merged total
    assert stickers.capture_pending()         # the add-by-sticker flow stays armed


def test_capture_adds_to_the_existing_packs_rather_than_replacing_them(monkeypatch):
    _owner_only(monkeypatch)
    stickers.add_pack("one")
    stickers.arm_capture()
    update = _FakeUpdate(OWNER, sticker=FakeStickerObj(set_name="two"))
    ctx = _FakeContext(bot_obj=MultiPackBot({"one": [("a", "💀")], "two": [("c", "🔥")]}))

    handled = _run(commands.try_capture_sticker(update, ctx))

    assert handled is True
    assert stickers.pack_names() == ["one", "two"]
    assert set(stickers.available_emoji()) == {"💀", "🔥"}
    assert not stickers.capture_pending()


def test_capturing_a_pack_already_in_the_list_says_so_and_disarms(monkeypatch):
    _owner_only(monkeypatch)
    stickers.add_pack("one")
    _run(stickers.load(MultiPackBot({"one": [("a", "💀")]})))
    stickers.arm_capture()
    update = _FakeUpdate(OWNER, sticker=FakeStickerObj(set_name="one"))
    ctx = _FakeContext(bot_obj=MultiPackBot({"one": [("a", "💀")]}))

    handled = _run(commands.try_capture_sticker(update, ctx))

    assert handled is True
    assert stickers.pack_names() == ["one"]
    assert "already" in update.message.replies[0].lower()
    assert not stickers.capture_pending()   # nothing left to wait for, it's in


# --- Sticker capture: reading the pack off a sticker the owner sends -------

def test_non_owner_sticker_is_not_captured_even_with_a_pending_flag(monkeypatch):
    _owner_only(monkeypatch)
    stickers.arm_capture()
    update = _FakeUpdate(NOT_OWNER, sticker=FakeStickerObj(set_name="notmine"))
    ctx = _FakeContext(bot_obj=FakeBot())

    handled = _run(commands.try_capture_sticker(update, ctx))

    assert handled is False
    assert stickers.pack_names() == []  # unchanged
    assert ctx.bot.requested is None
    assert stickers.capture_pending()  # still armed for the real owner


def test_capture_stores_the_pack_and_clears_the_flag(monkeypatch):
    _owner_only(monkeypatch)
    stickers.arm_capture()
    update = _FakeUpdate(OWNER, sticker=FakeStickerObj(set_name="ownerpack"))
    ctx = _FakeContext(bot_obj=FakeBot([("a", "💀"), ("b", "🗿")]))

    handled = _run(commands.try_capture_sticker(update, ctx))

    assert handled is True
    assert stickers.pack_names() == ["ownerpack"]
    assert ctx.bot.requested == "ownerpack"
    assert not stickers.capture_pending()
    assert stickers.enabled()
    assert "ownerpack" in update.message.replies[0]


def test_an_expired_flag_does_not_capture(monkeypatch):
    """The one that matters: fails if the expiry check is ever removed, since
    an unexpired-only capture_pending() would let this sticker through."""
    _owner_only(monkeypatch)
    db.set_kid_state(
        stickers.AWAITING_STICKER_KEY, str(time.time() - stickers.CAPTURE_WINDOW_S - 1)
    )
    update = _FakeUpdate(OWNER, sticker=FakeStickerObj(set_name="ownerpack"))
    ctx = _FakeContext(bot_obj=FakeBot())

    handled = _run(commands.try_capture_sticker(update, ctx))

    assert handled is False
    assert stickers.pack_names() == []  # unchanged
    assert ctx.bot.requested is None  # never even asked Telegram


def test_a_sticker_with_no_set_name_is_handled_without_changing_the_current_pack(monkeypatch):
    """A failed attempt must leave the flag armed — the natural next move is
    to send a different sticker, and re-typing /stickers is friction the
    owner shouldn't need at exactly the moment they're already confused."""
    _owner_only(monkeypatch)
    stickers.add_pack("existing")
    stickers.arm_capture()
    update = _FakeUpdate(OWNER, sticker=FakeStickerObj(set_name=None))
    ctx = _FakeContext(bot_obj=FakeBot())

    handled = _run(commands.try_capture_sticker(update, ctx))

    assert handled is True  # consumed this attempt, just not a load
    assert stickers.pack_names() == ["existing"]
    assert ctx.bot.requested is None
    assert stickers.capture_pending()  # still waiting for a valid one
    assert "another" in update.message.replies[0].lower() or "try" in update.message.replies[0].lower()


def test_a_load_failure_during_capture_leaves_the_flag_armed_for_a_retry(monkeypatch):
    _owner_only(monkeypatch)
    stickers.arm_capture()
    update = _FakeUpdate(OWNER, sticker=FakeStickerObj(set_name="badpack"))
    ctx = _FakeContext(bot_obj=FakeBot(fail=True))

    handled = _run(commands.try_capture_sticker(update, ctx))

    assert handled is True
    assert not stickers.enabled()
    assert stickers.capture_pending()  # still waiting — send another sticker, no need to retype
    reply = update.message.replies[0].lower()
    assert "couldn't" in reply or "fail" in reply
