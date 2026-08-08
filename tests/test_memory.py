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


# --- the extractor must only ever read the OTHER person's words -------------
#
# Live, every fact stored for the owner's chat was extracted from the KID's own
# messages and attributed to the owner:
#
#     They are in a "locked in mode"        <- the kid said "stay locked in"
#     They have a laundry task              <- the kid's own day_state
#     They take a cold plunge every morning <- invented by the sigma mood
#     They sent a sticker: 💪               <- the kid's own emoji
#
# transcript() renders both sides, and the kid sends two or three messages per
# turn against the user's one, so the window handed to the extractor was ~80%
# bot output. Those facts then came back as WHAT YOU KNOW ABOUT THEM and the kid
# doubled down: "yo whats up" -> "u still on laundry duty fr 🗿". A closed loop
# amplifying its own hallucinations, which is why memory made the bot worse.

def _capture(monkeypatch, reply="NONE"):
    seen = {}

    async def fake(prompt):
        seen["prompt"] = prompt
        return reply

    monkeypatch.setattr(memory, "_ask", fake)
    return seen


def test_user_transcript_contains_only_the_users_own_lines(tmp_path):
    _fresh(tmp_path)
    db.add_message(1, "user", "im walter")
    db.add_message(1, "kid", "stay locked in bro")
    db.add_message(1, "user", "i hate my job")
    out = memory.user_transcript(1)
    assert "im walter" in out and "i hate my job" in out
    assert "locked in" not in out
    assert "me:" not in out


def test_the_user_window_is_not_eaten_by_the_kids_own_messages(tmp_path):
    """The kid sends two or three messages per turn to the user's one, so a
    mixed window of N is mostly bot output. Filtering has to happen in the
    query, not after the rows come back."""
    _fresh(tmp_path)
    db.add_message(1, "user", "the oldest thing i said")
    for i in range(50):
        db.add_message(1, "kid", f"kid line {i}")
    db.add_message(1, "user", "the newest thing i said")
    out = memory.user_transcript(1, limit=5)
    assert "the oldest thing i said" in out
    assert "the newest thing i said" in out
    assert "kid line" not in out


def test_the_kids_own_lines_never_reach_the_extractor(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.add_message(1, "user", "im walter")
    db.add_message(1, "kid", "discipline over feelings no cap 💪")
    db.add_message(1, "kid", "cold plunge every morning fr")
    seen = _capture(monkeypatch)
    _run(memory.distill(1, db.get_chat_state(1)))

    prompt = seen["prompt"]
    assert "im walter" in prompt
    assert "discipline over feelings" not in prompt
    assert "cold plunge" not in prompt


def test_facts_still_come_from_the_users_lines_in_the_same_transcript(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.add_message(1, "user", "im walter and i hate my job")
    db.add_message(1, "kid", "stay locked in bro 💪")
    _capture(monkeypatch, "their name is walter\nhates their job")
    _run(memory.distill(1, db.get_chat_state(1)))

    assert {f["fact"] for f in db.recent_facts(1)} == {"their name is walter",
                                                       "hates their job"}


def test_the_kids_day_state_never_becomes_a_fact_about_them(tmp_path, monkeypatch):
    """life.current() is the KID's day. "They have a laundry task" is how the
    owner ended up being told they were on laundry duty."""
    _fresh(tmp_path)
    db.add_message(1, "user", "yo whats up")
    db.add_message(1, "kid", "folding laundry all day mom took my phone")
    seen = _capture(monkeypatch)
    _run(memory.distill(1, db.get_chat_state(1)))

    assert "laundry" not in seen["prompt"]
    assert db.recent_facts(1) == []


def test_the_extractor_is_told_the_lines_are_the_other_persons_only(tmp_path, monkeypatch):
    """Second line of defence behind the filtering: the prompt has to say whose
    words these are, and that the teenager's own life is never a fact."""
    _fresh(tmp_path)
    db.add_message(1, "user", "hi")
    seen = _capture(monkeypatch)
    _run(memory.distill(1, db.get_chat_state(1)))

    low = seen["prompt"].lower()
    assert "person's own words" in low
    assert "the teenager's replies are not included" in low
    assert "stated about themselves" in low
    assert "never invent" in low
    assert "is not a fact about this person" in low


# --- durable facts only: the live DB filled up with events ------------------
#
# Stored alongside the real facts: "I sent a 💪 sticker", "I sent a 🌟 sticker",
# "I'm back after being away". None of those stay true about a person, and each
# one displaces something that does out of the forty-slot window.

def test_a_sticker_is_not_a_fact(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.add_message(1, "user", "im walter")
    monkeypatch.setattr(memory, "_ask", lambda p: _done(
        "their name is walter\nsent a 💪 sticker\nI sent a 🌟 sticker"))
    _run(memory.distill(1, db.get_chat_state(1)))
    assert [f["fact"] for f in db.recent_facts(1)] == ["their name is walter"]


def test_an_event_is_not_a_fact(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.add_message(1, "user", "im walter")
    monkeypatch.setattr(memory, "_ask", lambda p: _done(
        "they are back after being away\njust said hi\nasked how school was\n"
        "works in IT"))
    _run(memory.distill(1, db.get_chat_state(1)))
    assert [f["fact"] for f in db.recent_facts(1)] == ["works in IT"]


def test_the_durability_filter_keeps_real_facts_about_a_person(tmp_path, monkeypatch):
    """The filter is aimed at events, not at anything that happens to mention a
    verb. Over-filtering loses memory, which is the thing being protected."""
    _fresh(tmp_path)
    db.add_message(1, "user", "hi")
    monkeypatch.setattr(memory, "_ask", lambda p: _done(
        "their name is walter\nworks in IT and it drains them\nplays minecraft\n"
        "lives in tashkent\nkeeps complaining about their boss\n"
        "is saving up for a new pc"))
    _run(memory.distill(1, db.get_chat_state(1)))
    assert len(db.recent_facts(1)) == 6


def test_a_pass_of_pure_junk_is_treated_as_nothing_to_record(tmp_path, monkeypatch):
    """Filtered down to empty is a NONE, not a success -- the notes blob must
    not be overwritten with nothing and the counter must back off."""
    _fresh(tmp_path)
    db.update_chat_state(1, notes="their name is walter", msgs_since_notes=20)
    db.add_message(1, "user", "hi")
    monkeypatch.setattr(memory, "_ask", lambda p: _done("sent a 🌟 sticker"))
    assert _run(memory.distill(1, db.get_chat_state(1))) == "their name is walter"
    assert db.recent_facts(1) == []


def test_the_extractor_is_told_to_record_only_durable_things(tmp_path, monkeypatch):
    _fresh(tmp_path)
    db.add_message(1, "user", "hi")
    seen = _capture(monkeypatch)
    _run(memory.distill(1, db.get_chat_state(1)))

    low = seen["prompt"].lower()
    assert "stays true" in low
    assert "not what they just did" in low
    assert "sticker" in low


def test_the_extractor_is_asked_for_one_consistent_voice(tmp_path, monkeypatch):
    """Half the live facts came back quoting the person ("I work in IT") and
    half describing them ("They work in IT."). db._normalise_fact now folds the
    two together, but the cheaper fix is not to produce both in the first
    place."""
    _fresh(tmp_path)
    db.add_message(1, "user", "hi")
    seen = _capture(monkeypatch)
    _run(memory.distill(1, db.get_chat_state(1)))

    low = seen["prompt"].lower()
    assert "third person" in low
    assert "never write \"i\" or \"my\"" in low


def test_a_chat_with_only_kid_messages_never_asks_the_model(tmp_path, monkeypatch):
    """A cold open they never answered. There is nothing to know, and asking
    anyway is exactly how the kid's own monologue became facts about them."""
    _fresh(tmp_path)
    db.add_message(1, "kid", "yo u up")
    db.add_message(1, "kid", "stay locked in")

    async def never(prompt):
        raise AssertionError("handed the model the kid's own monologue")

    monkeypatch.setattr(memory, "_ask", never)
    _run(memory.distill(1, db.get_chat_state(1)))

    assert db.recent_facts(1) == []
    # Nothing yet, not a failure: back off a few messages, don't burn the cycle.
    assert db.get_chat_state(1)["msgs_since_notes"] == memory.NOTES_EVERY - memory.NONE_BACKOFF
