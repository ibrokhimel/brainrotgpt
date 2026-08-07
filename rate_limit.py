"""Simple in-memory rate limiter for a public bot.

Stops one user hammering the bot, with three guards:
  - per-user cooldown between messages
  - per-user cap per rolling 60s
  - global cap per rolling 60s

Deliberately in-memory only: the limits reset on restart, and that is fine.
The Groq quota is protected by budget.py, which caps proactive calls globally
and persists across restarts — this class covers the per-user abuse case,
which a restart does not carry over anyway.
"""
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, cooldown_s=8.0, per_user_per_min=15, global_per_min=80):
        self.cooldown_s = cooldown_s
        self.per_user_per_min = per_user_per_min
        self.global_per_min = global_per_min
        self._last = {}                       # user_id -> last timestamp
        self._user_hits = defaultdict(deque)  # user_id -> deque[timestamps]
        self._global_hits = deque()

    @staticmethod
    def _prune(dq, now, window=60.0):
        while dq and now - dq[0] > window:
            dq.popleft()

    def check(self, user_id):
        """Return (allowed: bool, reason: str | None) without consuming a slot."""
        now = time.monotonic()

        last = self._last.get(user_id)
        if last is not None and now - last < self.cooldown_s:
            wait = self.cooldown_s - (now - last)
            return False, f"chill {wait:.0f}s ⏳ the aura economy 📈 needs to recharge"

        uh = self._user_hits[user_id]
        self._prune(uh, now)
        if len(uh) >= self.per_user_per_min:
            return False, "you're going too hard 😭🙏 wait a minute before the next one"

        self._prune(self._global_hits, now)
        if len(self._global_hits) >= self.global_per_min:
            return False, "the skibidi council 🚽👑 is overloaded rn, try again in a sec"

        return True, None

    def record(self, user_id):
        """Consume a slot — call only when a generation actually runs."""
        now = time.monotonic()
        self._last[user_id] = now
        self._user_hits[user_id].append(now)
        self._global_hits.append(now)

