"""Gemini on the replies a person actually reads.

Two things are under test here and they are deliberately separate. The first
half is the translation layer — OpenAI-shaped tool schemas in, Gemini
`function_declarations` out, and a `functionCall` part read back — which is pure
and needs no client at all. The second half is the routing: `reply` goes to
Gemini, everything background stays on Groq, and every way Gemini can fail ends
with the kid still texting back.

Nothing in this file touches the network. `gemini._get_client` is stubbed where
a client is needed at all, and `chat_engine._complete` (Groq) is stubbed exactly
as the rest of the suite stubs it.
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


# --- the switch -----------------------------------------------------------

def test_no_key_means_the_whole_feature_is_off(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_ENABLED", True)
    assert gemini.enabled() is False


def test_a_key_turns_it_on(monkeypatch):
    _key(monkeypatch)
    assert gemini.enabled() is True


def test_the_switch_beats_the_key(monkeypatch):
    _key(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_ENABLED", False)
    assert gemini.enabled() is False


# --- translation: OpenAI tool schemas -> Gemini function declarations -----

def test_the_web_tool_translates_into_a_gemini_declaration():
    [decl] = gemini._declarations([search.TOOL])
    assert decl["name"] == search.TOOL_NAME
    assert "look something up" in decl["description"].lower()
    params = decl["parameters"]
    # Gemini's `type` is an enum, not a json-schema string
    assert params["type"] == "OBJECT"
    assert params["properties"]["query"]["type"] == "STRING"
    assert params["required"] == ["query"]


def test_translation_is_tool_count_agnostic():
    """Another agent is adding a second tool; this must not care how many."""
    second = {"type": "function", "function": {
        "name": "remember", "description": "dig it up",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}}
    names = [d["name"] for d in gemini._declarations([search.TOOL, second])]
    assert names == [search.TOOL_NAME, "remember"]
    assert gemini._declarations([]) == []
    assert gemini._declarations(None) == []


def test_a_nameless_tool_is_dropped_rather_than_raising():
    assert gemini._declarations([{"type": "function", "function": {}}]) == []


# --- translation: reading a Gemini response back --------------------------

class _Part:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class _Call:
    def __init__(self, name, args):
        self.name, self.args = name, args


class _Resp:
    def __init__(self, *parts):
        content = type("C", (), {"parts": list(parts)})()
        self.candidates = [type("Cand", (), {"content": content})()]


def test_plain_text_comes_back_as_text():
    assert gemini._read(_Resp(_Part(text="yo ||| wsp")), frozenset()) == "yo ||| wsp"


def test_a_function_call_part_comes_back_as_a_tool_call():
    resp = _Resp(_Part(function_call=_Call(search.TOOL_NAME, {"query": "sybau meaning"})))
    out = gemini._read(resp, frozenset([search.TOOL_NAME]))
    assert isinstance(out, gemini.ToolCall)
    assert (out.query, out.name) == ("sybau meaning", search.TOOL_NAME)


def test_a_call_naming_a_tool_that_was_not_offered_is_ignored():
    resp = _Resp(_Part(text="hi"), _Part(function_call=_Call("rm_rf", {"query": "x"})))
    assert gemini._read(resp, frozenset([search.TOOL_NAME])) == "hi"


def test_a_blank_query_is_not_a_tool_call():
    resp = _Resp(_Part(function_call=_Call(search.TOOL_NAME, {"query": "  "})))
    assert gemini._read(resp, frozenset([search.TOOL_NAME])) == ""


def test_an_empty_response_reads_as_empty_not_an_exception():
    empty = type("R", (), {"candidates": []})()
    assert gemini._read(empty, frozenset()) == ""


# --- the client seam ------------------------------------------------------

def _stub_client(monkeypatch, resp):
    seen = {}

    async def generate_content(*, model, contents, config):  # noqa: A002
        seen.update(model=model, contents=contents, config=config)
        return resp

    models = type("M", (), {"generate_content": staticmethod(generate_content)})()
    client = type("C", (), {"aio": type("A", (), {"models": models})()})()
    monkeypatch.setattr(gemini, "_get_client", lambda: client)
    return seen


def test_complete_sends_the_system_prompt_as_a_system_instruction(monkeypatch):
    _key(monkeypatch)
    seen = _stub_client(monkeypatch, _Resp(_Part(text="yo")))
    msgs = [{"role": "system", "content": "you are jayden"},
            {"role": "user", "content": "wsp"}]

    out = _run(gemini.complete(msgs, temperature=1.05, max_tokens=400, tools=[search.TOOL]))

    assert out == "yo"
    assert seen["model"] == config.GEMINI_MODEL
    assert seen["contents"] == "wsp"
    assert seen["config"].system_instruction == "you are jayden"
    assert seen["config"].temperature == 1.05
    assert seen["config"].max_output_tokens == 400
    [tool] = seen["config"].tools
    assert [d.name for d in tool.function_declarations] == [search.TOOL_NAME]


def test_complete_offers_no_tools_when_given_none(monkeypatch):
    _key(monkeypatch)
    seen = _stub_client(monkeypatch, _Resp(_Part(text="yo")))
    _run(gemini.complete([{"role": "user", "content": "wsp"}],
                         temperature=1.0, max_tokens=120))
    assert not seen["config"].tools


def test_complete_does_not_pay_for_thinking(monkeypatch):
    """Thinking tokens are seconds the 12s reply budget does not have."""
    _key(monkeypatch)
    seen = _stub_client(monkeypatch, _Resp(_Part(text="yo")))
    _run(gemini.complete([{"role": "user", "content": "wsp"}],
                         temperature=1.0, max_tokens=120))
    assert seen["config"].thinking_config.thinking_budget == 0


def test_complete_gives_up_rather_than_hanging_past_the_deadline(monkeypatch):
    _key(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_TIMEOUT_S", 0.01)

    async def never(*, model, contents, config):  # noqa: A002
        await asyncio.sleep(5)

    models = type("M", (), {"generate_content": staticmethod(never)})()
    client = type("C", (), {"aio": type("A", (), {"models": models})()})()
    monkeypatch.setattr(gemini, "_get_client", lambda: client)

    try:
        _run(gemini.complete([{"role": "user", "content": "wsp"}],
                             temperature=1.0, max_tokens=120))
    except TimeoutError:
        return
    raise AssertionError("expected the deadline to fire")


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
