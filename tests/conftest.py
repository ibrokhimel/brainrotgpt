"""Test bootstrap: provide dummy secrets so config imports without a real .env.

All three are FORCED, not defaulted. With setdefault, a developer who has real
credentials exported in their shell runs the whole suite against the live bot
token and a billable Groq key.
"""
import os

import pytest

os.environ["BOT_TOKEN"] = "test-bot-token"
os.environ["GROQ_API_KEY"] = "test-groq-key"
os.environ["GROQ_BACKUP_KEYS"] = ""


@pytest.fixture(autouse=True)
def _never_really_wait(monkeypatch):
    """No test may spend the 429 backoff in wall time.

    A throttled reply waits ~31s for real, so a single test that raises a
    429-shaped error and forgets to say so turns the suite from seconds into
    minutes — which is exactly how this suite first hung. Neutralised for
    everyone by default; the tests that assert the schedule patch `_sleep`
    themselves and win, because the later monkeypatch is the one that lands.
    """
    import gemini

    async def instant(seconds):
        return None

    monkeypatch.setattr(gemini, "_sleep", instant)
