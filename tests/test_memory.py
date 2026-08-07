import asyncio

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


async def _done(value):
    return value
