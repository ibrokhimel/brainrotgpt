"""Routing between the two providers, and what happens when Gemini fails.

`reply` — the one call the owner actually reads — goes to Gemini; pings, cold
opens and everything else in the background stay on Groq. Every way Gemini can
fail ends with the kid still texting back, and says why in the log.

The translation layer and the client itself are in test_gemini.py. Nothing here
touches the network: `gemini.complete` and `chat_engine._complete` are both
stubbed, exactly as the rest of the suite stubs the Groq seam.
"""
import asyncio
import random

import chat_engine
import config
import db
import gemini
import search


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "gem.db"))
    config.OUTBOUND_DAILY_BUDGET = 100


def _run(coro):
    return asyncio.run(coro)


def _key(monkeypatch, value="test-gemini-key"):
    monkeypatch.setattr(config, "GEMINI_API_KEY", value)
    monkeypatch.setattr(config, "GEMINI_ENABLED", True)


def _patch_groq(monkeypatch, *replies):
    calls = []

    async def fake(messages, *, model, temperature, max_tokens, tools=None):
        calls.append({"messages": messages, "model": model, "tools": tools})
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(chat_engine, "_complete", fake)
    return calls


def _patch_gemini(monkeypatch, *replies, raises=None):
    calls = []

    async def fake(messages, *, temperature, max_tokens, tools=None):
        calls.append({"messages": messages, "tools": tools})
        if raises is not None:
            raise raises
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(gemini, "complete", fake)
    return calls


# --- routing --------------------------------------------------------------

def test_without_a_key_a_reply_goes_to_groq_exactly_as_before(tmp_path, monkeypatch):
    _fresh(tmp_path)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    calls = _patch_groq(monkeypatch, "yo ||| wsp")
    seen = _patch_gemini(monkeypatch, "never called")

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["yo", "wsp"]
    assert len(calls) == 1 and seen == []
    assert calls[0]["model"] == config.GROQ_MODEL


def test_with_a_key_a_reply_goes_to_gemini(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    groq = _patch_groq(monkeypatch, "groq answered")
    seen = _patch_gemini(monkeypatch, "nah thats crazy 💀 ||| fr")

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["nah thats crazy 💀", "fr"]
    assert len(seen) == 1 and groq == []


def test_a_reply_through_gemini_still_carries_the_kid_prompt(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    _patch_groq(monkeypatch, "groq answered")
    seen = _patch_gemini(monkeypatch, "yo")

    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    system = seen[0]["messages"][0]["content"]
    assert "Jayden" in system and "NEVER fake recognition" in system


def test_pings_stay_on_groq_even_with_a_key(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    calls = _patch_groq(monkeypatch, "yo")
    seen = _patch_gemini(monkeypatch, "never called")

    _run(chat_engine.ping(1, db.get_chat_state(1), 1, rng=random.Random(0)))

    assert len(calls) == 1 and seen == []
    assert calls[0]["model"] == config.GROQ_FALLBACK_MODEL


def test_cold_opens_stay_on_groq_even_with_a_key(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    calls = _patch_groq(monkeypatch, "yo")
    seen = _patch_gemini(monkeypatch, "never called")

    _run(chat_engine.cold_open(1, db.get_chat_state(1), rng=random.Random(0)))

    assert len(calls) == 1 and seen == []
    assert calls[0]["model"] == config.GROQ_FALLBACK_MODEL


# --- every way gemini can fail ends with the kid still texting ------------

def test_a_gemini_error_falls_back_to_groq(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    calls = _patch_groq(monkeypatch, "yo ||| wsp")
    _patch_gemini(monkeypatch, raises=RuntimeError("429 resource exhausted"))

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["yo", "wsp"]
    assert len(calls) == 1


def test_a_gemini_timeout_falls_back_to_groq(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    calls = _patch_groq(monkeypatch, "still here")
    _patch_gemini(monkeypatch, raises=TimeoutError())

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["still here"]
    assert len(calls) == 1


def test_gemini_answering_with_nothing_falls_back_to_groq(tmp_path, monkeypatch):
    """A safety block comes back as a 200 with no text. That is a failure too."""
    _fresh(tmp_path)
    _key(monkeypatch)
    calls = _patch_groq(monkeypatch, "still here")
    _patch_gemini(monkeypatch, "   ")

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["still here"]
    assert len(calls) == 1


def test_the_fallback_reply_is_not_budgeted(tmp_path, monkeypatch):
    """Replies to real users are never budgeted, whichever provider answers."""
    _fresh(tmp_path)
    _key(monkeypatch)
    monkeypatch.setattr(config, "OUTBOUND_DAILY_BUDGET", 0)
    _patch_groq(monkeypatch, "yo")
    _patch_gemini(monkeypatch, raises=RuntimeError("dead"))
    assert _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))


# --- the fallback has to SAY why ------------------------------------------
#
# Live, the owner's key turned out to be capped at 20 requests/day and was
# exhausted, so every reply 429'd and fell back — and the log said
# `gemini failed () — falling back to groq`, because a bare TimeoutError renders
# as the empty string under %s and the deadline was firing too. Finding it took
# reproducing the call by hand. A fallback log that omits the one thing you went
# to it for is worse than no log.

def _fallback_logs(monkeypatch, caplog, exc) -> list[str]:
    gemini._logged.clear()
    calls = []

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        calls.append(1)
        return "yo"

    async def dead(messages, *, temperature, max_tokens, tools=None):
        raise exc

    monkeypatch.setattr(gemini, "complete", dead)
    with caplog.at_level("WARNING", logger="brainrotgpt.gemini"):
        _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10))
    assert calls == [1]          # and it still answered
    return [r.getMessage() for r in caplog.records]


def test_the_fallback_log_names_an_exception_with_no_message(monkeypatch, caplog):
    """asyncio's deadline raises a bare TimeoutError — empty under %s."""
    _key(monkeypatch)
    [line] = _fallback_logs(monkeypatch, caplog, TimeoutError())
    assert "TimeoutError" in line


def test_the_fallback_log_carries_the_api_error_text(monkeypatch, caplog):
    _key(monkeypatch)
    [line] = _fallback_logs(monkeypatch, caplog, RuntimeError("429 RESOURCE_EXHAUSTED limit: 20"))
    assert "429 RESOURCE_EXHAUSTED limit: 20" in line


def test_a_persistent_failure_stops_repeating_itself(monkeypatch, caplog):
    """An exhausted quota fails on EVERY reply. One line per reply buries the log."""
    _key(monkeypatch)
    gemini._logged.clear()

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    async def dead(messages, *, temperature, max_tokens, tools=None):
        raise RuntimeError("429 RESOURCE_EXHAUSTED limit: 20")

    monkeypatch.setattr(gemini, "complete", dead)
    call = gemini.first(groq)
    with caplog.at_level("WARNING", logger="brainrotgpt.gemini"):
        for _ in range(5):
            _run(call([], model="m", temperature=1.0, max_tokens=10))

    assert len(caplog.records) == 1


def test_a_different_failure_is_never_swallowed_by_the_throttle(monkeypatch, caplog):
    """Throttling one condition must not hide the next one turning up."""
    _key(monkeypatch)
    gemini._logged.clear()
    errs = [RuntimeError("429 RESOURCE_EXHAUSTED"), RuntimeError("429 RESOURCE_EXHAUSTED"),
            RuntimeError("404 NOT_FOUND no longer available")]

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    async def dead(messages, *, temperature, max_tokens, tools=None):
        raise errs.pop(0)

    monkeypatch.setattr(gemini, "complete", dead)
    call = gemini.first(groq)
    with caplog.at_level("WARNING", logger="brainrotgpt.gemini"):
        for _ in range(3):
            _run(call([], model="m", temperature=1.0, max_tokens=10))

    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    assert "429" in lines[0] and "404" in lines[1]


def test_the_throttle_reopens_once_the_window_passes(monkeypatch, caplog):
    _key(monkeypatch)
    gemini._logged.clear()
    monkeypatch.setattr(gemini, "FALLBACK_LOG_EVERY_S", 0)

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    async def dead(messages, *, temperature, max_tokens, tools=None):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(gemini, "complete", dead)
    call = gemini.first(groq)
    with caplog.at_level("WARNING", logger="brainrotgpt.gemini"):
        for _ in range(3):
            _run(call([], model="m", temperature=1.0, max_tokens=10))

    assert len(caplog.records) == 3


def test_an_empty_answer_is_logged_and_throttled_too(monkeypatch, caplog):
    _key(monkeypatch)
    gemini._logged.clear()

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    async def blank(messages, *, temperature, max_tokens, tools=None):
        return "  "

    monkeypatch.setattr(gemini, "complete", blank)
    call = gemini.first(groq)
    with caplog.at_level("WARNING", logger="brainrotgpt.gemini"):
        for _ in range(3):
            _run(call([], model="m", temperature=1.0, max_tokens=10))

    assert len(caplog.records) == 1
    assert "nothing" in caplog.records[0].getMessage()


def test_the_throttle_cannot_grow_without_bound(monkeypatch, caplog):
    """Every failure is a distinct key; the log-state dict must not be a leak."""
    _key(monkeypatch)
    gemini._logged.clear()

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    n = [0]

    async def dead(messages, *, temperature, max_tokens, tools=None):
        n[0] += 1
        raise RuntimeError(f"distinct failure {n[0]}")

    monkeypatch.setattr(gemini, "complete", dead)
    call = gemini.first(groq)
    with caplog.at_level("WARNING", logger="brainrotgpt.gemini"):
        for _ in range(200):
            _run(call([], model="m", temperature=1.0, max_tokens=10))

    assert len(gemini._logged) <= gemini.FALLBACK_LOG_KEYS


# --- a tool call round-tripping through gemini ----------------------------

def test_a_tool_call_round_trips_through_gemini(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", True)
    groq = _patch_groq(monkeypatch, "groq answered")
    seen = _patch_gemini(monkeypatch,
                         gemini.ToolCall("sybau meaning slang", search.TOOL_NAME),
                         "oh that ||| yeah ik that one")

    looked_up = []

    async def fake_look_up(query, n=3):
        looked_up.append(query)
        return [{"title": "SYBAU", "snippet": "shut yo bitch ass up", "url": ""}]

    monkeypatch.setattr(search, "look_up", fake_look_up)

    db.add_message(1, "user", "fym gng sybau")
    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert looked_up == ["sybau meaning slang"]
    assert [p.value for p in pieces] == ["oh that", "yeah ik that one"]
    assert len(seen) == 2 and groq == []
    # the first round was offered the tool, the second was not — no chaining
    assert search.TOOL_NAME in [t["function"]["name"] for t in seen[0]["tools"]]
    assert seen[1]["tools"] is None
    # and the looked-up fact reached the second prompt
    assert "shut yo bitch ass up" in seen[1]["messages"][0]["content"]


def test_gemini_dying_on_the_second_round_still_answers(tmp_path, monkeypatch):
    """The tool ran; the burst must still arrive, from Groq if it has to."""
    _fresh(tmp_path)
    _key(monkeypatch)
    calls = []

    async def flaky(messages, *, temperature, max_tokens, tools=None):
        calls.append(tools)
        if len(calls) == 1:
            return gemini.ToolCall("sybau", search.TOOL_NAME)
        raise RuntimeError("gone")

    monkeypatch.setattr(gemini, "complete", flaky)
    groq = _patch_groq(monkeypatch, "never heard of that")

    async def fake_look_up(query, n=3):
        return []

    monkeypatch.setattr(search, "look_up", fake_look_up)

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["never heard of that"]
    assert len(groq) == 1
