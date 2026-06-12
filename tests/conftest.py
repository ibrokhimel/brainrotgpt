"""Test bootstrap: provide dummy secrets so config imports without a real .env."""
import os

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
