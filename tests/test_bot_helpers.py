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
    assert "Gym" in bot.persona_label_of("gym_sigma")


def test_keyboards_build():
    # smoke: keyboards construct without error
    assert bot.confirm_keyboard().inline_keyboard
    assert bot.result_keyboard(3, 0, True).inline_keyboard
    assert bot.persona_kb().inline_keyboard
    assert bot.intensity_kb().inline_keyboard
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
