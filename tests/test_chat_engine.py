import random

import chat_engine

STATE = {"mood": "sigma", "bond": 0, "notes": "", "salty": 0, "chattiness": "normal",
         "mood_set_at": 0.0}


def _prompt(**over):
    state = dict(STATE, **over.pop("state", {}))
    kw = dict(day_state="", memes=[], vocab=["rizz"], sticker_emoji=[], burst_target=2)
    kw.update(over)
    return chat_engine.build_system_prompt(state, **kw)


def test_prompt_names_the_kid_and_its_age():
    p = _prompt()
    assert chat_engine.KID_NAME in p
    assert str(chat_engine.KID_AGE) in p


def test_prompt_includes_the_burst_delimiter_instruction():
    assert "|||" in _prompt()


def test_prompt_carries_the_current_mood():
    assert "SIGMA" in _prompt(state={"mood": "sigma"}).upper()


def test_prompt_includes_the_day_state():
    assert "mom took my phone" in _prompt(day_state="mom took my phone")


def test_prompt_includes_notes_when_present():
    assert "walter" in _prompt(state={"notes": "their name is walter"}).lower()


def test_prompt_omits_the_notes_header_when_empty():
    assert "WHAT YOU KNOW ABOUT THEM" not in _prompt()


def test_prompt_includes_meme_blurbs():
    p = _prompt(memes=[{"term": "67", "blurb": "a number people yell"}])
    assert "67" in p and "a number people yell" in p


def test_prompt_lists_sticker_emoji_only_when_a_pack_is_loaded():
    assert "[sticker:" in _prompt(sticker_emoji=["💀", "🗿"])
    assert "[sticker:" not in _prompt(sticker_emoji=[])


def test_no_trailing_periods_normally():
    p = _prompt()
    assert "never end a message with a period" in p.lower()


def test_salty_flips_the_period_rule_and_adds_the_wounded_line():
    p = _prompt(state={"salty": 1})
    assert "ghosted" in p.lower()
    assert "use periods" in p.lower()


def test_bond_line_changes_across_buckets():
    low = chat_engine.bond_line(-50)
    mid = chat_engine.bond_line(5)
    high = chat_engine.bond_line(80)
    assert len({low, mid, high}) == 3


def test_bond_line_appears_in_the_prompt():
    assert chat_engine.bond_line(80) in _prompt(state={"bond": 80})


def test_mood_rerolls_only_once_stale():
    rng = random.Random(0)
    fresh = {"mood_set_at": 1000.0}
    assert not chat_engine.should_reroll_mood(fresh, 1000.0 + 60, rng=rng)
    assert chat_engine.should_reroll_mood(fresh, 1000.0 + 48 * 3600, rng=rng)


def test_mood_rerolls_when_never_set():
    assert chat_engine.should_reroll_mood({"mood_set_at": None}, 5.0, rng=random.Random(0))
