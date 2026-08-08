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
    p = _prompt().lower()
    assert "never end a message with a period" in p
    assert "end your messages with periods" not in p


def test_salty_flips_the_period_rule_and_adds_the_wounded_line():
    p = _prompt(state={"salty": 1}).lower()
    assert "ghosted" in p
    assert "end your messages with periods" in p
    assert "never end a message with a period" not in p


def test_low_bond_also_flips_the_period_rule():
    p = _prompt(state={"bond": -50}).lower()
    assert "end your messages with periods" in p
    assert "never end a message with a period" not in p


# --- Fix 5: unhinged-short, not bland-short ---------------------------------

def test_the_slang_list_arrives_as_a_requirement_not_a_suggestion():
    """`SLANG TO LEAN ON: ...` was a list with no obligation attached, and the
    model ignored it: live output was `hey`, `idk lol`, `so bored`, `u fold
    laundry yet` while the whole vocab block went unused."""
    assert chat_engine.VOCAB_RULE in _prompt(vocab=["gyatt 🍑", "aura farming 📈"])
    assert chat_engine.VOCAB_RULE not in _prompt(vocab=[])


def test_the_mood_is_pushed_rather_than_merely_mentioned():
    """brainrot.PERSONAS' mood descriptions are vivid and were barely surfacing."""
    assert chat_engine.MOOD_RULE in _prompt()


def test_the_prompt_names_bland_shortness_as_the_failure():
    """`short` was winning over the personality. The constraint stays; what
    changes is that blandness is called out as a failure of it."""
    p = _prompt().lower()
    assert "idk lol" in p
    assert "overreact" in p


def test_the_format_rules_survive_the_rebalance():
    """lowercase, ||| separation and the length cap were working and are not
    the problem -- unhinged-short must not quietly become long."""
    p = _prompt().lower()
    assert "lowercase always" in p
    assert "under 10 words" in p
    assert "|||" in p
    assert "no bullet points" in p


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
