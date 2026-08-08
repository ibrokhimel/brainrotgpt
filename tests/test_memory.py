import asyncio
import time

import db
import memory


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "m.db"))


def _run(coro):
    return asyncio.run(coro)


def test_transcript_labels_speakers(tmp_path):
    _fresh(tmp_path)
    db.add_message(1, "user", "hey")
    db.add_message(1, "kid", "yo")
    out = memory.transcript(1)
    assert "them: hey" in out
    assert "me: yo" in out
    assert out.index("them: hey") < out.index("me: yo")


def test_transcript_is_empty_for_a_new_chat(tmp_path):
    _fresh(tmp_path)
    assert memory.transcript(999) == ""


def test_last_kid_message_is_the_kids_own_most_recent_line(tmp_path):
    """The ghost ping's divergence rule needs the thing it must not repeat."""
    _fresh(tmp_path)
    db.add_message(1, "kid", "so bored")
    db.add_message(1, "user", "same")
    db.add_message(1, "kid", "still bored")
    assert memory.last_kid_message(1) == "still bored"


def test_last_kid_message_is_empty_when_the_kid_has_not_spoken(tmp_path):
    _fresh(tmp_path)
    db.add_message(1, "user", "hello?")
    assert memory.last_kid_message(1) == ""


def test_should_distill_only_at_the_threshold(tmp_path):
    _fresh(tmp_path)
    assert not memory.should_distill({"msgs_since_notes": 3})
    assert memory.should_distill({"msgs_since_notes": memory.NOTES_EVERY})
    assert memory.should_distill({"msgs_since_notes": memory.NOTES_EVERY + 4})


def test_distill_persists_and_caps_notes(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.add_message(1, "user", "im walter, i hate my job")
    monkeypatch.setattr(memory, "_ask", lambda prompt: _done("x" * 2000))
    state = db.get_chat_state(1)
    notes = _run(memory.distill(1, state))
    assert len(notes) <= memory.NOTES_MAX_CHARS
    assert db.get_chat_state(1)["notes"] == notes
    assert db.get_chat_state(1)["msgs_since_notes"] == 0


def test_distill_keeps_old_notes_when_the_model_fails(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.update_chat_state(1, notes="knows: walter", msgs_since_notes=20)
    db.add_message(1, "user", "hi")

    async def boom(prompt):
        raise RuntimeError("groq down")

    monkeypatch.setattr(memory, "_ask", boom)
    state = db.get_chat_state(1)
    assert _run(memory.distill(1, state)) == "knows: walter"
    assert db.get_chat_state(1)["msgs_since_notes"] == 0   # counter still resets


# --- facts: the counter, and the two ways a pass can come back empty --------

def test_distill_stores_one_fact_per_line(tmp_path, monkeypatch):
    """A paragraph can only be stored whole and rewritten whole, so a later pass
    silently drops what an earlier one caught. Lines are stored independently."""
    _fresh(tmp_path)
    db.add_message(1, "user", "im walter, i hate my job")
    monkeypatch.setattr(memory, "_ask", lambda p: _done(
        "their name is walter\n- hates their job\n\n2. has a kid called flynn"))
    _run(memory.distill(1, db.get_chat_state(1)))

    assert [f["fact"] for f in db.recent_facts(1)] == [
        "has a kid called flynn", "hates their job", "their name is walter"]


def test_a_repeated_fact_does_not_pile_up_across_passes(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.add_message(1, "user", "im walter")
    monkeypatch.setattr(memory, "_ask", lambda p: _done("their name is walter"))
    for _ in range(3):
        _run(memory.distill(1, db.get_chat_state(1)))
    assert len(db.recent_facts(1)) == 1


def test_a_none_backs_off_a_few_messages_rather_than_a_whole_cycle(tmp_path, monkeypatch):
    """THE bug that left every chat memoryless in production. NONE is not a
    failure -- the model worked and the chat was simply still too thin, which
    stops being true a few messages later. Resetting the counter for it threw
    that away and pushed the next attempt a full cycle out, so on a short bursty
    exchange the kid could never accumulate anything at all."""
    _fresh(tmp_path)
    db.add_message(1, "user", "yo")
    monkeypatch.setattr(memory, "_ask", lambda p: _done("NONE"))
    _run(memory.distill(1, db.get_chat_state(1)))

    since = db.get_chat_state(1)["msgs_since_notes"]
    assert since == memory.NOTES_EVERY - memory.NONE_BACKOFF
    assert 0 < since < memory.NOTES_EVERY
    assert not memory.should_distill(db.get_chat_state(1))          # not immediately
    assert memory.should_distill({"msgs_since_notes": since + memory.NONE_BACKOFF})


def test_a_model_failure_still_resets_the_counter_fully(tmp_path, monkeypatch):
    """The other half of the same decision, kept separate on purpose: a broken
    model must not be retried on every single message."""
    _fresh(tmp_path)
    db.add_message(1, "user", "yo")

    async def boom(prompt):
        raise RuntimeError("groq down")

    monkeypatch.setattr(memory, "_ask", boom)
    _run(memory.distill(1, db.get_chat_state(1)))
    assert db.get_chat_state(1)["msgs_since_notes"] == 0


def test_distillation_respects_the_outbound_budget(tmp_path, monkeypatch):
    """Distillation is a proactive call, and six-message intervals mean a lot
    more of them. It must still stop when the budget is gone."""
    _fresh(tmp_path)
    import budget
    import config
    monkeypatch.setattr(config, "OUTBOUND_DAILY_BUDGET", 1)
    budget.spend(time.time())

    def never(prompt):
        raise AssertionError("asked the model with no budget left")

    monkeypatch.setattr(memory, "_ask", never)
    db.add_message(1, "user", "yo")
    assert _run(memory.distill(1, db.get_chat_state(1))) == ""


def test_a_successful_pass_spends_budget(tmp_path, monkeypatch):
    _fresh(tmp_path)
    import budget
    import config
    monkeypatch.setattr(config, "OUTBOUND_DAILY_BUDGET", 50)
    now = time.time()
    before = budget.remaining(now)
    monkeypatch.setattr(memory, "_ask", lambda p: _done("their name is walter"))
    db.add_message(1, "user", "im walter")
    _run(memory.distill(1, db.get_chat_state(1)))
    assert budget.remaining(now) == before - 1


async def _done(value):
    return value
