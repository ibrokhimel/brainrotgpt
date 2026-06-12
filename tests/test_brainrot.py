import asyncio

import pytest

import brainrot

SETTINGS = {"persona": "random", "intensity": "medium", "tone": "default",
            "language": "auto", "candidates": 1}


class _Msg:
    def __init__(self, c):
        self.content = c


class _Choice:
    def __init__(self, c):
        self.message = _Msg(c)


class _Usage:
    total_tokens = 42
    completion_tokens = 20


class _Resp:
    def __init__(self, c):
        self.choices = [_Choice(c)]
        self.usage = _Usage()


def _patch(monkeypatch, content="brainrot reply 🗿"):
    async def fake_create(**kwargs):
        return _Resp(content)
    monkeypatch.setattr(brainrot._client.chat.completions, "create", fake_create)


def test_choose_persona_pinned():
    s = dict(SETTINGS, persona="gym_sigma")
    assert brainrot.choose_persona(s)[0] == "gym_sigma"


def test_choose_persona_random_avoids():
    # avoid a key 200x; it should never come back
    for _ in range(200):
        p = brainrot.choose_persona(SETTINGS, avoid_persona="gym_sigma")
        assert p[0] != "gym_sigma"


def test_distinct_personas_are_distinct():
    ps = brainrot._distinct_personas(SETTINGS, 4, None)
    keys = [p[0] for p in ps]
    assert len(keys) == len(set(keys)) == 4


def test_system_prompt_includes_flavor():
    persona = brainrot.PERSONA_BY_KEY["corporate_memo"]
    prompt = brainrot._build_system_prompt(persona, dict(SETTINGS, intensity="unhinged"))
    assert "CORPORATE" in prompt
    assert "MAXIMUM length" in prompt  # unhinged intensity line
    assert "VOCAB" in prompt


def test_user_content_wraps_and_avoids():
    content = brainrot._build_user_content("hello there", avoid_text="old reply")
    assert "CONVERSATION" in content
    assert "REGENERATE" in content and "old reply" in content


def test_max_tokens_by_intensity():
    assert brainrot._max_tokens({"intensity": "mild"}) < brainrot._max_tokens({"intensity": "unhinged"})


def test_generate_returns_result(monkeypatch):
    _patch(monkeypatch)
    res = asyncio.run(brainrot.generate("hello", SETTINGS))
    assert res.text == "brainrot reply 🗿"
    assert res.tokens == 42
    assert res.persona_key in brainrot.PERSONA_BY_KEY


def test_generate_many_distinct(monkeypatch):
    _patch(monkeypatch)
    out = asyncio.run(brainrot.generate_many("hello", SETTINGS, 3))
    assert len(out) == 3
    assert len({r.persona_key for r in out}) == 3


def test_generate_retries_then_fails(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("groq down")
    monkeypatch.setattr(brainrot._client.chat.completions, "create", boom)
    with pytest.raises(brainrot.BrainrotError, match="groq down"):
        asyncio.run(brainrot.generate("hello", SETTINGS))
