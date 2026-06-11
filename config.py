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


BOT_TOKEN = _require("BOT_TOKEN")
GROQ_API_KEY = _require("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# Rate limiting (public-bot protection)
RL_COOLDOWN_S = float(os.getenv("RL_COOLDOWN_S", "8"))
RL_PER_USER_PER_MIN = int(os.getenv("RL_PER_USER_PER_MIN", "15"))
RL_GLOBAL_PER_MIN = int(os.getenv("RL_GLOBAL_PER_MIN", "80"))
