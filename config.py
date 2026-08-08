"""Loads configuration and secrets from the .env file."""
import os

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value or value.startswith("your-"):
        raise RuntimeError(
            f"Missing {key}. Copy .env.example to .env and fill in your real values."
        )
    return value


def _flag(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _id_set(key: str) -> set[int]:
    raw = os.getenv(key, "")
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


# --- Secrets --------------------------------------------------------------
BOT_TOKEN = _require("BOT_TOKEN")
GROQ_API_KEY = _require("GROQ_API_KEY")
# Extra Groq keys (comma/semicolon separated) tried in order when the primary
# key runs out of tokens / hits its rate limit. GROQ_KEYS = primary + backups.
GROQ_BACKUP_KEYS = [
    k.strip() for k in os.getenv("GROQ_BACKUP_KEYS", "").replace(";", ",").split(",")
    if k.strip()
]
GROQ_KEYS = [GROQ_API_KEY] + [k for k in GROQ_BACKUP_KEYS if k != GROQ_API_KEY]

# --- Models ---------------------------------------------------------------
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# Used if the primary model errors out (retry/fallback).
GROQ_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")
# Multimodal model for reading forwarded screenshots.
GROQ_VISION_MODEL = os.getenv(
    "GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"
)

# --- Generation defaults (overridable per-chat via /settings) -------------
DEFAULT_PERSONA = os.getenv("DEFAULT_PERSONA", "random")
DEFAULT_INTENSITY = os.getenv("DEFAULT_INTENSITY", "medium")  # mild|medium|unhinged (chaos level)
DEFAULT_LENGTH = os.getenv("DEFAULT_LENGTH", "short")  # short|medium|long|max (output length)
DEFAULT_TONE = os.getenv("DEFAULT_TONE", "default")
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "auto")  # auto = match the convo
DEFAULT_CANDIDATES = _int("DEFAULT_CANDIDATES", 1)  # best-of-N (1 = single reply)
MAX_CANDIDATES = _int("MAX_CANDIDATES", 3)
# Group mode: how many recent messages to buffer per group so an @mention can
# reply off the recent context. Needs privacy mode OFF in BotFather to see them.
GROUP_HISTORY_SIZE = _int("GROUP_HISTORY_SIZE", 15)
STREAMING = _flag("STREAMING", False)  # stream tokens into the message as they arrive
MAX_TRANSCRIPT_CHARS = _int("MAX_TRANSCRIPT_CHARS", 6000)  # token-budget guard

# --- Live trends (auto-refresh the brainrot vocab) ------------------------
# Best-effort: a daily job pulls hot titles from public subreddits and an LLM
# extracts current slang. Owner curates via /trend. Failures are non-fatal.
TREND_FETCH_ENABLED = _flag("TREND_FETCH_ENABLED", True)
TREND_SUBREDDITS = [
    s.strip() for s in os.getenv(
        "TREND_SUBREDDITS",
        # r/OutOfTheLoop is the highest-signal free source there is — people ask
        # "what does X mean" exactly as a meme peaks. r/InstagramReels reaches
        # Instagram content through Reddit's public JSON instead of IG's wall.
        "OutOfTheLoop,brainrot,memes,dankmemes,GenZ,teenagers,tiktokcringe,InstagramReels",
    ).split(",") if s.strip()
]
KYM_FETCH_ENABLED = _flag("KYM_FETCH_ENABLED", True)

# --- Web lookup (the kid checking something instead of bluffing) ----------
# The model gets one tool and decides for itself when it needs it. DuckDuckGo,
# so there is no API key to provision. Off switches the tool off entirely — no
# redeploy, no code change — and the kid falls back to admitting it doesn't know.
WEB_SEARCH_ENABLED = _flag("WEB_SEARCH_ENABLED", True)

# --- Recall (the kid reaching past the 40-message window) ------------------
# FTS5 over every message ever stored, so a conversation from last week is still
# reachable. Local, so it costs no network round trip. Off leaves the kid with
# the rolling window and the facts list, exactly as before.
RECALL_ENABLED = _flag("RECALL_ENABLED", True)

TREND_FETCH_HOUR = _int("TREND_FETCH_HOUR", 5)  # daily auto-fetch hour (server local)
TREND_MAX_ADD = _int("TREND_MAX_ADD", 25)  # cap terms stored per auto refresh

# --- Rate limiting (public-bot protection) --------------------------------
RL_COOLDOWN_S = _float("RL_COOLDOWN_S", 8)
RL_PER_USER_PER_MIN = _int("RL_PER_USER_PER_MIN", 15)
RL_GLOBAL_PER_MIN = _int("RL_GLOBAL_PER_MIN", 80)

# --- Access control -------------------------------------------------------
# If PRIVATE_MODE is on, only OWNER_IDS may generate. Owner-only commands
# (/stats) always require OWNER_IDS regardless of PRIVATE_MODE.
OWNER_IDS = _id_set("OWNER_IDS")
PRIVATE_MODE = _flag("PRIVATE_MODE", False)

# --- Persistence ----------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "brainrot.db")

# --- Ops: health check, webhook, logging ----------------------------------
HEALTH_PORT = _int("HEALTH_PORT", 0)  # 0 = disabled
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # empty = long polling
WEBHOOK_LISTEN = os.getenv("WEBHOOK_LISTEN", "0.0.0.0")
WEBHOOK_PORT = _int("WEBHOOK_PORT", 8443)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "").strip()  # empty = stderr only
# Refuse to start a second polling instance of the same bot (prevents the
# getUpdates 409 "terminated by other getUpdates request" conflict).
SINGLE_INSTANCE_LOCK = _flag("SINGLE_INSTANCE_LOCK", True)

# --- Daily brainrot -------------------------------------------------------
DAILY_DEFAULT_HOUR = _int("DAILY_DEFAULT_HOUR", 9)  # local server hour for /daily

# --- Outbound budget (protects the Groq quota) ----------------------------
# Ghost pings, cold opens, notes distillation and the daily life state are LLM
# calls nobody asked for, and they scale with chat count. This caps them per day.
# Replies to real users are NEVER budgeted. 0 = unlimited.
OUTBOUND_DAILY_BUDGET = _int("OUTBOUND_DAILY_BUDGET", 300)

# --- The kid's day --------------------------------------------------------
SCHOOL_START_HOUR = _int("SCHOOL_START_HOUR", 8)
SCHOOL_END_HOUR = _int("SCHOOL_END_HOUR", 15)
LIFE_REFRESH_HOUR = _int("LIFE_REFRESH_HOUR", 6)  # when the daily life state regenerates

# --- Stickers -------------------------------------------------------------
# The short name of a Telegram sticker pack (the bit after t.me/addstickers/).
# Empty = stickers disabled. The pack is re-read daily, so adding stickers in
# Telegram makes them available to the kid without a redeploy.
STICKER_PACK_NAME = os.getenv("STICKER_PACK_NAME", "").strip()
STICKER_RANDOM_CHANCE = _float("STICKER_RANDOM_CHANCE", 0.07)

# --- Proactive behaviour --------------------------------------------------
GHOST_ENABLED = _flag("GHOST_ENABLED", True)
COLDOPEN_ENABLED = _flag("COLDOPEN_ENABLED", True)
COLDOPEN_MIN_BOND = _int("COLDOPEN_MIN_BOND", 10)
MAX_PINGS_PER_DAY = _int("MAX_PINGS_PER_DAY", 3)
