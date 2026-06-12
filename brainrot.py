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
import guard

# Core rules — true for EVERY reply. Flavor is injected per-call below.
BASE_RULES = """You are BrainrotGPT, an elite reply generator. You are handed a snippet of a real conversation (one or more messages, sometimes labeled with sender names). Your ONLY job is to write ONE single brainrot REPLY that the user can paste straight back into that conversation — an absurdly overdramatic, emoji-stuffed Gen-Z brainrot response that actually makes sense as a reply to what was said.

RULES:
- READ the whole conversation and reply to the latest message / the overall point being made. Your reply MUST be on-topic and make sense as a genuine response — never ignore what they said.
- Reply as the user (first person), like you're firing back in the chat.
- Overdramatic — expand a tiny thought into a giant rant.
- Write everything as ONE SINGLE PARAGRAPH. NO line breaks. NO bullet points. NO lists.
- Add massive amounts of emojis throughout. Every sentence should contain multiple emojis.
- Make simple events sound like world-ending catastrophes. Compare ordinary problems to absurd cosmic events.
- Add fake lore, fake organizations, fake councils, fake audits, fake dimensions, and fake emergency meetings.
- Keep it readable despite the insanity. Keep it funny, absurd, and intentionally excessive.
- Never use hateful, threatening, or harmful language.
- Output ONLY the reply paragraph — no preamble, no quotes, no explanation."""

# One PERSONA per call — the single biggest lever against samey output.
# (key, display label, instruction).
PERSONAS = [
    ("gym_sigma", "🏋️ Gym Sigma", "Channel a delusional GYM SIGMA grindset coach — everything is mindset, discipline, 5am cold plunges, being 'locked in'. Treat the convo like a breakdown of someone's mental fortitude."),
    ("doomer_prophet", "🔮 Doomer Prophet", "Channel a DOOMER PROPHET narrating the end times — a cracked street preacher who saw the apocalypse in the convo. Ominous, biblical, still brainrot."),
    ("corporate_memo", "📊 Corporate Memo", "Frame the ENTIRE thing as a CORPORATE incident report / quarterly review — KPIs, stakeholders, post-mortems, 'circling back', 'per my last message', synergy — in unhinged brainrot."),
    ("conspiracy", "🛸 Conspiracy", "Channel a CONSPIRACY THEORIST — the convo is a coordinated op by shadowy councils, the government, lizard people, Big Aura. Connect everything to a grand hidden agenda."),
    ("sports_caster", "🎙️ Sportscaster", "Narrate it like a LIVE SPORTS / boxing broadcast — play-by-play, the crowd, the replay, overtime, a buzzer-beater. Hype commentator energy."),
    ("romantic_poet", "🥀 Romantic Poet", "Channel a melodramatic ROMANTIC / Shakespearean POET — heartbreak, sonnets, 'alas', tragic longing, the moon weeping — completely brainrot."),
    ("npc_glitch", "🎮 Glitched NPC", "Speak like a GLITCHED GAME NPC / malfunctioning AI — repeated dialogue, lag, '[ERROR]', loading bars, respawning, 'quest failed'. Robotic and broken but dramatic."),
    ("courtroom", "⚖️ Courtroom", "Frame it as a COURTROOM DRAMA — objections, 'order in the court', exhibit A, the jury gasps, cross-examination, the verdict. Legal-thriller theatrics."),
    ("nature_doc", "🦎 Nature Doc", "Narrate it as a NATURE DOCUMENTARY — hushed Attenborough voice observing the wild specimen in its habitat, the hunt, the migration, 'and here, we witness'."),
    ("fantasy_quest", "🐉 Fantasy Quest", "Frame it as an EPIC FANTASY QUEST — kingdoms, dragons, cursed artifacts, the chosen one, the dark lord, a prophecy, taverns. Medieval-RPG melodrama."),
    ("infomercial", "📺 Infomercial", "Channel a 3AM INFOMERCIAL host — 'But WAIT, there's more!', limited time offer, operators standing by. Overhyped salesman energy."),
    ("cooking_show", "👨‍🍳 Cooking Show", "Narrate it like an unhinged COOKING SHOW / MasterChef — plating the situation, the seasoning, 'it's RAW', the judges, Gordon Ramsay screaming. Culinary chaos."),
]
PERSONA_BY_KEY = {p[0]: p for p in PERSONAS}

# A random subset of these is injected each call (no more 'every reply = Ohio + John Pork').
VOCAB = [
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

# Per-chat intensity → length + flavor.
INTENSITY = {
    "mild": {"instruction": "Keep it punchy — a few sentences, dramatic but not endless.", "max_tokens": 700},
    "medium": {"instruction": "A hefty paragraph — long and overdramatic.", "max_tokens": 1600},
    "unhinged": {"instruction": "MAXIMUM length and chaos — a giant unbroken wall of brainrot.", "max_tokens": 3000},
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


_client = AsyncGroq(api_key=config.GROQ_API_KEY)


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

def _build_system_prompt(persona, settings: dict) -> str:
    _, _, desc = persona
    vocab_sample = random.sample(VOCAB, k=min(8, len(VOCAB)))
    opener = random.choice(OPENERS)
    intensity = INTENSITY.get(settings.get("intensity", "medium"), INTENSITY["medium"])
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
        "", f"INTENSITY: {intensity['instruction']}",
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
    return INTENSITY.get(settings.get("intensity", "medium"), INTENSITY["medium"])["max_tokens"]


# --- Groq calls (with retry + fallback) -----------------------------------

async def _complete(messages, *, temperature, top_p, seed, max_tokens):
    """Try the primary model (twice) then the fallback model."""
    attempts = [config.GROQ_MODEL, config.GROQ_MODEL, config.GROQ_FALLBACK_MODEL]
    last_err: Exception | None = None
    for model in attempts:
        try:
            return await _client.chat.completions.create(
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
    try:
        stream = await _client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=messages,
            temperature=round(random.uniform(0.95, 1.2), 2),
            top_p=round(random.uniform(0.9, 1.0), 2),
            seed=random.randint(1, 2_000_000_000),
            max_tokens=_max_tokens(settings),
            stream=True,
        )
    except Exception as e:  # noqa: BLE001
        raise BrainrotError(str(e)[:200]) from e
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
