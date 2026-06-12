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
        "TREND_SUBREDDITS", "brainrot,GenZ,teenagers,tiktokcringe"
    ).split(",") if s.strip()
]
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
