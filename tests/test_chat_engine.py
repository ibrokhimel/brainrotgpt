import random

import chat_engine
import persona
import recall

STATE = {"mood": "sigma", "bond": 0, "notes": "", "salty": 0, "chattiness": "normal",
         "mood_set_at": 0.0}


def _prompt(**over):
    state = dict(STATE, **over.pop("state", {}))
    kw = dict(day_state="", memes=[], vocab=["rizz"], sticker_emoji=[], burst_target=2)
    kw.update(over)
    return persona.build_system_prompt(state, **kw)


def test_prompt_names_the_kid_and_its_age():
    p = _prompt()
    assert persona.KID_NAME in p
    assert str(persona.KID_AGE) in p


def test_prompt_includes_the_burst_delimiter_instruction():
    assert "|||" in _prompt()


def test_prompt_carries_the_current_mood():
    assert "SIGMA" in _prompt(state={"mood": "sigma"}).upper()


def test_a_retired_mood_in_the_db_does_not_break_the_prompt():
    """`nerd` is in live chat_state rows and is no longer a persona. It has to
    degrade to the default mood rather than crash or leak the lecturer voice."""
    p = _prompt(state={"mood": "nerd"})
    assert "SKIBIDI" in p.upper()
    assert "pushing up the glasses" not in p.lower()   # the nerd voice, not just its key


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
    assert persona.VOCAB_RULE in _prompt(vocab=["gyatt 🍑", "aura farming 📈"])
    assert persona.VOCAB_RULE not in _prompt(vocab=[])


def test_the_mood_is_pushed_rather_than_merely_mentioned():
    """brainrot.PERSONAS' mood descriptions are vivid and were barely surfacing."""
    assert persona.MOOD_RULE in _prompt()


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
    assert persona.RELEVANCE_RULE in p
    low = p.lower()
    assert "on-topic" in low
    assert "never ignore what they said" in low


def test_relevance_outranks_the_style_rules():
    """Order is the whole point: when voice and relevance pull apart the model
    should already have read that relevance wins."""
    p = _prompt()
    assert p.index(persona.RELEVANCE_RULE) < p.index(persona.HOW_YOU_TEXT)


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
    assert persona.DAY_STATE_RULE in p
    low = p.lower()
    assert "don't force it" not in low
    assert "only if it actually connects to what they just said" in low


def test_the_day_state_rule_is_absent_when_there_is_no_day_state():
    assert persona.DAY_STATE_RULE not in _prompt()


def test_the_notes_block_is_conditional_too():
    assert persona.NOTES_RULE in _prompt(state={"notes": "their name is walter"})
    assert persona.NOTES_RULE not in _prompt()


# --- The kid is ADHD, and is not a nerd -------------------------------------
#
# Live, with the `nerd` mood: "you play any games?" -> "as per my research, 74%
# of gamers play fortnite, but like, what's your go-to game tho? 🗿". The mood
# is gone, but the model reaches for the statistics tic on its own, so it now
# has to be banned outright. And one 17-word message where a burst belonged:
# RELEVANCE_RULE claimed to win "whenever the two pull against each other",
# which was meant to settle topic disputes and was settling length ones too.

def test_the_statistics_tic_is_banned_outright():
    low = _prompt().lower()
    assert "as per my research" in low       # named, so it can be refused
    assert "percentage" in low
    assert "statistic" in low
    assert "studies show" in low


def test_correcting_and_explaining_are_banned_too():
    low = _prompt().lower()
    assert "never correct anyone" in low
    assert "never explain" in low


def test_the_kid_actually_reads_as_adhd():
    p = _prompt()
    assert persona.ADHD_RULE in p
    low = p.lower()
    assert "mid-sentence" in low                       # abandons thoughts
    assert "tangent" in low                            # derails
    assert "don't wait for the answer" in low          # asks and moves on
    assert "three messages ago" in low                 # circles back


def test_adhd_is_not_a_licence_to_go_off_topic():
    """The distinction that matters: react to what they said FIRST, then
    spiral. Opening on the tangent is the `u still on laundry duty fr` bug."""
    low = _prompt().lower()
    assert "react to what they said first" in low
    assert "then spiral" in low


def test_relevance_still_outranks_the_adhd_energy():
    p = _prompt()
    assert p.index(persona.RELEVANCE_RULE) < p.index(persona.ADHD_RULE)


def test_relevance_governs_the_subject_not_the_length():
    """`it wins whenever the two pull against each other` was overriding the
    under-10-words and separate-messages rules as well as the topic ones, and
    the kid sent a single 17-word message."""
    low = _prompt().lower()
    assert "wins whenever" not in low
    assert "subject" in low
    assert "never makes a message longer" in low
    assert "never merges your messages into one" in low


def test_the_burst_format_survives_the_adhd_rework():
    low = _prompt().lower()
    assert "under 10 words" in low
    assert "separate messages" in low
    assert "|||" in low


def test_the_memory_block_is_untouched_by_the_ban_on_facts():
    """`i dont need facts` is about fake statistics, not about recall -- the
    owner asked for memory explicitly."""
    p = _prompt(facts=["their name is walter", "works in IT and it drains them"])
    assert "WHAT YOU KNOW ABOUT THEM" in p
    assert "works in IT and it drains them" in p
    assert persona.FACTS_RULE in p


# --- It has to be allowed to not know things --------------------------------
#
# Live, the owner sent `fym gng sybau`. The kid replied `same lol whats sybau
# mean` and then `omg what's sybau like??` -- inventing a reaction to a word it
# had just admitted it didn't know -- and when pushed, insisted it did know.
# Being clueless is completely in character for a 14-year-old; bluffing is not,
# and it is the thing that reads as hallucination.

def test_the_prompt_forbids_faking_recognition():
    p = _prompt()
    assert persona.HONESTY_RULE in p
    low = p.lower()
    assert "never fake recognition" in low
    assert "never invent" in low


def test_not_knowing_is_offered_in_the_kids_own_voice():
    """A bare `admit it` produces an assistant apologising. It needs lines it
    can actually send."""
    low = _prompt().lower()
    assert "what does that even mean" in low
    assert "never heard of that" in low


def test_being_pushed_does_not_make_it_remember():
    low = _prompt().lower()
    assert "you still don't know" in low


def test_it_may_not_invent_facts_about_them_either():
    low = _prompt(facts=["their name is walter"]).lower()
    assert "they never said it" in low


def test_bond_line_changes_across_buckets():
    low = persona.bond_line(-50)
    mid = persona.bond_line(5)
    high = persona.bond_line(80)
    assert len({low, mid, high}) == 3


def test_bond_line_appears_in_the_prompt():
    assert persona.bond_line(80) in _prompt(state={"bond": 80})


def test_mood_rerolls_only_once_stale():
    rng = random.Random(0)
    fresh = {"mood_set_at": 1000.0}
    assert not persona.should_reroll_mood(fresh, 1000.0 + 60, rng=rng)
    assert persona.should_reroll_mood(fresh, 1000.0 + 48 * 3600, rng=rng)


def test_mood_rerolls_when_never_set():
    assert persona.should_reroll_mood({"mood_set_at": None}, 5.0, rng=random.Random(0))


# --- facts: the accumulating half of memory reaching the prompt -------------

def test_facts_reach_the_prompt_as_separate_lines():
    p = _prompt(facts=["their name is walter", "hates their job"])
    assert "- their name is walter" in p
    assert "- hates their job" in p


def test_the_facts_header_is_absent_when_there_are_none():
    p = _prompt(facts=[])
    assert "WHAT YOU KNOW ABOUT THEM" not in p
    assert persona.FACTS_RULE not in p


def test_the_kid_is_told_to_use_the_facts_not_just_hold_them():
    """Knowing things about someone and never acting on it is the same as not
    knowing them -- the old block was handed over bare."""
    p = _prompt(facts=["hates their job"])
    assert persona.FACTS_RULE in p
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
    assert persona.NOTES_RULE not in p


def test_a_legacy_notes_blob_still_shows_when_there_are_no_facts():
    """Chats whose notes predate the facts table must not go blank."""
    p = _prompt(state={"notes": "their name is walter"}, facts=[])
    assert "their name is walter" in p
    assert persona.NOTES_RULE in p


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
    recall.add_fact(1, "their name is walter")
    recall.add_fact(2, "their name is jesse")
    assert chat_engine._facts_for(db.get_chat_state(1)) == ["their name is walter"]


def test_facts_lookup_never_breaks_generation(monkeypatch):
    def boom(chat_id, limit=0):
        raise RuntimeError("db gone")

    monkeypatch.setattr(chat_engine.recall, "recent_facts", boom)
    assert chat_engine._facts_for({"chat_id": 1}) == []
    assert chat_engine._facts_for({}) == []
