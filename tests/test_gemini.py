"""The Gemini provider itself: the switch, the translation layer, the client.

search.TOOL and recall.TOOL are written OpenAI-style because Groq speaks
OpenAI. Gemini does not — it wants `function_declarations` with an uppercase
`type` enum and answers with a `functionCall` part — so the two halves of that
mapping, `_declarations` and `_read`, are what most of this file is about. They
are pure, and tested as such.

Which provider gets which call, and what happens when Gemini fails, is in
test_gemini_routing.py. Nothing here touches the network: `_get_client` is
stubbed wherever a client is needed at all.
"""
import asyncio

import config
import gemini
import search


def _run(coro):
    return asyncio.run(coro)


def _key(monkeypatch, value="test-gemini-key"):
    monkeypatch.setattr(config, "GEMINI_API_KEY", value)
    monkeypatch.setattr(config, "GEMINI_ENABLED", True)


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

