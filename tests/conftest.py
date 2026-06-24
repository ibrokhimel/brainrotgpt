"""Test bootstrap: provide dummy secrets so config imports without a real .env.

GROQ_BACKUP_KEYS is forced empty so tests never build a client around a real
backup key from a developer's .env (which would make live API calls).
"""
import os

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ["GROQ_BACKUP_KEYS"] = ""
