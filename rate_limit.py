"""Simple in-memory rate limiter for a public bot.

Protects the Groq quota with three guards:
  - per-user cooldown between generations
  - per-user cap per rolling 60s
  - global cap per rolling 60s
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

    def seed(self, events):
        """Pre-populate windows from persisted (user_id, epoch) events.

        Lets per-minute limits survive a restart instead of resetting to zero.
        Wall-clock epochs are mapped into the monotonic-clock domain.
        """
        if not events:
            return
        now_wall = time.time()
        now_mono = time.monotonic()
        for user_id, ts in events:
            mono = now_mono - (now_wall - ts)
            self._user_hits[user_id].append(mono)
            self._global_hits.append(mono)
            last = self._last.get(user_id)
            if last is None or mono > last:
                self._last[user_id] = mono
        for dq in self._user_hits.values():
            dq_sorted = deque(sorted(dq))
            dq.clear()
            dq.extend(dq_sorted)
        g = deque(sorted(self._global_hits))
        self._global_hits.clear()
        self._global_hits.extend(g)
