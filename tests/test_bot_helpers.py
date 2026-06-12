import asyncio

from telegram.error import Conflict

import bot


def test_build_transcript_labels_senders():
    buf = [{"sender": "Alex", "text": "hi"}, {"sender": None, "text": "yo"}]
    assert bot.build_transcript(buf) == "Alex: hi\nyo"


def test_split_text_short_is_single():
    assert bot.split_text("hello") == ["hello"]


def test_split_text_long_splits_under_limit():
    text = "word " * 2000  # ~10k chars
    chunks = bot.split_text(text, limit=4096)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)


def test_build_preview_truncates_long_lines():
    buf = [{"sender": None, "text": "x" * 500}]
    preview = bot.build_preview(buf)
    assert "..." in preview
    assert len(preview) < 500


def test_persona_label_random():
    assert bot.persona_label_of("random") == "🎲 Random"
    assert "Sigma" in bot.persona_label_of("sigma")


def test_keyboards_build():
    # smoke: keyboards construct without error
    assert bot.confirm_keyboard().inline_keyboard
    assert bot.result_keyboard(3, 0, True).inline_keyboard
    assert bot.persona_kb().inline_keyboard
    assert bot.intensity_kb().inline_keyboard
    assert bot.length_kb().inline_keyboard
    assert bot.lang_kb().inline_keyboard
    assert bot.cand_kb().inline_keyboard


def _callbacks(kb):
    return {b.callback_data for row in kb.inline_keyboard for b in row}


def test_merge_button_only_with_prev():
    assert "merge" not in _callbacks(bot.confirm_keyboard(has_prev=False))
    assert "merge" in _callbacks(bot.confirm_keyboard(has_prev=True))


def test_start_fresh_archives_after_generated():
    session = {"buffer": [{"sender": None, "text": "old"}], "candidates": ["x"], "generated": True}
    bot.start_fresh_if_done(session)
    assert session["buffer"] == []
    assert session["prev_buffer"] == [{"sender": None, "text": "old"}]
    assert session["candidates"] == []
    assert session["generated"] is False


def test_start_fresh_noop_while_building():
    session = {"buffer": [{"sender": None, "text": "a"}], "generated": False}
    bot.start_fresh_if_done(session)
    assert session["buffer"] == [{"sender": None, "text": "a"}]
    assert "prev_buffer" not in session


def test_confirm_message_flags_prev():
    session = {"buffer": [{"sender": None, "text": "new"}], "prev_buffer": [{"sender": None, "text": "old"}]}
    text, kb = bot.confirm_message(session)
    assert "Merge" in text
    assert "merge" in _callbacks(kb)


# --- group mode helpers ---------------------------------------------------

class _Ent:
    def __init__(self, type_, offset, length, user=None):
        self.type, self.offset, self.length, self.user = type_, offset, length, user


class _User:
    def __init__(self, uid):
        self.id = uid


class _GMsg:
    def __init__(self, text, entities=None):
        self.text = text
        self.caption = None
        self.entities = entities or []
        self.caption_entities = []


def test_parse_mention_detects_and_strips():
    text = "@brainrotcbot roast him fr"
    m = _GMsg(text, [_Ent("mention", 0, len("@brainrotcbot"))])
    mentioned, leftover = bot.parse_mention(m, "brainrotcbot", 999)
    assert mentioned is True
    assert leftover == "roast him fr"


def test_parse_mention_ignores_other_username():
    m = _GMsg("@someoneelse hey", [_Ent("mention", 0, len("@someoneelse"))])
    mentioned, _ = bot.parse_mention(m, "brainrotcbot", 999)
    assert mentioned is False


def test_parse_mention_text_mention_by_id():
    m = _GMsg("hey you", [_Ent("text_mention", 0, 3, user=_User(999))])
    mentioned, _ = bot.parse_mention(m, "brainrotcbot", 999)
    assert mentioned is True


def test_group_history_rolls_to_maxlen():
    cid = -100123
    bot.group_history(cid).clear()
    for i in range(bot.config.GROUP_HISTORY_SIZE + 5):
        bot.group_history(cid).append({"sender": None, "text": str(i)})
    dq = bot.group_history(cid)
    assert len(dq) == bot.config.GROUP_HISTORY_SIZE
    assert dq[-1]["text"] == str(bot.config.GROUP_HISTORY_SIZE + 4)  # newest kept


# --- single-instance guard + error handler --------------------------------

def test_single_instance_lock_blocks_second_acquire():
    first = bot.acquire_single_instance_lock()
    try:
        if first:  # only assert the blocking property once we actually hold it
            assert bot.acquire_single_instance_lock() is False
    finally:
        if bot._instance_lock_sock is not None:
            bot._instance_lock_sock.close()
            bot._instance_lock_sock = None


def test_on_error_swallows_conflict_and_generic():
    class _Ctx:
        def __init__(self, error):
            self.error = error

    # neither should raise out of the handler
    asyncio.run(bot.on_error(None, _Ctx(Conflict("boom"))))
    asyncio.run(bot.on_error(None, _Ctx(RuntimeError("kaboom"))))
