"""Test bootstrap: provide dummy secrets so config imports without a real .env.

All three are FORCED, not defaulted. With setdefault, a developer who has real
credentials exported in their shell runs the whole suite against the live bot
token and a billable Groq key.
"""
import os

os.environ["BOT_TOKEN"] = "test-bot-token"
os.environ["GROQ_API_KEY"] = "test-groq-key"
os.environ["GROQ_BACKUP_KEYS"] = ""
