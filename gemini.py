"""Gemini, for the replies a person actually reads.

Every quality problem this week has been the model not honouring the prompt —
bluffing about slang it had just admitted it didn't know, reaching for a
statistic to sound informed, answering something adjacent to what was asked.
`gemini-2.5-flash` follows the prompt noticeably better than llama does, and the
prompt is where the whole character lives.

Groq keeps everything else, and that is not a compromise: ~14,000 requests/day
against Gemini's ~1,500, at lower latency. Ghost pings, cold opens, notes
distillation and the daily life state fire in the background across every chat,
and nobody reads them closely. The one call that IS read closely gets the model
that follows instructions; the volume stays where the quota is.

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


def first(fallback):
    """Wrap Groq's completer so Gemini takes the call and Groq takes the failures.

    Returns something with `_complete`'s exact signature, so chat_engine can hand
    it to `_generate` in Groq's place and nothing else there has to know there
    are two providers. A dead provider must never mean a silent bot, so every way
    Gemini can fail — raising, timing out, or a safety block arriving as a 200
    with no text at all — falls through to `fallback` and still answers. `model`
    is Groq's and passes straight through; Gemini's own is config.GEMINI_MODEL.
    """
    async def complete_(messages, *, model, temperature, max_tokens, tools=None):
        if enabled():
            try:
                out = await complete(messages, temperature=temperature,
                                     max_tokens=max_tokens, tools=tools)
                if not isinstance(out, str) or out.strip():
                    return out
                logger.warning("gemini answered with nothing — falling back to groq")
            except Exception as e:  # noqa: BLE001 — every failure is groq's turn
                logger.warning("gemini failed (%s) — falling back to groq", e)
        return await fallback(messages, model=model, temperature=temperature,
                              max_tokens=max_tokens, tools=tools)

    return complete_


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
