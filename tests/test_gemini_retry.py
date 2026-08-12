"""Waiting out Gemini's rolling window instead of failing into a dead provider.

Groq is IP-blocked from the deployment, so "fall back to Groq" now means "say
nothing". Gemini's 429 is a short rolling window that recovers inside a minute,
which makes it the one failure worth waiting on — and the only one. A 403 or a
404 will never come good, so retrying either just delays the inevitable.

Replies get the whole retry budget because a person is sitting there. The
proactive calls get one attempt: nobody is waiting on a ghost ping, and it must
never spend the quota a real reply is about to need.

Nothing here sleeps for real — `gemini._sleep` is the seam, and it records the
delays that were asked for so the schedule itself can be asserted.
"""
import asyncio
import random

import chat_engine
import config
import db
import gemini
import life
import memory
import search


def _run(coro):
    return asyncio.run(coro)


def _fresh(tmp_path):
    db.close()
    db.init_db(str(tmp_path / "retry.db"))
    config.OUTBOUND_DAILY_BUDGET = 100


def _key(monkeypatch, value="test-gemini-key"):
    monkeypatch.setattr(config, "GEMINI_API_KEY", value)
    monkeypatch.setattr(config, "GEMINI_ENABLED", True)


def _no_sleep(monkeypatch) -> list[float]:
    """Swap the wait for a recording of what it would have been."""
    slept: list[float] = []

    async def fake(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gemini, "_sleep", fake)
    return slept


def _gemini(monkeypatch, *outcomes):
    """Stub the provider seam. An Exception in `outcomes` is raised, not returned."""
    calls = []

    async def fake(messages, *, temperature, max_tokens, tools=None):
        out = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append({"messages": messages, "tools": tools})
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(gemini, "complete", fake)
    return calls


def _groq(monkeypatch, reply="groq answered"):
    calls = []

    async def fake(messages, *, model, temperature, max_tokens, tools=None):
        calls.append({"messages": messages, "model": model})
        return reply

    monkeypatch.setattr(chat_engine, "_complete", fake)
    return calls


def _dead_groq(monkeypatch):
    """What the live deployment actually has: an IP-blocked provider."""
    calls = []

    async def fake(messages, *, model, temperature, max_tokens, tools=None):
        calls.append(1)
        raise RuntimeError('403 {"error":{"message":"Access denied."}}')

    monkeypatch.setattr(chat_engine, "_complete", fake)
    return calls


def _throttle(delay: str | None = None) -> Exception:
    body = '{"error":{"code":429,"status":"RESOURCE_EXHAUSTED"'
    if delay:
        body += f',"details":[{{"retryDelay":"{delay}"}}]'
    return RuntimeError(f"429 {body}}}}}")


# --- what gets retried, and what must not ---------------------------------

def test_a_429_is_retried_and_a_later_attempt_answers(monkeypatch):
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, _throttle(), _throttle(), "back online")

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        raise AssertionError("groq must not be reached")

    out = _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10))

    assert out == "back online"
    assert len(seen) == 3
    assert slept == list(gemini.RETRY_BACKOFF_S[:2])


def test_a_403_is_never_retried(monkeypatch):
    """Groq is 403 from this machine and always will be. So is a blocked key."""
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, RuntimeError('403 {"error":{"message":"Access denied."}}'))
    fell_back = []

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        fell_back.append(1)
        return "yo"

    assert _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10)) == "yo"
    assert len(seen) == 1 and slept == [] and fell_back == [1]


def test_a_404_is_never_retried(monkeypatch):
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, RuntimeError("404 NOT_FOUND model no longer available"))

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10))
    assert len(seen) == 1 and slept == []


def test_a_timeout_is_never_retried(monkeypatch):
    """The deadline already spent the latency budget; spending it again is worse."""
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, TimeoutError())

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10))
    assert len(seen) == 1 and slept == []


def test_an_empty_answer_is_not_retried(monkeypatch):
    """A safety block is a 200. Asking the same question again gets the same 200."""
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, "   ")

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    assert _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10)) == "yo"
    assert len(seen) == 1 and slept == []


# --- the retry is bounded --------------------------------------------------

def test_a_persistent_429_gives_up_rather_than_hanging(monkeypatch):
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, _throttle())
    fell_back = []

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        fell_back.append(1)
        return "yo"

    assert _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10)) == "yo"
    assert len(seen) == len(gemini.RETRY_BACKOFF_S) + 1
    assert slept == list(gemini.RETRY_BACKOFF_S)
    assert fell_back == [1]


def test_the_backoff_schedule_stays_inside_the_latency_cap():
    """A reply nobody waits for is a reply that failed. The cap is the contract."""
    assert sum(gemini.RETRY_BACKOFF_S) <= gemini.RETRY_MAX_TOTAL_S


def test_a_server_retry_delay_is_honoured_over_the_schedule(monkeypatch):
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    _gemini(monkeypatch, _throttle("17s"), "back online")

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        raise AssertionError("groq must not be reached")

    assert _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10)) == "back online"
    assert slept == [17.0]


def test_a_retry_delay_past_the_cap_gives_up_instead_of_waiting(monkeypatch):
    """Being told to wait two minutes is being told this reply is not coming."""
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, _throttle("120s"))
    fell_back = []

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        fell_back.append(1)
        return "yo"

    _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10))
    assert len(seen) == 1 and slept == [] and fell_back == [1]


def test_honoured_delays_still_total_under_the_cap(monkeypatch):
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    _gemini(monkeypatch, _throttle("20s"), _throttle("20s"), _throttle("20s"))

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    _run(gemini.first(groq)([], model="m", temperature=1.0, max_tokens=10))
    assert sum(slept) <= gemini.RETRY_MAX_TOTAL_S


# --- replies pay for the wait; background jobs do not ---------------------

def test_a_background_call_gets_exactly_one_attempt(monkeypatch):
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, _throttle())

    async def groq(messages, *, model, temperature, max_tokens, tools=None):
        return "yo"

    _run(gemini.first(groq, backoff=gemini.NO_RETRY)([], model="m", temperature=1.0, max_tokens=10))
    assert len(seen) == 1 and slept == []


def test_a_ping_does_not_burn_the_quota_a_reply_will_need(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, _throttle())
    _dead_groq(monkeypatch)

    assert _run(chat_engine.ping(1, db.get_chat_state(1), 1, rng=random.Random(0))) == []
    assert len(seen) == 1 and slept == []


def test_a_cold_open_does_not_burn_the_quota_a_reply_will_need(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, _throttle())
    _dead_groq(monkeypatch)

    assert _run(chat_engine.cold_open(1, db.get_chat_state(1), rng=random.Random(0))) == []
    assert len(seen) == 1 and slept == []


def test_a_reply_gets_the_whole_retry_budget(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, _throttle(), _throttle(), "ok fine ||| wsp")
    groq = _groq(monkeypatch)

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["ok fine", "wsp"]
    assert len(seen) == 3 and groq == []
    assert slept == list(gemini.RETRY_BACKOFF_S[:2])


def test_a_tool_calling_reply_cannot_spend_the_cap_twice(tmp_path, monkeypatch):
    """Two rounds through one wrapper is still one reply, and one budget."""
    _fresh(tmp_path)
    _key(monkeypatch)
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", True)
    slept = _no_sleep(monkeypatch)
    _groq(monkeypatch)

    async def fake_look_up(query, n=3):
        return []

    monkeypatch.setattr(search, "look_up", fake_look_up)

    outcomes = [_throttle(), gemini.ToolCall("sybau", search.TOOL_NAME),
                _throttle(), _throttle(), _throttle(), _throttle()]
    seen = []

    async def flaky(messages, *, temperature, max_tokens, tools=None):
        out = outcomes[min(len(seen), len(outcomes) - 1)]
        seen.append(1)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(gemini, "complete", flaky)

    db.add_message(1, "user", "fym sybau")
    _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert sum(slept) <= gemini.RETRY_MAX_TOTAL_S


# --- everything dead is silence, not an error -----------------------------

def test_both_providers_dead_produces_an_empty_burst(tmp_path, monkeypatch):
    """The live shape: Gemini throttled to the end of the budget, Groq 403."""
    _fresh(tmp_path)
    _key(monkeypatch)
    _no_sleep(monkeypatch)
    _gemini(monkeypatch, _throttle())
    groq = _dead_groq(monkeypatch)

    pieces = _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0)))

    assert pieces == []
    assert groq == [1]


def test_a_dead_reply_never_says_anything_about_an_error(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    _no_sleep(monkeypatch)
    _gemini(monkeypatch, RuntimeError("403 Access denied"))
    _dead_groq(monkeypatch)

    assert _run(chat_engine.reply(1, db.get_chat_state(1), rng=random.Random(0))) == []


# --- background work routes through the same provider as replies ----------

def test_pings_route_through_gemini(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    seen = _gemini(monkeypatch, "yo")
    groq = _groq(monkeypatch)

    pieces = _run(chat_engine.ping(1, db.get_chat_state(1), 1, rng=random.Random(0)))

    assert [p.value for p in pieces] == ["yo"]
    assert len(seen) == 1 and groq == []


def test_cold_opens_route_through_gemini(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    seen = _gemini(monkeypatch, "sup")
    groq = _groq(monkeypatch)

    pieces = _run(chat_engine.cold_open(1, db.get_chat_state(1), rng=random.Random(0)))

    assert [p.value for p in pieces] == ["sup"]
    assert len(seen) == 1 and groq == []


def test_distillation_routes_through_gemini(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    seen = _gemini(monkeypatch, "their name is walter")
    db.add_message(1, "user", "im walter")

    _run(memory.distill(1, db.get_chat_state(1)))

    assert len(seen) == 1
    assert "walter" in seen[0]["messages"][0]["content"]
    assert db.get_chat_state(1)["notes"] == "their name is walter"


def test_the_daily_life_state_routes_through_gemini(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    _gemini(monkeypatch, "got their phone taken away")

    assert _run(life.refresh()) == "got their phone taken away"


def test_distillation_gets_one_attempt_on_a_429(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, _throttle())
    db.add_message(1, "user", "im walter")

    _run(memory.distill(1, db.get_chat_state(1)))

    assert len(seen) == 1 and slept == []


def test_the_life_state_gets_one_attempt_on_a_429(tmp_path, monkeypatch):
    _fresh(tmp_path)
    _key(monkeypatch)
    slept = _no_sleep(monkeypatch)
    seen = _gemini(monkeypatch, _throttle())

    _run(life.refresh())

    assert len(seen) == 1 and slept == []


def test_without_a_gemini_key_background_work_still_goes_to_groq(tmp_path, monkeypatch):
    """The key can be absent — bot.py must import and run with Groq alone."""
    _fresh(tmp_path)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    seen = _gemini(monkeypatch, "never called")
    groq = _groq(monkeypatch, "yo")

    pieces = _run(chat_engine.ping(1, db.get_chat_state(1), 1, rng=random.Random(0)))

    assert [p.value for p in pieces] == ["yo"]
    assert seen == [] and len(groq) == 1
    assert groq[0]["model"] == config.GROQ_FALLBACK_MODEL
