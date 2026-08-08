"""The kid looking something up mid-reply.

Live, `fym gng sybau` came back as "same lol whats sybau mean" and then, one
message later, "omg what's sybau like??" — a reaction invented for a word it had
just admitted it did not know. HONESTY_RULE stops the bluff; this is the other
half, where it can actually go and find out.

Both seams are stubbed here: `chat_engine._complete` (Groq) and
`search.look_up` (the network). Nothing in this file touches the internet.
"""
import asyncio
import random

import chat_engine
import config
import db
import persona
import search


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "ce_search.db"))
    config.OUTBOUND_DAILY_BUDGET = 100
    config.WEB_SEARCH_ENABLED = True


def _run(coro):
    return asyncio.run(coro)


def _patch_groq(monkeypatch, *replies):
    """Stub Groq with a scripted sequence of turns, recording every call.

    Each element is either a string (the model answered) or a ToolCall (the
    model wants a lookup). The last element repeats if it runs out.
    """
    calls = []

    async def fake(messages, *, model, temperature, max_tokens, tools=None):
        calls.append({"messages": messages, "model": model, "tools": tools})
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(chat_engine, "_complete", fake)
    return calls


def _patch_search(monkeypatch, results=None, *, raises=False):
    seen = []

    async def fake(query, n=3):
        seen.append(query)
        if raises:
            raise RuntimeError("ddg exploded")
        return list(results or [])

    monkeypatch.setattr(search, "look_up", fake)
    return seen


SYBAU = [{"title": "SYBAU", "snippet": "shut yo bitch ass up", "url": "https://x.test/1"}]


def test_the_model_asking_for_a_lookup_runs_the_search(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _patch_groq(monkeypatch, chat_engine.ToolCall("sybau meaning slang"), "oh that ||| yeah ik that one")
    seen = _patch_search(monkeypatch, SYBAU)

    db.add_message(1, "user", "fym gng sybau")
    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert seen == ["sybau meaning slang"]
    assert [p.value for p in pieces] == ["oh that", "yeah ik that one"]


def test_the_looked_up_facts_reach_the_final_prompt(tmp_path, monkeypatch):
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, chat_engine.ToolCall("sybau"), "oh that")
    _patch_search(monkeypatch, SYBAU)

    db.add_message(1, "user", "fym gng sybau")
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert len(calls) == 2
    final_system = calls[1]["messages"][0]["content"]
    assert "shut yo bitch ass up" in final_system
    # the first round could not have carried them — nothing had been looked up
    assert "shut yo bitch ass up" not in calls[0]["messages"][0]["content"]


def test_the_final_prompt_forbids_ever_admitting_the_search(tmp_path, monkeypatch):
    """A 14-year-old does not say "let me search that for you". The facts have
    to arrive as something it simply knows."""
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, chat_engine.ToolCall("sybau"), "oh that")
    _patch_search(monkeypatch, SYBAU)

    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    final_system = calls[1]["messages"][0]["content"].lower()
    assert "did not look it up" in final_system
    assert "searched" in final_system and "googled" in final_system


def test_a_search_that_finds_nothing_still_produces_a_reply_and_invents_nothing(tmp_path, monkeypatch):
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, chat_engine.ToolCall("sybau"), "bro idk what that even means 💀")
    _patch_search(monkeypatch, [])

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["bro idk what that even means 💀"]
    # no facts, so no facts block — the don't-bluff rule is what's left standing
    final_system = calls[1]["messages"][0]["content"]
    assert "STUFF YOU ALREADY KNOW" not in final_system
    assert "NEVER fake recognition" in final_system


JUNK = [{"title": "Top 10 Gen Z Slang Words of 2026 (You Won't BELIEVE #7)",
         "snippet": "Sign up for our newsletter to keep up with the latest trends! Click here.",
         "url": "https://seo.test/1"}]


def test_junk_results_do_not_override_the_kids_right_to_not_know(tmp_path, monkeypatch):
    """Results ARRIVING is not results ANSWERING.

    This asserts the instruction, not the model's behaviour — the seam is
    stubbed, so nothing here can prove what llama does with it. What it does
    prove is that a reply built on junk still carries both halves: the rule
    saying throw it away if it doesn't answer, and HONESTY_RULE underneath.
    A regression that dropped either would land silently otherwise.
    """
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, chat_engine.ToolCall("sybau"), "bro idk what that even means 💀")
    _patch_search(monkeypatch, JUNK)

    db.add_message(1, "user", "fym gng sybau")
    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["bro idk what that even means 💀"]
    final = calls[1]["messages"][0]["content"]
    assert "NEVER fake recognition" in final          # HONESTY_RULE still standing
    low = final.lower()
    assert "still don't know" in low                  # and the judgement clause on top
    assert "spam" in low and "contradicts itself" in low


def test_a_search_that_raises_still_produces_a_reply(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _patch_groq(monkeypatch, chat_engine.ToolCall("sybau"), "never heard of that")
    _patch_search(monkeypatch, raises=True)

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))
    assert [p.value for p in pieces] == ["never heard of that"]


def test_at_most_one_lookup_per_reply(tmp_path, monkeypatch):
    """The model asking again on the second round must not start a chain."""
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, chat_engine.ToolCall("sybau"), chat_engine.ToolCall("sybau again"))
    seen = _patch_search(monkeypatch, SYBAU)

    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert seen == ["sybau"]
    assert len(calls) == 2
    assert calls[1]["tools"] is None      # the second round has no tool to reach for


def test_a_reply_that_needs_no_lookup_costs_one_round_trip(tmp_path, monkeypatch):
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, "yo ||| wsp")
    seen = _patch_search(monkeypatch, SYBAU)

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["yo", "wsp"]
    assert len(calls) == 1 and seen == []


def test_a_reply_is_offered_the_tool(tmp_path, monkeypatch):
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, "yo")
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))
    names = [t["function"]["name"] for t in calls[0]["tools"]]
    # Membership, not equality: `remember` is offered alongside this one, and
    # which other tools exist is not what this test is about.
    assert search.TOOL_NAME in names


def test_pings_and_cold_opens_get_no_tool(tmp_path, monkeypatch):
    """Neither is answering a question, so neither has anything to look up."""
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, "yo")
    _run(chat_engine.ping(1, db.get_chat_state(1), 1, rng=random.Random(0)))
    _run(chat_engine.cold_open(1, db.get_chat_state(1), rng=random.Random(0)))
    assert [c["tools"] for c in calls] == [None, None]


def test_the_tool_is_withheld_when_web_search_is_switched_off(tmp_path, monkeypatch):
    _fresh(tmp_path)
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", False)
    calls = _patch_groq(monkeypatch, "yo")
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))
    # The switch withholds THIS tool; `remember` has its own and is unaffected.
    names = [t["function"]["name"] for t in calls[0]["tools"] or []]
    assert search.TOOL_NAME not in names


# --- The trigger: availability is not motivation ----------------------------

def test_the_kid_is_told_to_look_it_up_before_it_is_told_it_can_not_know(tmp_path, monkeypatch):
    """The live failure was the tool being offered but unmotivated.

    `yo what does sybau mean` -> "bro what's sybau even mean 💀 / never heard
    of that lol", and the tool was never called. HONESTY_RULE hands the model a
    clean, in-character way to not know, so it takes it and stops there.
    Something has to say look FIRST, and it has to outrank the rule it is
    short-circuiting — so the ORDER is asserted, not just the presence.
    """
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, "yo")
    db.add_message(1, "user", "yo what does sybau mean")
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    system = calls[0]["messages"][0]["content"]
    assert search.LOOKUP_RULE in system
    assert system.index(search.LOOKUP_RULE) < system.index("WHEN YOU DON'T KNOW SOMETHING")


def test_the_second_round_is_not_told_to_look_it_up_again(tmp_path, monkeypatch):
    """It has no tool on the second round, so "look it up" would be an
    instruction it cannot carry out. JUDGE_IT and HONESTY_RULE cover it."""
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, chat_engine.ToolCall("sybau"), "oh that")
    _patch_search(monkeypatch, SYBAU)
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert search.LOOKUP_RULE not in calls[1]["messages"][0]["content"]


def test_the_trigger_is_withheld_when_web_search_is_switched_off(tmp_path, monkeypatch):
    """No tool means the instruction would be a lie. HONESTY_RULE alone then."""
    _fresh(tmp_path)
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", False)
    calls = _patch_groq(monkeypatch, "yo")
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))
    assert search.LOOKUP_RULE not in calls[0]["messages"][0]["content"]


def test_pings_and_cold_opens_are_not_told_to_look_things_up(tmp_path, monkeypatch):
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, "yo")
    _run(chat_engine.ping(1, db.get_chat_state(1), 1, rng=random.Random(0)))
    _run(chat_engine.cold_open(1, db.get_chat_state(1), rng=random.Random(0)))
    assert all(search.LOOKUP_RULE not in c["messages"][0]["content"] for c in calls)


# --- The closing line vetoing every rule above it ---------------------------

def test_the_closing_line_permits_a_tool_call_when_a_tool_is_offered(tmp_path, monkeypatch):
    """The bug that made JUDGE_IT, LOOKUP_RULE and the ordering all moot.

    "Output ONLY the messages, separated by |||" was correct when text was the
    only output and silently became a veto the moment tools existed: a model
    reads it literally and will not emit a function call under it. Live, with
    the full prompt, Gemini answered `whats the weather in tashkent rn` by
    TYPING "look it up" as a message and then inventing a temperature. Drop the
    line and the same prompt calls the tool.

    No stubbed test could have caught it — the provider is always faked, so what
    a real model does with the assembled prompt is exactly what is never run.
    The suite can only pin the wording; see the report for the live check.
    """
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, "yo")
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    system = calls[0]["messages"][0]["content"]
    assert persona.CLOSING_WITH_TOOLS in system
    assert persona.CLOSING_STRICT not in system


def test_the_closing_line_stays_strict_when_no_tool_is_offered(tmp_path, monkeypatch):
    """Pings and cold opens carry no tool, and the strict line does real work
    there against preamble and self-narration. It only loosens where it must."""
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, "yo")
    _run(chat_engine.ping(1, db.get_chat_state(1), 1, rng=random.Random(0)))
    _run(chat_engine.cold_open(1, db.get_chat_state(1), rng=random.Random(0)))
    for c in calls:
        assert persona.CLOSING_STRICT in c["messages"][0]["content"]
        assert persona.CLOSING_WITH_TOOLS not in c["messages"][0]["content"]


def test_the_second_round_gets_the_strict_closing_line(tmp_path, monkeypatch):
    """It has no tools on the second round, so there is nothing to permit."""
    _fresh(tmp_path)
    calls = _patch_groq(monkeypatch, chat_engine.ToolCall("sybau"), "oh that")
    _patch_search(monkeypatch, SYBAU)
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))
    assert persona.CLOSING_STRICT in calls[1]["messages"][0]["content"]


def test_every_offered_tool_is_covered_not_just_the_lookup(tmp_path, monkeypatch):
    """`remember` was vetoed by the same line. Keying the fix off can_look_up
    alone would have left recall's tool broken with web search switched off."""
    _fresh(tmp_path)
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", False)
    monkeypatch.setattr(config, "RECALL_ENABLED", True)
    calls = _patch_groq(monkeypatch, "yo")
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    system = calls[0]["messages"][0]["content"]
    assert search.LOOKUP_RULE not in system          # no web tool, so no trigger
    assert persona.CLOSING_WITH_TOOLS in system      # but `remember` still needs the permission
