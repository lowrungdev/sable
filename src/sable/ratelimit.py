"""A per-person limit on how often sable may be set off.

Every trigger can cost something: a command runs, the model is paid for, a
reaction is answered. A sliding window per actor keeps one person - or one
script - from turning a room into a bill. Kept in memory and bounded: a key that
has gone quiet for a whole window is forgotten.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

#: Seconds a trigger counts against its person.
WINDOW = 60.0

#: What :meth:`RateLimiter.hit` answers.
ALLOWED = "allowed"
#: Over the limit, and the first time in this window: worth one log line.
FIRST_REFUSAL = "first"
#: Over the limit again: stay quiet.
REFUSED = "refused"


class RateLimiter:
    def __init__(
        self,
        limit: int,
        *,
        window: float = WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._warned: dict[str, float] = {}
        self._swept = clock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def hit(self, key: str) -> str:
        """Count one trigger from ``key``: ALLOWED, FIRST_REFUSAL or REFUSED.

        A refused trigger is not counted, so somebody who stops is let back in
        one window after their last allowed one rather than never.
        """
        if not self.enabled:
            return ALLOWED
        now = self._clock()
        self._sweep(now)
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) < self.limit:
            hits.append(now)
            self._warned.pop(key, None)
            return ALLOWED
        warned = self._warned.get(key)
        if warned is not None and now - warned < self.window:
            return REFUSED
        self._warned[key] = now
        return FIRST_REFUSAL

    def _sweep(self, now: float) -> None:
        """Drop every key that has been quiet for a window, once per window."""
        if now - self._swept < self.window:
            return
        self._swept = now
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= self.window]:
            del self._hits[key]
        for key in [k for k, at in self._warned.items() if now - at >= self.window]:
            del self._warned[key]

    def __len__(self) -> int:
        return len(self._hits)
