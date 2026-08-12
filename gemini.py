"""Gemini, for the replies a person actually reads.

Every quality problem this week has been the model not honouring the prompt —
bluffing about slang it had just admitted it didn't know, reaching for a
statistic to sound informed, answering something adjacent to what was asked.
`gemini-2.5-flash` follows the prompt noticeably better than llama does, and the
prompt is where the whole character lives.

Groq used to keep everything else — ~14,000 requests/day against Gemini's
~1,500, at lower latency — and then Groq started answering every call from the
deployment's IP with `403 Access denied`. A fallback to a blocked provider is
not a fallback, so the split by volume is gone: everything Gemini can take,
Gemini takes, and Groq stays wired underneath for whenever that IP is unblocked.

What replaces the split is a split by WHO IS WAITING. Gemini's limit is a short
rolling window that recovers inside a minute, so a 429 is worth waiting out
rather than failing — but only for a reply, where a person is watching an empty
chat. Ghost pings, cold opens, notes distillation and the daily life state get
one attempt and give up: nobody reads them the moment they land, and a proactive
call that sits in a backoff loop is spending the quota the next real reply
needs. `first`'s `backoff` argument is that asymmetry and nothing else.

TRANSLATION. `search.TOOL` and `recall.TOOL` are written OpenAI-style because
Groq speaks OpenAI. Gemini does not: it wants `function_declarations` with an
uppercase enum for `type`, and it answers with a `functionCall` part rather than
`tool_calls`. `_declarations` and `_read` are the two halves of that mapping and
they are the whole of it — deliberately, because chat_engine never sends a tool
RESULT back to the model. It reruns generation with the result folded into a
rebuilt system prompt instead, so there is no `tool` role and no
`functionResponse` to translate, and this file stays small enough to trust.

Failure is not an option the caller has to think about: chat_engine falls back
to Groq on anything raised here, so a dead provider, an exhausted quota and a
safety block all end with the kid still texting back. That is also why the
deadline below is short — a late reply is worse than a Groq one.
"""
import asyncio
import logging
import re
import time
from typing import NamedTuple

import config

logger = logging.getLogger("brainrotgpt.gemini")

# json-schema types worth passing through. Anything else is dropped rather than
# guessed at: a schema Gemini rejects fails the whole call, and a tool the model
# cannot see is a smaller loss than a reply that never arrives.
_TYPES = frozenset({"object", "string", "number", "integer", "boolean", "array"})

# The kid is rude by design — "negative aura fr", the salty mode, the whole
# annoyed bond line. Default thresholds would filter a normal teenager, and a
# filtered response arrives as a 200 with no text, which chat_engine reads as a
# failure and pays for with a second round trip to Groq. Only the high end
# blocks.
_SAFETY = ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
           "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")


class ToolCall(NamedTuple):
    """The model reaching for a tool. Mirrors chat_engine.ToolCall's shape."""
    query: str
    name: str


def enabled() -> bool:
    """No key means the whole feature is off and everything routes to Groq."""
    return bool(config.GEMINI_ENABLED and config.GEMINI_API_KEY)


_client = None


def _get_client():
    """Built once, on first use. Importing here — like search.py does with ddgs
    — means a missing dependency cannot stop the bot booting."""
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _schema(node: object) -> dict:
    """json-schema -> Gemini Schema. Recursive, because `properties` nest."""
    if not isinstance(node, dict):
        return {}
    out: dict = {}
    t = node.get("type")
    if isinstance(t, str) and t.lower() in _TYPES:
        out["type"] = t.upper()
    if node.get("description"):
        out["description"] = str(node["description"])
    if isinstance(node.get("enum"), list):
        out["enum"] = [str(v) for v in node["enum"]]
    if isinstance(node.get("properties"), dict):
        out["properties"] = {k: _schema(v) for k, v in node["properties"].items()}
    if isinstance(node.get("items"), dict):
        out["items"] = _schema(node["items"])
    if isinstance(node.get("required"), list):
        out["required"] = [str(r) for r in node["required"]]
    return out


def _declarations(tools) -> list[dict]:
    """OpenAI tool schemas -> Gemini function declarations.

    Tool-count-agnostic on purpose: it reads whatever list chat_engine assembled,
    so a tool added or switched off elsewhere needs nothing here.
    """
    decls = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        decl = {"name": str(name), "description": str(fn.get("description") or "")}
        params = _schema(fn.get("parameters") or {})
        if params.get("properties"):
            decl["parameters"] = params
        decls.append(decl)
    return decls


def _split(messages) -> tuple[str, str]:
    """chat_engine sends [system, user]; Gemini wants those in two places."""
    def joined(role):
        return "\n\n".join(str(m.get("content") or "") for m in messages or []
                           if isinstance(m, dict) and m.get("role") == role
                           and m.get("content"))

    return joined("system"), joined("user")


def _read(resp, offered: frozenset[str]) -> "str | ToolCall":
    """Pull a tool call or the reply text out of a Gemini response.

    Everything here comes off the wire, so nothing is trusted: a call naming a
    tool that was not offered on THIS round is ignored — which is what stops the
    second round, offered nothing, from starting a chain — and a blank query
    reads as no call rather than an error.
    """
    parts = []
    for cand in getattr(resp, "candidates", None) or []:
        parts.extend(getattr(getattr(cand, "content", None), "parts", None) or [])

    for p in parts:
        fc = getattr(p, "function_call", None)
        if fc is None:
            continue
        name = getattr(fc, "name", "") or ""
        if name not in offered:
            continue
        args = getattr(fc, "args", None)
        query = str(args.get("query") or "").strip() if isinstance(args, dict) else ""
        if query:
            return ToolCall(query, name)

    return "".join(str(getattr(p, "text", "") or "") for p in parts).strip()


# Live, the owner's key turned out to be capped at 20 requests/day and was
# exhausted, so every reply fell back to Groq — and the log said
# `gemini failed () — falling back to groq`, which took a hand-run call to
# explain. Two things were wrong with it. `%s` on a bare TimeoutError (which is
# what the deadline raises) renders as the empty string, so `%r` here: it names
# the type whether or not the exception carries a message. And a persistent
# condition like an exhausted quota fails on EVERY reply, so an unthrottled line
# per reply buries the rest of the log — but the FIRST occurrence of each
# distinct failure always gets through, because a silent fallback is the bug
# this is fixing, not the fix.
FALLBACK_LOG_EVERY_S = 300
FALLBACK_LOG_KEYS = 32   # bounded: the key carries error text, so it must not be a leak

_logged: dict[str, float] = {}


def _log_fallback(key: str, detail: str) -> None:
    now = time.monotonic()
    last = _logged.get(key)
    if last is not None and (now - last) < FALLBACK_LOG_EVERY_S:
        return
    if len(_logged) >= FALLBACK_LOG_KEYS:
        _logged.clear()
    _logged[key] = now
    logger.warning("gemini failed, falling back to groq: %s", detail)


# Waiting out the rolling window. Three retries, ~31s of waiting worst case,
# which is inside the ~35s a reply may add before the silence is the better
# outcome anyway. The first step is short because most 429s here clear almost
# immediately; the last is long because a window that survived 11s is a window
# that needs real time.
RETRY_BACKOFF_S = (3.0, 8.0, 20.0)
RETRY_MAX_TOTAL_S = 35.0
NO_RETRY: tuple[float, ...] = ()     # what every proactive call passes

# A 429 is the ONLY failure worth a second attempt. 403 (the IP block), 404 (a
# retired model), a safety block, a blown deadline — none of those come good by
# asking again, and each retry is seconds of an already-late reply. Matched on
# the code when the SDK exposes one and on the text when it does not, because
# google.genai's error type is not importable here without paying for the import
# on a machine that may not have the package at all.
_THROTTLED = re.compile(r"\b429\b|RESOURCE_EXHAUSTED", re.I)

# `'retryDelay': '17s'` inside the error body. The server knows when its own
# window reopens better than a fixed schedule does.
_RETRY_DELAY = re.compile(r"retry[-_]?delay\W{0,4}?(\d+(?:\.\d+)?)\s*s", re.I)


def _throttled(e: BaseException) -> bool:
    for attr in ("code", "status_code"):
        v = getattr(e, attr, None)
        if isinstance(v, int) and not isinstance(v, bool):
            return v == 429          # an SDK that reports a code is believed outright
    return bool(_THROTTLED.search(str(e)))


def _retry_delay(e: BaseException) -> float | None:
    m = _RETRY_DELAY.search(str(e))
    return float(m.group(1)) if m else None


async def _sleep(seconds: float) -> None:
    """The wait, behind a seam so tests can assert the schedule without living it."""
    await asyncio.sleep(seconds)


def first(fallback, *, backoff: tuple[float, ...] = RETRY_BACKOFF_S):
    """Wrap Groq's completer so Gemini takes the call and Groq takes the failures.

    Returns something with `_complete`'s exact signature, so chat_engine can hand
    it to `_generate` in Groq's place and nothing else there has to know there
    are two providers. A dead provider must never mean a silent bot, so every way
    Gemini can fail — raising, timing out, or a safety block arriving as a 200
    with no text at all — falls through to `fallback` and still answers. `model`
    is Groq's and passes straight through; Gemini's own is config.GEMINI_MODEL.

    `backoff` is how long this caller is willing to wait out a 429, one entry per
    retry. Replies take the default; everything proactive passes NO_RETRY and
    gets a single attempt. Nothing else is ever retried.

    The latency cap belongs to the WRAPPER, not to one call through it, because
    a reply that reaches for a tool goes through here twice: per-call budgets
    would let a throttled tool round and a throttled second round each spend the
    cap and put the reply a minute out. Each `reply` builds its own wrapper, so
    the running total is one reply's and never leaks between chats.
    """
    waited = 0.0

    async def complete_(messages, *, model, temperature, max_tokens, tools=None):
        nonlocal waited
        if enabled():
            for i in range(len(backoff) + 1):
                try:
                    out = await complete(messages, temperature=temperature,
                                         max_tokens=max_tokens, tools=tools)
                    if not isinstance(out, str) or out.strip():
                        return out
                    _log_fallback("empty", "answered with nothing (safety block or truncation)")
                    break
                except Exception as e:  # noqa: BLE001 — every failure is groq's turn
                    delay = _wait_for(e, backoff, i, waited)
                    if delay is None:
                        # Keyed on the type plus the head of the message: an
                        # exhausted quota repeats with a different retry delay
                        # every time, so the whole string would defeat the
                        # throttle it is keying.
                        _log_fallback(f"{type(e).__name__}:{str(e)[:40]}", repr(e))
                        break
                    logger.info("gemini throttled, retrying in %.1fs (attempt %d)", delay, i + 2)
                    waited += delay
                    await _sleep(delay)
        return await fallback(messages, model=model, temperature=temperature,
                              max_tokens=max_tokens, tools=tools)

    return complete_


def _wait_for(e: BaseException, backoff: tuple[float, ...], i: int,
              waited: float) -> float | None:
    """How long to wait before attempt i+2, or None to stop retrying now.

    A server-supplied delay wins over the schedule — it is the window's actual
    reopening time — but it cannot buy more than the cap, and a delay that would
    blow the cap stops the retries here rather than waiting a truncated amount
    that is guaranteed to 429 again on arrival.
    """
    if i >= len(backoff) or not _throttled(e):
        return None
    delay = _retry_delay(e)
    if delay is None:
        delay = backoff[i]
    return delay if delay > 0 and waited + delay <= RETRY_MAX_TOTAL_S else None


async def complete(messages, *, temperature, max_tokens, tools=None) -> "str | ToolCall":
    """One Gemini completion. Raises on anything — the caller falls back to Groq.

    Returns the reply text, or a ToolCall when `tools` were offered and the model
    reached for one. Empty text is a legitimate answer here (a safety block, a
    truncation) and chat_engine treats it as a failure; this function does not
    second-guess it.
    """
    from google.genai import types

    system, user = _split(messages)
    decls = _declarations(tools)
    offered = frozenset(d["name"] for d in decls)

    cfg = types.GenerateContentConfig(
        system_instruction=system or None,
        temperature=temperature,
        top_p=0.95,
        max_output_tokens=max_tokens,
        # 2.5 thinks by default, and thinking is both seconds the 12s reply
        # budget does not have and tokens spent inside max_output_tokens that
        # the burst never sees. A 14-year-old does not deliberate.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        safety_settings=[types.SafetySetting(category=c, threshold="BLOCK_ONLY_HIGH")
                         for c in _SAFETY],
        tools=[types.Tool(function_declarations=decls)] if decls else None,
    )

    resp = await asyncio.wait_for(
        _get_client().aio.models.generate_content(
            model=config.GEMINI_MODEL, contents=user, config=cfg,
        ),
        config.GEMINI_TIMEOUT_S,
    )
    return _read(resp, offered)
