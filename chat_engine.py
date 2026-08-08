"""Making the call: gather the live inputs, talk to the provider, parse a burst.

Who the kid IS lives in persona.py — this module is everything with a side
effect. It reads the facts, trends, day-state and stickers that the prompt needs
(`_facts_for`, `_context`), owns the tool-calling machinery (`ToolCall`,
`_complete`, `_generate`) and exposes the three ways the kid speaks: `reply` to
a real person, and the two proactive ones, `ping` and `cold_open`.

The split is by side effect, not by subject: persona.py is pure string building
and can be tested with no connection and no stubbed provider; everything here
touches the database, the clock, or the network.
"""
import json
import logging
import random as _random
import time
from typing import NamedTuple

from groq import AsyncGroq

import brainrot
import budget
import burst
import config
import db
import gemini
import guard
import life
import memory
import persona
import recall
import search
import stickers

logger = logging.getLogger("brainrotgpt.chat_engine")


FACTS_IN_PROMPT = 12      # newest-first; the rest stay in the DB


def _facts_for(state: dict) -> list[str]:
    """What this chat has told the kid. Memory never gets to break a reply."""
    chat_id = state.get("chat_id")
    if chat_id is None:
        return []
    try:
        return [r["fact"] for r in recall.recent_facts(int(chat_id), limit=FACTS_IN_PROMPT)]
    except Exception:  # noqa: BLE001 — generation must never depend on memory
        return []


_clients = [AsyncGroq(api_key=k) for k in config.GROQ_KEYS]

# Burst size weights: 1 msg 40%, 2 msgs 35%, 3 msgs 20%, 4-5 msgs 5%.
_BURST_SIZES = (1, 2, 3, 4, 5)
_BURST_WEIGHTS = {
    "chill":  (60, 30, 8, 1, 1),
    "normal": (40, 35, 20, 3, 2),
    "clingy": (20, 30, 30, 12, 8),
}


# Spec §4: the school block shortens replies too, not just slows them. A kid
# under a desk sends one or two, not a five-message burst — but never zero,
# because the point is that they still text, just badly.
SCHOOL_BURST_CAP = 2


def burst_target(chattiness: str, *, rng, in_school: bool = False) -> int:
    weights = _BURST_WEIGHTS.get(chattiness, _BURST_WEIGHTS["normal"])
    n = rng.choices(_BURST_SIZES, weights=weights, k=1)[0]
    return min(n, SCHOOL_BURST_CAP) if in_school else n


class ToolCall(NamedTuple):
    """The model reaching for a tool. Every tool takes exactly one argument.

    `name` defaults to the web lookup because that was the only tool when this
    was written, and the kid asking to look something up stays the common case.
    """
    query: str
    name: str = search.TOOL_NAME


def _tool_call(msg, offered: frozenset[str]) -> ToolCall | None:
    """Pull a tool request out of a Groq message, or None.

    Every field here comes off the wire, so nothing is trusted: a malformed
    arguments blob or a blank query reads as "no call" rather than raising, and
    a call naming a tool that was not in `offered` — including anything on a
    round where no tools were offered at all — is ignored outright.
    """
    for c in getattr(msg, "tool_calls", None) or []:
        fn = getattr(c, "function", None)
        name = getattr(fn, "name", "") if fn is not None else ""
        if name not in offered:
            continue
        try:
            args = json.loads(getattr(fn, "arguments", "") or "{}")
        except (TypeError, ValueError):
            continue
        query = (args.get("query") or "").strip() if isinstance(args, dict) else ""
        if query:
            return ToolCall(query, name)
    return None


async def _complete(messages, *, model, temperature, max_tokens, tools=None) -> "str | ToolCall":
    """One completion, trying each API key in turn. Raises if all keys fail.

    Returns the reply text — or, when `tools` were offered and the model reached
    for one, a ToolCall saying which tool and with what argument.
    """
    last_err: Exception | None = None
    extra = {"tools": tools, "tool_choice": "auto"} if tools else {}
    # Only a tool offered on THIS round is honoured, which is what stops the
    # second round starting a chain: it gets no tools, so any call it makes
    # anyway is ignored rather than run.
    offered = frozenset(t["function"]["name"] for t in tools or [])
    for client in _clients:
        try:
            resp = await client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                top_p=0.95, seed=_random.randint(1, 2_000_000_000),
                max_tokens=max_tokens, **extra,
            )
            msg = resp.choices[0].message
            call = _tool_call(msg, offered)
            return call if call is not None else (msg.content or "").strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err or RuntimeError("no groq client")


def _context(state: dict, *, rng, target: int, lookup: list[dict] | None = None,
             recalled: list[dict] | None = None, can_look_up: bool = False,
             tools_offered: bool = False) -> str:
    try:
        memes = db.trend_memes_for_generation(limit=2)
        vocab = db.trend_terms_for_generation(limit=8) or rng.sample(brainrot.VOCAB, 6)
    except Exception:  # noqa: BLE001 — generation must never depend on trends
        memes, vocab = [], rng.sample(brainrot.VOCAB, 6)
    return persona.build_system_prompt(
        state, day_state=life.current(), memes=memes, vocab=vocab,
        sticker_emoji=stickers.available_emoji(), burst_target=target,
        facts=_facts_for(state), lookup=lookup, recalled=recalled,
        can_look_up=can_look_up, tools_offered=tools_offered,
    )


async def _generate(system: str, user: str, *, model, temperature, max_tokens,
                    max_msgs: int, tools=None, run_tool=None, complete=None) -> list[burst.Piece]:
    """Generate a burst, optionally letting the model use one tool first.

    `tools` and `run_tool` go together: the tool schemas to offer, and an async
    callable taking the ToolCall, running it, and returning the system prompt to
    generate from. Pass neither (pings, cold opens — neither is answering a
    question) and the model never sees a tool. `complete` is the provider seam —
    None is Groq, and `reply` hands in gemini.first so Gemini takes that one call.

    The whole path is inside the try on purpose. burst.parse runs regexes over
    untrusted model output and the tool round parses JSON off the wire, so both
    are part of "generation": a failure anywhere goes quiet exactly like a
    network failure rather than raising into the bot.
    """
    def msgs(sys_prompt):
        return [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]

    call = complete or _complete  # resolved here, not defaulted in the signature, so patching _complete still lands
    try:
        out = await call(msgs(system), model=model, temperature=temperature,
                         max_tokens=max_tokens, tools=tools if run_tool else None)
        if not isinstance(out, str):
            # At most one tool call per reply: the second round is offered no
            # tools, so there is no chain to run away with and no loop to bound.
            # Both tools already swallow their own failures, but they are caught
            # again here so a broken tool degrades to a normal reply instead of
            # the kid going silent — the unmodified prompt carries no facts and
            # no recall block, leaving HONESTY_RULE standing.
            try:
                rebuilt = await run_tool(out)
            except Exception as e:  # noqa: BLE001
                logger.warning("tool %r blew up for %r: %s", out.name, out.query, e)
                rebuilt = system
            out = await call(msgs(rebuilt), model=model, temperature=temperature, max_tokens=max_tokens)
        return burst.parse(out if isinstance(out, str) else "", max_msgs=max_msgs)
    except Exception as e:  # noqa: BLE001 — the kid goes quiet, it never errors at you
        logger.warning("generation failed: %s", e)
        return []


async def reply(chat_id: int, state: dict, *, rng) -> list[burst.Piece]:
    """Answer a real user. Deliberately NOT budgeted."""
    target = burst_target(state.get("chattiness") or "normal", rng=rng,
                          in_school=life.in_school_block(time.time()))
    # Read per-reply, not at import: either switch works on a live bot. Computed
    # BEFORE the prompt because the prompt's closing line depends on it — "output
    # ONLY the messages" suppresses function calling outright, so the permissive
    # form has to go in whenever any tool is really on the table.
    tools = ([search.TOOL] if config.WEB_SEARCH_ENABLED else []) + \
            ([recall.TOOL] if config.RECALL_ENABLED else [])

    # Both flags apply only to this first round — it is the only one carrying
    # tools. run_tool's rebuild below deliberately leaves them off.
    system = _context(state, rng=rng, target=target,
                      can_look_up=config.WEB_SEARCH_ENABLED,
                      tools_offered=bool(tools))
    convo = guard.wrap_untrusted(memory.transcript(chat_id))
    user = f"{convo}\n\nReply as {persona.KID_NAME}, {target} message(s), separated by |||."

    async def run_tool(call: ToolCall) -> str:
        # Rebuilt rather than patched: the block belongs inside the prompt's own
        # ordering, and one more sqlite read is nothing on this path.
        if call.name == recall.TOOL_NAME:
            return _context(state, rng=rng, target=target,
                            recalled=db.search_messages(chat_id, call.query))
        if call.name == search.TOOL_NAME:
            return _context(state, rng=rng, target=target,
                            lookup=await search.look_up(call.query))
        # An unrecognised tool changes nothing, rather than falling through to
        # whichever branch happens to be last and silently web-searching for it.
        return system

    return await _generate(system, user, model=config.GROQ_MODEL, temperature=1.05, max_tokens=400, max_msgs=5,
                           tools=tools or None, run_tool=run_tool if tools else None, complete=gemini.first(_complete))


_PING_ENERGY = {
    1: "you texted them a bit ago and got nothing. nudge them, totally casual. one or two words.",
    2: "still nothing, an hour or two later. mildly impatient.",
    3: "hours later, still ignored. now you're being dramatic about it.",
    4: "a whole day. passive-aggressive, wounded, over it.",
    5: "days. this is your last message before you give up on them entirely. short and final.",
}


# Live, three consecutive pings came back "hey / idk what to say lol",
# "hey / idk lol", "hey / sitll bored". _PING_ENERGY already varies by stage, so
# the repetition is happening WITHIN a stage's output: the model was handed the
# transcript and nothing else, and restating the last line is the cheapest thing
# it can do. Naming the exact sentence is the point — "be original" gives it
# nothing to diverge from.
_PING_NO_REPEAT = (
    "HARD RULE — your last message was: {last}\n"
    "Do NOT repeat it, paraphrase it, or open with the same word it opened with. "
    "If you already said you were bored, you cannot say it again. This is not a "
    "greeting either — you are mid-conversation with them. A ping is a NEW angle: "
    "a fresh thought, a callback to something specific they told you, or something "
    "random that just happened to you."
)
_PING_NO_REPEAT_FIRST = (
    "HARD RULE — do not open with a bare greeting, and do not say the same word "
    "twice running. A ping is a new angle: a fresh thought, a callback to something "
    "specific, or something random that just happened to you."
)
PING_LAST_MAX_CHARS = 160   # the kid's own output, but keep it bounded in the prompt


def _ping_divergence(chat_id: int) -> str:
    last = memory.last_kid_message(chat_id)[:PING_LAST_MAX_CHARS].strip()
    if not last:
        return _PING_NO_REPEAT_FIRST
    # Quoted with repr so the model sees where the kid's own text starts and
    # ends. It reaches the prompt inside guard.wrap_untrusted's transcript
    # already; this is the same content, pointed at.
    return _PING_NO_REPEAT.format(last=repr(last))


async def ping(chat_id: int, state: dict, stage: int, *, rng) -> list[burst.Piece]:
    """A ghost-ladder nudge. Budgeted, and routed to the cheap model."""
    if not budget.can_spend(time.time()):
        return []
    system = _context(state, rng=rng, target=1)
    convo = guard.wrap_untrusted(memory.transcript(chat_id, limit=6))
    user = (f"{convo}\n\nThey have not replied. {_PING_ENERGY.get(stage, _PING_ENERGY[1])}\n\n"
            f"{_ping_divergence(chat_id)}\n\n"
            f"Send 1-2 very short messages, separated by |||.")
    pieces = await _generate(system, user, model=config.GROQ_FALLBACK_MODEL,
                             temperature=1.1, max_tokens=120, max_msgs=2)
    if pieces:
        budget.spend(time.time())
    return pieces


async def cold_open(chat_id: int, state: dict, *, rng) -> list[burst.Piece]:
    """Texting first, unprompted. Budgeted, cheap model."""
    if not budget.can_spend(time.time()):
        return []
    system = _context(state, rng=rng, target=1)
    user = ("Text them first, out of nowhere. Either say what's going on with you "
            "today, bring up a meme you're into, or call back to something you know "
            "about them. 1-2 very short messages, separated by |||.")
    pieces = await _generate(system, user, model=config.GROQ_FALLBACK_MODEL,
                             temperature=1.15, max_tokens=120, max_msgs=2)
    if pieces:
        budget.spend(time.time())
    return pieces
