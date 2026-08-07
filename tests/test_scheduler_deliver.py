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


# --- I3: spec 12's "retry once on the next tick" ----------------------------

class _Ctx:
    def __init__(self, bot):
        self.bot = bot


def test_a_failed_reply_is_retried_on_the_next_tick(tmp_path, monkeypatch):
    """tick clears next_action_at before acting -- correctly, so a mid-action
    failure can't re-fire every tick forever -- but nothing ever rescheduled.
    A single Groq 5xx or rate-limit during a reply job meant the user's message
    was answered NEVER, with no log they could see and no state remembering a
    reply was owed. On a shared free-tier key with backup keys chained for
    exactly this reason, transient failure is the expected case.
    """
    _fresh(tmp_path)
    db.update_chat_state(1, next_action_at=100.0, next_action_kind="reply")

    async def boom(chat_id, state, *, rng):
        raise RuntimeError("groq 503")

    monkeypatch.setattr(scheduler.chat_engine, "reply", boom)
    monkeypatch.setattr(scheduler.chat_engine, "should_reroll_mood", lambda *a, **kw: False)
    monkeypatch.setattr(scheduler.config, "COLDOPEN_ENABLED", False)
    _run(scheduler.tick(_Ctx(_Bot())))

    state = db.get_chat_state(1)
    assert state["next_action_at"] is not None, "the reply was dropped, not retried"
    assert state["next_action_kind"] == "reply:retry"


def test_a_second_failure_drops_the_reply_silently(tmp_path, monkeypatch):
    """Spec 12: retry ONCE, then drop it silently. Not an infinite loop."""
    _fresh(tmp_path)
    db.update_chat_state(1, next_action_at=100.0, next_action_kind="reply:retry")

    async def boom(chat_id, state, *, rng):
        raise RuntimeError("groq 503 again")

    monkeypatch.setattr(scheduler.chat_engine, "reply", boom)
    monkeypatch.setattr(scheduler.chat_engine, "should_reroll_mood", lambda *a, **kw: False)
    monkeypatch.setattr(scheduler.config, "COLDOPEN_ENABLED", False)
    _run(scheduler.tick(_Ctx(_Bot())))

    state = db.get_chat_state(1)
    assert state["next_action_at"] is None
    assert state["next_action_kind"] is None


def test_a_retry_that_succeeds_behaves_like_an_ordinary_reply(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.update_chat_state(1, next_action_at=100.0, next_action_kind="reply:retry")

    async def fake_reply(chat_id, state, *, rng):
        return [burst.Piece("text", "sorry phone died")]

    monkeypatch.setattr(scheduler.chat_engine, "reply", fake_reply)
    monkeypatch.setattr(scheduler.chat_engine, "should_reroll_mood", lambda *a, **kw: False)
    monkeypatch.setattr(scheduler.config, "COLDOPEN_ENABLED", False)
    bot = _Bot()
    _run(scheduler.tick(_Ctx(bot)))

    assert bot.sent == ["sorry phone died"]
    assert db.get_chat_state(1)["next_action_kind"] == "ping"   # the ladder arms normally


def test_a_failed_ping_is_not_retried(tmp_path, monkeypatch):
    """Only replies are owed to a person. A missed proactive ping is just a
    ping that didn't happen -- retrying it would spend budget chasing someone
    on the kid's behalf, not answering them."""
    _fresh(tmp_path)
    db.update_chat_state(1, next_action_at=100.0, next_action_kind="ping", ping_stage=1)

    async def boom(chat_id, state, stage, *, rng):
        raise RuntimeError("groq 503")

    monkeypatch.setattr(scheduler.chat_engine, "ping", boom)
    monkeypatch.setattr(scheduler.config, "COLDOPEN_ENABLED", False)
    _run(scheduler.tick(_Ctx(_Bot())))

    assert db.get_chat_state(1)["next_action_at"] is None
