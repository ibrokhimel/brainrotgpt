import asyncio

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
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, uid):
        self.effective_user = _FakeUser(uid)
        self.message = _FakeMessage()


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

    assert db.get_kid_state(stickers.STICKER_PACK_KEY) == "mypack"
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

    assert db.get_kid_state(stickers.STICKER_PACK_KEY) == "mypack"
    assert ctx.bot.requested == "mypack"


def test_off_disables_stickers(monkeypatch):
    _owner_only(monkeypatch)
    db.set_kid_state(stickers.STICKER_PACK_KEY, "mypack")
    _run(stickers.load(FakeBot([("a", "💀")])))
    assert stickers.enabled()

    update = _FakeUpdate(OWNER)
    ctx = _FakeContext(args=["off"])
    _run(commands.cmd_stickers(update, ctx))

    assert not stickers.enabled()
    assert db.get_kid_state(stickers.STICKER_PACK_KEY) == ""


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
    db.set_kid_state(stickers.STICKER_PACK_KEY, "mypack")
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
