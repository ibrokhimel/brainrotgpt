"""Live trend refresh — keep the brainrot vocab current.

The generation model is frozen (no web access), so freshness has to come from
outside: this module pulls hot post titles from a few public subreddits and asks
the LLM to extract the slang/meme terms showing up right now, then stores them as
'auto' trends (db.add_trend). The owner curates on top via /trend add|ban|remove.

Best-effort by design: every external step is wrapped so a network hiccup, a
rate-limit, or a bad model response just yields fewer terms — never an exception
that reaches the bot. Reddit's public JSON is used at low volume (once daily) with
a descriptive User-Agent; if it blocks, swap TREND_SUBREDDITS or disable fetching.
"""
import logging
import re

import httpx
from groq import AsyncGroq

import config
import db

logger = logging.getLogger("brainrotgpt.trends")

_client = AsyncGroq(api_key=config.GROQ_API_KEY)
# Backup-key clients, tried in order if the primary key is out of tokens.
_clients = [_client] + [AsyncGroq(api_key=k) for k in config.GROQ_KEYS[1:]]

# Conservative brand-safety guard on top of the model instruction — drop any
# candidate containing these substrings (sexual / hateful / self-harm / slurs).
_DENY = (
    "sex", "porn", "nude", "nsfw", "onlyfans", "rape", "kill", "suicide",
    "self harm", "selfharm", "nigg", "fag", "retard", "slur", "kys", "incest",
)

# A clean slang term is short, mostly word characters, no URLs or sentences.
_TERM_OK = re.compile(r"^[#@]?[\w][\w '/-]{0,38}$")


def _is_safe(term: str) -> bool:
    t = term.lower()
    return not any(bad in t for bad in _DENY)


def _parse_terms(raw: str) -> list[str]:
    """Turn a model's comma/newline list into clean, deduped, safe terms."""
    out: list[str] = []
    seen: set[str] = set()
    for piece in re.split(r"[,\n;]", raw or ""):
        t = piece.strip()
        # strip a leading list marker (a bullet, or "1." / "2)" enumeration) but
        # NOT a standalone number that IS the term (e.g. "67")
        t = re.sub(r"^\s*(?:[-*•]\s*|\d+[.)]\s+)", "", t).strip().strip('"').strip("'").strip()
        if not t or len(t) > 40 or (" " in t and len(t.split()) > 3):
            continue
        if not _TERM_OK.match(t) or not _is_safe(t):
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


async def _fetch_reddit_titles(subreddits, per: int = 25, timeout: float = 10.0) -> list[str]:
    titles: list[str] = []
    headers = {"User-Agent": "brainrotgpt/1.0 (trend refresh; contact: bot owner)"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as cx:
            for sub in subreddits:
                try:
                    r = await cx.get(
                        f"https://www.reddit.com/r/{sub}/hot.json", params={"limit": per}
                    )
                    r.raise_for_status()
                    children = r.json().get("data", {}).get("children", [])
                    for c in children:
                        title = (c.get("data") or {}).get("title")
                        if title:
                            titles.append(title)
                except Exception as e:  # noqa: BLE001 — one bad sub shouldn't kill the rest
                    logger.warning("trend fetch failed for r/%s: %s", sub, e)
    except Exception as e:  # noqa: BLE001 — client construction / DNS / etc.
        logger.warning("trend fetch client error: %s", e)
    return titles


async def _extract_terms(titles: list[str]) -> list[str]:
    if not titles:
        return []
    sample = "\n".join(titles[:80])
    prompt = (
        "Below are recent social-media post titles. Extract the current Gen-Z / "
        "brainrot / TikTok-meme SLANG TERMS or meme names that appear or are clearly "
        "implied. Return ONLY a comma-separated list of short terms (1-3 words each) "
        "— no numbering, no explanations, no sentences. Skip anything sexual, hateful, "
        "violent, or about self-harm.\n\nTITLES:\n" + sample
    )
    raw = ""
    last_err: Exception | None = None
    for client in _clients:  # fall through to a backup key if the primary is tapped out
        try:
            resp = await client.chat.completions.create(
                model=config.GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=300,
            )
            raw = resp.choices[0].message.content or ""
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
    if last_err is not None and not raw:
        logger.warning("trend extraction failed on all keys: %s", last_err)
        return []
    return _parse_terms(raw)


async def refresh(limit: int | None = None) -> int:
    """Fetch → extract → store. Returns the number of NEW terms added."""
    if not config.TREND_FETCH_ENABLED:
        return 0
    limit = config.TREND_MAX_ADD if limit is None else limit
    titles = await _fetch_reddit_titles(config.TREND_SUBREDDITS)
    terms = await _extract_terms(titles)
    if not terms:
        logger.info("trend refresh: no terms (titles=%d)", len(titles))
        return 0
    banned = db.banned_trend_terms()
    added = 0
    for t in terms[:limit]:
        if t.lower() in banned:
            continue
        if db.add_trend(t, source="auto"):
            added += 1
    logger.info("trend refresh: +%d new term(s) from %d titles", added, len(titles))
    return added
