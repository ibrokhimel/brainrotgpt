"""scheduler.deliver is a shared entry point with two callers (the tick and the
group path). These cover the contract both of them inherit."""
import asyncio

import pytest
from telegram.error import Forbidden

import burst
import db
import scheduler


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "sd.db"))


def _run(coro):
    return asyncio.run(coro)


class _Bot:
    """Sends succeed unless `fail_with` is set."""

    def __init__(self, fail_with=None):
        self.fail_with = fail_with
        self.sent = []

    async def send_chat_action(self, chat_id, action):
        pass

    async def send_message(self, chat_id, text, **kw):
        if self.fail_with:
            raise self.fail_with
        self.sent.append(text)
        return type("M", (), {"message_id": len(self.sent)})()

    async def send_sticker(self, chat_id, file_id, **kw):
        pass


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    async def instant(_seconds):
        pass

    monkeypatch.setattr(scheduler.asyncio, "sleep", instant)
    # Keep the random sticker out of these tests so `pieces` is what we passed.
    monkeypatch.setattr(scheduler.stickers, "enabled", lambda: False)
    monkeypatch.setattr(scheduler.burst, "apply_typos", lambda pieces, *, rng: pieces)


def test_deliver_records_the_kid_spoke_when_a_send_lands(tmp_path):
    _fresh(tmp_path)
    db.update_chat_state(1, salty=1)
    ok = _run(scheduler.deliver(_Bot(), 1, [burst.Piece("text", "yo")], db.get_chat_state(1)))

    assert ok is True
    state = db.get_chat_state(1)
    assert state["last_kid_ts"] is not None
    assert state["salty"] == 0
    assert [r["text"] for r in db.recent_messages(1)] == ["yo"]


def test_deliver_does_not_consume_salty_when_every_send_failed(tmp_path):
    """`if sent or pieces` was unconditionally true -- pieces is guaranteed
    non-empty by the early return above it, so `sent` contributed nothing.

    The wounded reply a user earned by coming back is then burned without ever
    being delivered, and last_kid_ts advances from a message that does not
    exist -- which is what the ghost ladder and the cold-open quiet window are
    both measured from.
    """
    _fresh(tmp_path)
    db.update_chat_state(1, salty=1, gave_up=0)
    before = db.get_chat_state(1)
    bot = _Bot(fail_with=RuntimeError("groq/telegram blip"))
    ok = _run(scheduler.deliver(bot, 1, [burst.Piece("text", "yo")], before))

    assert ok is False
    state = db.get_chat_state(1)
    assert state["salty"] == 1, "the wounded reply is still owed"
    assert state["last_kid_ts"] == before["last_kid_ts"], "the kid did not speak"


def test_deliver_mutes_the_chat_when_the_user_blocked_the_bot(tmp_path):
    """Forbidden handling belongs in deliver, not at each call site: it has two
    callers and the response is the same for both. on_group_message was the
    second caller and had none, so being removed from a group meant every
    subsequent @mention paid for a full 70B generation and logged a traceback,
    forever, with nothing ever muting the chat."""
    _fresh(tmp_path)
    db.update_chat_state(1, next_action_at=123.0, next_action_kind="ping")
    bot = _Bot(fail_with=Forbidden("bot was blocked by the user"))
    ok = _run(scheduler.deliver(bot, 1, [burst.Piece("text", "yo")], db.get_chat_state(1)))

    assert ok is False
    state = db.get_chat_state(1)
    assert state["muted"] == 1
    assert state["next_action_at"] is None
    assert state["next_action_kind"] is None


def test_deliver_does_not_raise_forbidden_into_its_callers(tmp_path):
    """The group path has no try/except around deliver; if this raises, it
    lands in PTB's error handler, which only logs."""
    _fresh(tmp_path)
    bot = _Bot(fail_with=Forbidden("bot was blocked by the user"))
    _run(scheduler.deliver(bot, 1, [burst.Piece("text", "yo")], db.get_chat_state(1)))
    # No exception escaped.


def test_do_reply_arms_no_ghost_ping_when_nothing_was_delivered(tmp_path, monkeypatch):
    """Chasing someone about a message they never received is the same defect
    as pinging someone who just replied -- the ladder only makes sense after
    the kid actually spoke."""
    _fresh(tmp_path)

    async def fake_reply(chat_id, state, *, rng):
        return [burst.Piece("text", "yo")]

    monkeypatch.setattr(scheduler.chat_engine, "reply", fake_reply)
    monkeypatch.setattr(scheduler.chat_engine, "should_reroll_mood", lambda *a, **kw: False)
    bot = _Bot(fail_with=RuntimeError("telegram blip"))
    _run(scheduler._do_reply(bot, 1, db.get_chat_state(1), 1000.0))

    state = db.get_chat_state(1)
    assert state["next_action_kind"] != "ping"
