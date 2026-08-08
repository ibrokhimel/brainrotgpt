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


# --- Relevance: the kid has to answer what was actually said ----------------
#
# Live, the owner sent "yo whats up" and got back "u gettin soft or somethin 🔥
# ||| still on that laundry grind i hope 💪 ||| dont fold under pressure no cap
# 😭" -- in voice, and not one of the three an answer. The v2 prompt
# (brainrot.BASE_RULES) opened with an explicit on-topic requirement; this one
# had none, and HOW_YOU_TEXT actively licensed changing the subject.

def test_the_prompt_requires_an_on_topic_reply():
    p = _prompt()
    assert chat_engine.RELEVANCE_RULE in p
    low = p.lower()
    assert "on-topic" in low
    assert "never ignore what they said" in low


def test_relevance_outranks_the_style_rules():
    """Order is the whole point: when voice and relevance pull apart the model
    should already have read that relevance wins."""
    p = _prompt()
    assert p.index(chat_engine.RELEVANCE_RULE) < p.index(chat_engine.HOW_YOU_TEXT)


def test_the_licence_to_ignore_the_question_is_gone():
    """HOW_YOU_TEXT used to say `sometimes you just don't answer the question
    and say something else entirely`. That line is the direct cause of the
    non-sequiturs and must not come back."""
    low = _prompt(day_state="folding laundry", state={"notes": "n"}).lower()
    assert "don't answer the question" not in low
    assert "say something else entirely" not in low


def test_the_overreaction_is_anchored_to_what_they_said():
    """Keep the overreaction, drop the free-floating one -- unanchored, it
    attaches to whatever is nearest in context (the laundry)."""
    low = _prompt().lower()
    assert "overreact" in low
    assert "about what they said" in low


def test_the_day_state_arrives_as_context_rather_than_a_nudge():
    """`Bring it up if it fits. Don't force it.` ran on every single turn and
    is why laundry kept surfacing regardless of the message."""
    p = _prompt(day_state="folding laundry all day")
    assert chat_engine.DAY_STATE_RULE in p
    low = p.lower()
    assert "don't force it" not in low
    assert "only if it actually connects to what they just said" in low


def test_the_day_state_rule_is_absent_when_there_is_no_day_state():
    assert chat_engine.DAY_STATE_RULE not in _prompt()


def test_the_notes_block_is_conditional_too():
    assert chat_engine.NOTES_RULE in _prompt(state={"notes": "their name is walter"})
    assert chat_engine.NOTES_RULE not in _prompt()


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


# --- facts: the accumulating half of memory reaching the prompt -------------

def test_facts_reach_the_prompt_as_separate_lines():
    p = _prompt(facts=["their name is walter", "hates their job"])
    assert "- their name is walter" in p
    assert "- hates their job" in p


def test_the_facts_header_is_absent_when_there_are_none():
    p = _prompt(facts=[])
    assert "WHAT YOU KNOW ABOUT THEM" not in p
    assert chat_engine.FACTS_RULE not in p


def test_the_kid_is_told_to_use_the_facts_not_just_hold_them():
    """Knowing things about someone and never acting on it is the same as not
    knowing them -- the old block was handed over bare."""
    p = _prompt(facts=["hates their job"])
    assert chat_engine.FACTS_RULE in p
    low = p.lower()
    assert "act like you were listening" in low
    assert "never list them back" in low          # reference, don't recite
    assert "more than one at a time" in low       # reference, don't interrogate


def test_facts_supersede_the_notes_blob_rather_than_repeating_it():
    """notes is by construction the latest distillation's lines and every one of
    those was written to facts in the same pass, so printing both says the same
    thing twice under two rules that pull in different directions."""
    p = _prompt(state={"notes": "their name is walter"}, facts=["their name is walter"])
    assert p.count("their name is walter") == 1
    assert chat_engine.NOTES_RULE not in p


def test_a_legacy_notes_blob_still_shows_when_there_are_no_facts():
    """Chats whose notes predate the facts table must not go blank."""
    p = _prompt(state={"notes": "their name is walter"}, facts=[])
    assert "their name is walter" in p
    assert chat_engine.NOTES_RULE in p


def test_blank_facts_do_not_produce_an_empty_bullet():
    p = _prompt(facts=["", "   ", "hates their job"])
    assert "- hates their job" in p
    assert "- \n" not in p


def test_context_pulls_this_chats_facts_from_the_db(tmp_path):
    """_facts_for reads the chat off the state row, so a reply for chat 1 must
    never be handed chat 2's memory."""
    import db
    db.close()
    db.init_db(str(tmp_path / "ce_facts.db"))
    db.add_fact(1, "their name is walter")
    db.add_fact(2, "their name is jesse")
    assert chat_engine._facts_for(db.get_chat_state(1)) == ["their name is walter"]


def test_facts_lookup_never_breaks_generation(monkeypatch):
    def boom(chat_id, limit=0):
        raise RuntimeError("db gone")

    monkeypatch.setattr(chat_engine.db, "recent_facts", boom)
    assert chat_engine._facts_for({"chat_id": 1}) == []
    assert chat_engine._facts_for({}) == []
