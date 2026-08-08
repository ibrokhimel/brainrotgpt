"""Fix 1: a computed reply delay must actually be the delay.

`schedule_reply_at` returns a couple of seconds for an engaged chat, but
delivery used to happen only in `scheduler.tick`, which runs every 60 seconds
-- so a 3-second reply landed anywhere from 3 to 63 seconds later. Live: `yo`
at 18:20:15 was answered at 18:21:51, 96 seconds for a message that should have
taken ~35.

These cover the in-process fast path and, just as importantly, the properties
it must NOT break: the schedule is still written to SQLite before anything
sleeps (so a restart mid-sleep recovers through the tick), and the tick and the
fast path can never both deliver the same reply.
"""
import asyncio
import time

import bot
import burst
import db
import scheduler
from rate_limit import RateLimiter


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "fp.db"))


def _run(coro):
    return asyncio.run(coro)


class _Bot:
    def __init__(self):
        self.sent = []


class _Ctx:
    def __init__(self, bot_obj):
        self.bot = bot_obj


class _FakeMessage:
    # Distinct uids per test on purpose: bot.limiter's per-user cooldown is
    # module-level state, so reusing an id silently drops the second message.
    def __init__(self, text, uid):
        self.text = text
        self.caption = None
        self.from_user = type("U", (), {"id": uid, "full_name": "Them"})()
        self.message_id = 1

    async def set_reaction(self, reaction):
        pass


class _FakeUpdate:
    def __init__(self, message, chat_id):
        self.message = message
        self.effective_chat = type("C", (), {"id": chat_id})()


def _quiet(monkeypatch):
    """Stub out Groq and the typing pauses, leaving the scheduling plumbing real.

    `burst.send` is replaced rather than `asyncio.sleep` on purpose: these tests
    measure real elapsed time, so the module-wide sleep has to keep working.

    `bot.limiter` is module-level and its 80-per-rolling-minute global cap is
    shared by the whole suite, so a test that reaches on_user_message needs its
    own — otherwise it passes alone and silently drops the message in a full run.
    """
    async def fake_send(bot_obj, chat_id, pieces, **kw):
        texts = [p.value for p in pieces if p.kind == "text"]
        bot_obj.sent.extend(texts)
        return texts

    async def fake_reply(chat_id, state, *, rng):
        return [burst.Piece("text", "yo")]

    monkeypatch.setattr(bot, "limiter", RateLimiter())
    monkeypatch.setattr(scheduler.burst, "send", fake_send)
    monkeypatch.setattr(scheduler.burst, "apply_typos", lambda pieces, *, rng: pieces)
    monkeypatch.setattr(scheduler.stickers, "enabled", lambda: False)
    monkeypatch.setattr(scheduler.chat_engine, "reply", fake_reply)
    monkeypatch.setattr(scheduler.chat_engine, "should_reroll_mood", lambda *a, **kw: False)
    monkeypatch.setattr(scheduler.config, "GHOST_ENABLED", False)
    monkeypatch.setattr(scheduler.config, "COLDOPEN_ENABLED", False)
    monkeypatch.setattr(scheduler.memory, "should_distill", lambda state: False)


# --- the fix itself ---------------------------------------------------------

def test_a_short_reply_delay_is_delivered_without_waiting_for_a_tick(tmp_path, monkeypatch):
    """The headline defect: the tick's 60s granularity swamped the delay."""
    _fresh(tmp_path)
    _quiet(monkeypatch)
    monkeypatch.setattr(bot._rng, "random", lambda: 1.0)     # skip the reaction branch
    monkeypatch.setattr(bot.ghost, "schedule_reply_at", lambda now, **kw: now + 0.02)
    bot_obj = _Bot()

    async def scenario():
        await bot.on_user_message(_FakeUpdate(_FakeMessage("yo", 8010), 801), _Ctx(bot_obj))
        await asyncio.sleep(0.15)   # far less than one tick, and no tick is run
        return bot_obj.sent

    assert _run(scenario()) == ["yo"], "the reply waited for the tick"
    assert db.get_chat_state(801)["next_action_at"] is None


def test_the_live_cold_reply_that_missed_the_ceiling_is_taken_in_process(tmp_path, monkeypatch):
    """The exact failure: "morning" at 09:08:32 computed a 65.3s reply, which
    overshot the 55s ceiling by ten seconds, fell through to the tick, and was
    still pending -- 13.4s overdue -- at +78s. A reply should never wait for
    the tick; the tick's real jobs are minutes to days out."""
    _fresh(tmp_path)
    _quiet(monkeypatch)
    state = db.update_chat_state(1, next_action_at=time.time() + 65.3,
                                 next_action_kind="reply")

    async def scenario():
        task = scheduler.arm_fast_reply(_Bot(), 1, state)
        if task is not None:
            task.cancel()
        return task

    assert _run(scenario()) is not None, "a 65s reply still fell through to the tick"


def test_a_long_delay_stays_entirely_on_the_tick(tmp_path, monkeypatch):
    """Ghost pings, cold opens and sleep-window deferrals must not be taken
    in-process: an hours-long asyncio.sleep is exactly what SQLite scheduling
    exists to avoid."""
    _fresh(tmp_path)
    _quiet(monkeypatch)
    state = db.update_chat_state(1, next_action_at=time.time() + 3600,
                                 next_action_kind="reply")

    async def scenario():
        return scheduler.arm_fast_reply(_Bot(), 1, state)

    assert _run(scenario()) is None
    assert db.get_chat_state(1)["next_action_at"] is not None


def test_the_fast_path_never_takes_a_ping_or_a_cold_open(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _quiet(monkeypatch)
    for kind in ("ping", "coldopen"):
        state = db.update_chat_state(1, next_action_at=time.time() + 1,
                                     next_action_kind=kind)

        async def scenario(state=state):
            return scheduler.arm_fast_reply(_Bot(), 1, state)

        assert _run(scenario()) is None, f"{kind} was taken off the tick"


# --- durability: the property the fast path must not cost us ----------------

def test_the_schedule_is_in_sqlite_before_anything_sleeps(tmp_path, monkeypatch):
    """`chat_state.next_action_at` is written BEFORE the task is spawned, so a
    process that dies mid-sleep still recovers the pending reply on the next
    tick. The in-process task is a fast path, not a replacement for the row."""
    _fresh(tmp_path)
    _quiet(monkeypatch)
    monkeypatch.setattr(bot._rng, "random", lambda: 1.0)
    monkeypatch.setattr(bot.ghost, "schedule_reply_at", lambda now, **kw: now + 30)
    seen = {}

    async def scenario():
        await bot.on_user_message(_FakeUpdate(_FakeMessage("yo", 8020), 802), _Ctx(_Bot()))
        # The handler has returned and the task is still sleeping. This is the
        # instant a `kill -9` would land in.
        seen.update(db.get_chat_state(802))

    _run(scenario())
    assert seen["next_action_at"] is not None
    assert seen["next_action_kind"] == "reply"


def test_a_restart_during_the_sleep_still_delivers_through_the_tick(tmp_path, monkeypatch):
    """Losing the in-process task (a restart) must lose nothing: the row is
    still armed and the tick picks it up, which is the pre-fix behaviour and
    the whole reason scheduling lives in SQLite."""
    _fresh(tmp_path)
    _quiet(monkeypatch)
    monkeypatch.setattr(bot._rng, "random", lambda: 1.0)
    monkeypatch.setattr(bot.ghost, "schedule_reply_at", lambda now, **kw: now + 30)
    bot_obj = _Bot()

    async def before_restart():
        await bot.on_user_message(_FakeUpdate(_FakeMessage("yo", 8030), 803), _Ctx(bot_obj))
        for t in list(scheduler._pending):
            t.cancel()          # the process died; every pending task went with it
        await asyncio.sleep(0)

    _run(before_restart())
    assert bot_obj.sent == []

    async def after_restart():
        # A fresh process: only the tick, and the schedule is now due.
        db.update_chat_state(803, next_action_at=time.time() - 1)
        await scheduler.tick(_Ctx(bot_obj))

    _run(after_restart())
    assert bot_obj.sent == ["yo"]


# --- double delivery --------------------------------------------------------

def test_the_tick_and_the_fast_path_cannot_both_deliver(tmp_path, monkeypatch):
    """Both clear `next_action_at` atomically before acting, so whichever wins
    the race delivers and the other no-ops. Answering one message twice is the
    most obviously non-human failure this change could have introduced."""
    _fresh(tmp_path)
    _quiet(monkeypatch)
    bot_obj = _Bot()
    state = db.update_chat_state(1, next_action_at=time.time() + 0.02,
                                 next_action_kind="reply")

    async def scenario():
        task = scheduler.arm_fast_reply(bot_obj, 1, state)
        await asyncio.sleep(0.08)               # the fast path fires here
        await scheduler.tick(_Ctx(bot_obj))     # ...and the tick right after
        await task

    _run(scenario())
    assert bot_obj.sent == ["yo"]


def test_the_tick_winning_the_race_leaves_the_fast_path_a_no_op(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _quiet(monkeypatch)
    bot_obj = _Bot()
    state = db.update_chat_state(1, next_action_at=time.time() - 1,
                                 next_action_kind="reply")

    async def scenario():
        task = scheduler.arm_fast_reply(bot_obj, 1, state)
        await scheduler.tick(_Ctx(bot_obj))     # the tick claims it first
        await task

    _run(scenario())
    assert bot_obj.sent == ["yo"]


def test_the_tick_ignores_a_row_the_fast_path_already_claimed(tmp_path, monkeypatch):
    """The race that actually bites: `due_chats` snapshots the row, the fast
    path claims and delivers it, and only then does the tick reach that entry
    in its own loop. Re-claiming inside the loop, rather than trusting the
    snapshot, is what stops this becoming two identical replies to one message.
    """
    _fresh(tmp_path)
    _quiet(monkeypatch)
    bot_obj = _Bot()
    stale = db.update_chat_state(1, next_action_at=time.time() - 1, next_action_kind="reply")
    monkeypatch.setattr(db, "due_chats", lambda _now: [stale])   # the snapshot
    assert db.claim_due_action(1, time.time()) is not None       # ...the fast path took it

    _run(scheduler.tick(_Ctx(bot_obj)))
    assert bot_obj.sent == [], "the tick acted on a stale snapshot"


def test_a_stale_task_does_not_deliver_a_reply_rescheduled_for_later(tmp_path, monkeypatch):
    """A second message re-arms the row for a later time. The first task wakes,
    finds nothing DUE, and must leave the pending reply alone rather than firing
    it early and clearing it."""
    _fresh(tmp_path)
    _quiet(monkeypatch)
    bot_obj = _Bot()
    state = db.update_chat_state(1, next_action_at=time.time() + 0.02,
                                 next_action_kind="reply")

    async def scenario():
        task = scheduler.arm_fast_reply(bot_obj, 1, state)
        db.update_chat_state(1, next_action_at=time.time() + 3600)
        await task

    _run(scenario())
    assert bot_obj.sent == []
    assert db.get_chat_state(1)["next_action_at"] is not None, "the pending reply was eaten"


def test_a_fast_path_failure_gets_spec_12s_one_retry(tmp_path, monkeypatch):
    """The fast path runs the same delivery path as the tick, so it inherits the
    retry contract too -- otherwise moving replies off the tick would quietly
    drop every transient Groq failure."""
    _fresh(tmp_path)
    _quiet(monkeypatch)

    async def boom(chat_id, state, *, rng):
        raise RuntimeError("groq 503")

    monkeypatch.setattr(scheduler.chat_engine, "reply", boom)
    state = db.update_chat_state(1, next_action_at=time.time() + 0.02,
                                 next_action_kind="reply")

    async def scenario():
        await scheduler.arm_fast_reply(_Bot(), 1, state)

    _run(scenario())
    assert db.get_chat_state(1)["next_action_kind"] == scheduler.RETRY_KIND


# --- the atomic claim itself ------------------------------------------------

def test_claim_due_action_hands_the_row_to_exactly_one_caller(tmp_path):
    _fresh(tmp_path)
    now = time.time()
    db.update_chat_state(1, next_action_at=now - 1, next_action_kind="reply")

    first = db.claim_due_action(1, now)
    second = db.claim_due_action(1, now)

    assert first is not None and first["next_action_kind"] == "reply"
    assert second is None
    assert db.get_chat_state(1)["next_action_at"] is None


def test_claim_due_action_ignores_an_action_that_is_not_due_yet(tmp_path):
    _fresh(tmp_path)
    now = time.time()
    db.update_chat_state(1, next_action_at=now + 60, next_action_kind="reply")
    assert db.claim_due_action(1, now) is None
    assert db.get_chat_state(1)["next_action_at"] is not None


def test_claim_due_action_respects_a_kind_filter(tmp_path):
    _fresh(tmp_path)
    now = time.time()
    db.update_chat_state(1, next_action_at=now - 1, next_action_kind="ping")
    assert db.claim_due_action(1, now, kinds=("reply",)) is None
    assert db.get_chat_state(1)["next_action_kind"] == "ping"


def test_claim_due_action_never_claims_a_muted_or_given_up_chat(tmp_path):
    _fresh(tmp_path)
    now = time.time()
    db.update_chat_state(1, next_action_at=now - 1, next_action_kind="reply", muted=1)
    assert db.claim_due_action(1, now) is None
    db.update_chat_state(2, next_action_at=now - 1, next_action_kind="reply", gave_up=1)
    assert db.claim_due_action(2, now) is None
