"""Groq client + the BrainrotGPT generation engine.

Every reply is randomized so outputs don't collapse into the same shape:
a rotating PERSONA (register), a random SUBSET of vocab, a random structural
OPENER, jittered sampling + seed, plus per-chat TONE / INTENSITY / LANGUAGE.

Reliability: each call retries the primary model then falls back to a second
model. Forwarded text is wrapped as untrusted data (prompt-injection defense)
and trimmed to a token budget. Supports single, best-of-N, and streaming modes.
"""
import asyncio
import random
from dataclasses import dataclass

from groq import AsyncGroq

import config
import db
import guard

# Core rules — true for EVERY reply. Flavor is injected per-call below.
BASE_RULES = """You are BrainrotGPT, an elite reply generator. You are handed a snippet of a real conversation (one or more messages, sometimes labeled with sender names). Your ONLY job is to write ONE single brainrot REPLY that the user can paste straight back into that conversation — an absurdly overdramatic, emoji-stuffed Gen-Z brainrot response that actually makes sense as a reply to what was said.

RULES:
- READ the whole conversation and reply to the latest message / the overall point being made. Your reply MUST be on-topic and make sense as a genuine response — never ignore what they said.
- Reply as the user (first person), like you're firing back in the chat.
- Overdramatic — treat the tiniest thing like a colossal deal. (How LONG the reply runs is set by the LENGTH instruction below — obey it; do NOT default to a giant rant.)
- NO line breaks. NO bullet points. NO lists — it's one flowing message.
- Stuff it with emojis — multiple per sentence.
- Make simple events sound like world-ending catastrophes. Compare ordinary problems to absurd cosmic events.
- Fake lore (councils, audits, dimensions, emergency meetings) is great flavor, but only as much as the LENGTH allows — don't pad a short reply with it.
- Keep it readable despite the insanity. Keep it funny, absurd, and intentionally excessive.
- Never use hateful, threatening, or harmful language.
- Output ONLY the reply paragraph — no preamble, no quotes, no explanation."""

# One PERSONA per call — the single biggest lever against samey output.
# (key, display label, instruction). These are recognizable internet-culture
# *voices* (kept orthogonal to TONE, which is the attitude toward the situation).
#
# chat_engine reuses this list as the KID'S MOOD WHEEL, and that is the harder
# constraint of the two: every entry has to be a voice a chronically-online
# 14-year-old actually has. Four v2 registers were not — `nerd` (a lecturer
# citing fake statistics), `wise_elder` (a sage), `sports_caster` (a
# broadcaster) and `conspiracy` (a forum poster) all read as adults, and `nerd`
# in particular answered "you play any games?" with "as per my research, 74% of
# gamers play fortnite". They are gone; see mood_persona for the migration.
PERSONAS = [
    ("sigma", "🗿 Sigma", "Channel a delusional SIGMA / gigachad grindset coach — everything is mindset, discipline, 5am cold plunges, 'staying locked in', looksmaxxing, never beta. Treat the convo as a test of someone's mental fortitude."),
    ("skibidi", "💀 Skibidi", "Go FULL unfiltered skibidi brainrot — maximum Ohio, Fanum tax, gyatt, rizz, every brainrot term firing at once with zero self-awareness. The most chronically-online reply imaginable."),
    ("rizzler", "😎 Rizzler", "Channel a smooth-talking RIZZLER — oozing confidence and charm, spinning every line into flirty unspoken-rizz game and W-rizz energy. Effortlessly suave but still total brainrot."),
    ("delulu", "🦋 Delulu", "Channel a fully DELULU dreamer — 'delulu is the solulu', detached from reality, romanticizing everything, building an entire fantasy world out of the convo. Hopelessly, confidently delusional."),
    ("drama_queen", "👑 Drama Queen", "Channel a theatrical DRAMA QUEEN — soap-opera meltdown, gasps, betrayal, fainting couch, 'I have NEVER been so disrespected in my LIFE'. Treat the smallest thing as the scandal of the century."),
    ("heartbroken", "🥀 Heartbroken", "Channel a melodramatic HEARTBROKEN sad-boy/poet — emotional damage, betrayal, staring out the rainy window, 'it is what it is', violins swelling. Tragic, wounded, dramatic brainrot."),
    ("gamer", "🎮 Gamer", "Channel a sweaty GAMER raging in voice chat — everything is a boss fight, a clutch, a respawn, lag, 'GG', '0.2 KD', no-grass-touched denial. Gaming metaphors for the whole situation."),
    ("villain", "😈 Villain Era", "Channel someone in their VILLAIN ERA / main-character arc — unbothered, moisturized, in their lane, plotting, 'I'm the problem and I love it'. Smug, self-assured antihero energy."),
]
PERSONA_BY_KEY = {p[0]: p for p in PERSONAS}

DEFAULT_MOOD = "skibidi"


def mood_persona(key: str | None):
    """A stored chat_state.mood → the persona tuple to write this turn in.

    Moods are persisted, so retiring a persona is a live migration: there are
    rows carrying mood='nerd' right now. An unknown key lands on DEFAULT_MOOD
    instead of raising — and instead of being echoed back at the owner by
    /settings, which is what a bare dict lookup with a passthrough default did.
    """
    return PERSONA_BY_KEY.get(key or "", PERSONA_BY_KEY[DEFAULT_MOOD])

# A random subset of these is injected each call (no more 'every reply = Ohio + John Pork').
# Keep the freshest trends at the top of the "current refresh" block and trim stale ones
# over time. (A live trends source could be mixed into the per-call sample below too.)
VOCAB = [
    # --- 2025–26 refresh — the currently-trending stuff ---
    "67 (six seven) 🔢", "Italian brainrot 🇮🇹🧠", "tralalero tralala 🦈👟",
    "Tung Tung Tung Sahur 🥁", "Bombardiro Crocodilo 🐊✈️", "Ballerina Cappuccina ☕🩰",
    "crashing out 😵‍💫", "we're so cooked 🍳💀", "chopped 🪓", "it's giving ✨",
    "the ick 🤢", "mewing 🤫", "aura points 📈", "ragebait 🎣", "glazing 🍩",
    "yapping 🗣️", "that's my twin 👯", "based 🗿", "menace to society 😈",
    "side eye 👀", "let him cook 🍳", "the Costco guys BOOM 💥", "goated 🐐",
    "what the sigma 🗿", "negative aura -1000 📉",
    # --- evergreen brainrot ---
    "sigma 🗿", "aura 📈", "Ohio 🌽", "Skibidi 🚽", "Fanum Tax 🍕",
    "John Pork 📞🐷", "Baby Gronk 🏈", "Balkan rage 🇦🇱", "Tiki Phonk 🎧🔥",
    "rizz 😭🙏", "mogging 🗿", "CaseOh 🍔", "Costco chicken 🍗",
    "shadow realm 🌌", "aura farming 📈🗿", "interdimensional bugs 👁️",
    "gyatt 🍑", "the Grimace shake 🟣", "the backrooms 🚪", "NPC behavior 🎮",
    "touch grass 🌱", "delulu 🦋", "main character energy 🎬", "the lobotomy 🧠",
    "negative aura 📉", "the gooning chamber 🔒", "Quandale Dingle 🗿", "rizzler 😎",
    "the skibidi council 🚽👑", "looksmaxxing 🪞", "the sigma grindset 💪",
    "brainrot overdose 🧠💥", "the aura court ⚖️📈", "chat is this real 💀",
    "Fanum stealing the food 🍕😤", "low taper fade 💇",
]

# A random OPENER varies the *shape* so replies don't all start the same way.
OPENERS = [
    "Open mid-sentence as if you've been ranting for an hour and we just tuned in.",
    "Start dead calm and reasonable for exactly one clause, then completely lose it.",
    "Open like a BREAKING NEWS / emergency broadcast interrupting regular programming.",
    "Open by addressing an imaginary live audience / 'chat' watching this unfold.",
    "Open by calling an emergency meeting of a fake council about this exact situation.",
    "Open with a fake disclaimer/legal notice, then immediately abandon it for chaos.",
    "Open by 'pulling up the data' / presenting fake statistics about what happened.",
    "Open as if recounting this to a future generation who must NEVER forget.",
]

# Per-chat INTENSITY = how feral the energy is (independent of length).
INTENSITY = {
    "mild": "Dial the chaos DOWN — dramatic and funny but grounded, lightly seasoned brainrot.",
    "medium": "Balanced chaos — solidly unhinged but still easy to follow.",
    "unhinged": "MAXIMUM chaos — completely feral, every sentence escalating into absurdity.",
}

# Per-chat LENGTH = how long the reply runs (independent of intensity).
# Each maps to a length instruction + the model's max_tokens cap.
LENGTH = {
    "short": {"instruction": "Keep it SHORT — ONE or two punchy sentences, max. No wall of text, no rambling, don't stack lore: land one hard hit and STOP.", "max_tokens": 300},
    "medium": {"instruction": "Medium length — one solid overdramatic paragraph.", "max_tokens": 1200},
    "long": {"instruction": "Long — a big, sprawling, multi-sentence rant.", "max_tokens": 2200},
    "max": {"instruction": "MAXIMUM length — a giant unbroken wall of brainrot, go as long as you possibly can.", "max_tokens": 4000},
}

# Per-chat tone preset layered on top of the persona.
TONE_INSTRUCTIONS = {
    "default": "",
    "roast": "Aim the energy at roasting/clowning the other person's take — playful, never hateful.",
    "cope": "Frame it as the user coping hard, in total denial about an L, spinning it into a W.",
    "hype": "Pure hype — gas the user up like they're the main character who just won everything.",
    "deny": "Deny everything — 'that never happened', deflect with absurd confidence.",
    "gaslight": "Comedically gaslight the situation — rewrite reality in the user's favor, absurdly sure.",
}


@dataclass
class BrainrotResult:
    text: str
    persona_key: str
    persona_label: str
    tokens: int


class BrainrotError(Exception):
    """Raised when the Groq call fails or returns nothing usable."""


# Primary client kept as a named handle (tests/back-compat); the pool adds any
# backup-key clients, tried in order when the primary runs out of tokens.
_client = AsyncGroq(api_key=config.GROQ_API_KEY)
_clients = [_client] + [AsyncGroq(api_key=k) for k in config.GROQ_KEYS[1:]]


# --- Persona selection ----------------------------------------------------

def choose_persona(settings: dict, avoid_persona: str | None = None, exclude=()):
    """Pick a persona tuple honoring a pinned persona or rolling randomly."""
    pinned = settings.get("persona", "random")
    if pinned != "random" and pinned in PERSONA_BY_KEY:
        return PERSONA_BY_KEY[pinned]
    skip = set(exclude) | ({avoid_persona} if avoid_persona else set())
    pool = [p for p in PERSONAS if p[0] not in skip] or PERSONAS
    return random.choice(pool)


def _distinct_personas(settings: dict, n: int, avoid_persona: str | None):
    pinned = settings.get("persona", "random")
    if pinned != "random" and pinned in PERSONA_BY_KEY:
        return [PERSONA_BY_KEY[pinned]] * n
    pool = [p for p in PERSONAS if p[0] != avoid_persona] or PERSONAS
    return random.sample(pool, k=min(n, len(pool)))


# --- Prompt assembly ------------------------------------------------------

def _vocab_sample(k: int = 8, live_k: int = 2) -> list[str]:
    """Per-call brainrot terms: blend a couple of LIVE trend terms (manual +
    auto-fetched) with the static VOCAB so replies drift with the trends.
    Falls back to pure static if there are no live trends / the DB is closed."""
    try:
        live = db.trend_terms_for_generation(limit=20)
    except Exception:  # noqa: BLE001 — generation must never depend on trends
        live = []
    if not live:
        return random.sample(VOCAB, k=min(k, len(VOCAB)))
    n_live = min(live_k, len(live), k)
    picked = random.sample(live, n_live)
    static = random.sample(VOCAB, k=min(k - n_live, len(VOCAB)))
    sample = picked + static
    random.shuffle(sample)
    return sample


def _build_system_prompt(persona, settings: dict) -> str:
    _, _, desc = persona
    vocab_sample = _vocab_sample()
    opener = random.choice(OPENERS)
    intensity = INTENSITY.get(settings.get("intensity", "medium"), INTENSITY["medium"])
    length = LENGTH.get(settings.get("length", "medium"), LENGTH["medium"])
    tone = TONE_INSTRUCTIONS.get(settings.get("tone", "default"), "")
    lang = settings.get("language", "auto")
    lang_line = (
        "Reply in the SAME language as the conversation."
        if lang == "auto" else f"Write the reply in {lang}."
    )
    parts = [
        BASE_RULES, "",
        f"PERSONA FOR THIS REPLY: {desc}",
        "Commit fully to this persona — let it shape the metaphors, framing, and jokes.",
        "", f"OPENING STYLE: {opener}",
        "", f"INTENSITY: {intensity}",
        "", f"LENGTH: {length['instruction']}",
        "", f"LANGUAGE: {lang_line}",
    ]
    if tone:
        parts += ["", f"TONE: {tone}"]
    parts += [
        "",
        f"BRAINROT VOCAB TO LEAN ON THIS TIME (weave in naturally): {', '.join(vocab_sample)}.",
        "Use mostly THESE terms; don't drag in the same few brainrot words every time.",
    ]
    return "\n".join(parts)


def _build_user_content(transcript: str, avoid_text: str | None) -> str:
    trimmed, _ = guard.trim_transcript(transcript)
    content = guard.wrap_untrusted(trimmed) + "\n\nOUTPUT:"
    if avoid_text:
        content += (
            "\n\nIMPORTANT: This is a REGENERATE. Your previous attempt is below. "
            "Write a COMPLETELY different reply — different angle, opening, metaphors "
            "and bits. Do NOT reuse its jokes or structure.\n\nPREVIOUS ATTEMPT:\n"
            + avoid_text[:700]
        )
    return content


def _max_tokens(settings: dict) -> int:
    return LENGTH.get(settings.get("length", "medium"), LENGTH["medium"])["max_tokens"]


# --- Groq calls (with retry + fallback) -----------------------------------

async def _complete(messages, *, temperature, top_p, seed, max_tokens):
    """Try the primary model (twice) then the fallback model — and for each, try
    each API key in turn (primary then backups). Cycling keys is what lets a
    backup take over when the primary key is out of tokens / rate-limited."""
    attempts = [config.GROQ_MODEL, config.GROQ_MODEL, config.GROQ_FALLBACK_MODEL]
    last_err: Exception | None = None
    for model in attempts:
        for client in _clients:
            try:
                return await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    top_p=top_p,
                    seed=seed,
                    max_tokens=max_tokens,
                )
            except Exception as e:  # noqa: BLE001 — network/auth/rate-limit/bad model
                last_err = e
    raise BrainrotError(str(last_err)[:200] if last_err else "unknown error")


def _extract(resp) -> tuple[str, int]:
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise BrainrotError("empty response from model")
    tokens = 0
    usage = getattr(resp, "usage", None)
    if usage is not None:
        tokens = getattr(usage, "total_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0
    return text, int(tokens or len(text) // 4)


async def _generate_once(transcript, settings, persona, avoid_text) -> BrainrotResult:
    messages = [
        {"role": "system", "content": _build_system_prompt(persona, settings)},
        {"role": "user", "content": _build_user_content(transcript, avoid_text)},
    ]
    resp = await _complete(
        messages,
        temperature=round(random.uniform(0.95, 1.2), 2),
        top_p=round(random.uniform(0.9, 1.0), 2),
        seed=random.randint(1, 2_000_000_000),
        max_tokens=_max_tokens(settings),
    )
    text, tokens = _extract(resp)
    return BrainrotResult(text=text, persona_key=persona[0], persona_label=persona[1], tokens=tokens)


# --- Public API -----------------------------------------------------------

async def generate(
    transcript: str,
    settings: dict,
    *,
    avoid_text: str | None = None,
    avoid_persona: str | None = None,
) -> BrainrotResult:
    """Single brainrot reply honoring the chat's settings."""
    persona = choose_persona(settings, avoid_persona=avoid_persona)
    return await _generate_once(transcript, settings, persona, avoid_text)


async def generate_many(
    transcript: str,
    settings: dict,
    n: int,
    *,
    avoid_text: str | None = None,
    avoid_persona: str | None = None,
) -> list[BrainrotResult]:
    """Best-of-N: generate n candidates concurrently (distinct personas if random)."""
    n = max(1, min(n, len(PERSONAS)))
    personas = _distinct_personas(settings, n, avoid_persona)
    results = await asyncio.gather(
        *[_generate_once(transcript, settings, p, avoid_text) for p in personas],
        return_exceptions=True,
    )
    out = [r for r in results if isinstance(r, BrainrotResult)]
    if out:
        return out
    for r in results:
        if isinstance(r, Exception):
            raise r if isinstance(r, BrainrotError) else BrainrotError(str(r)[:200])
    raise BrainrotError("no candidates produced")


async def generate_stream(transcript, settings, persona, *, avoid_text=None):
    """Yield cumulative text as tokens stream in. No fallback (keep it simple)."""
    messages = [
        {"role": "system", "content": _build_system_prompt(persona, settings)},
        {"role": "user", "content": _build_user_content(transcript, avoid_text)},
    ]
    create_kwargs = dict(
        model=config.GROQ_MODEL,
        messages=messages,
        temperature=round(random.uniform(0.95, 1.2), 2),
        top_p=round(random.uniform(0.9, 1.0), 2),
        seed=random.randint(1, 2_000_000_000),
        max_tokens=_max_tokens(settings),
        stream=True,
    )
    stream = None
    last_err: Exception | None = None
    for client in _clients:  # fall through to a backup key if the primary is tapped out
        try:
            stream = await client.chat.completions.create(**create_kwargs)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
    if stream is None:
        raise BrainrotError(str(last_err)[:200] if last_err else "stream init failed")
    text = ""
    async for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        if delta:
            text += delta
            yield text
    if not text.strip():
        raise BrainrotError("empty response from model")


# Prompt used for the scheduled /daily brainrot horoscope.
DAILY_PROMPT = (
    "Someone just woke up and opened their phone. Give them today's unhinged "
    "brainrot horoscope / hype-up for the day."
)
