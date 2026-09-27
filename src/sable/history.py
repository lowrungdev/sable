"""Per-conversation rolling chat history.

In-process and intentionally simple: it is a cache, not a record. History is
lost on restart, which is the right default for a chat bot - swap this class for
a Redis- or SQLite-backed one if you need durability.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .llm import Message


@dataclass
class _Entry:
    message: Message
    at: float


class History:
    def __init__(self, max_turns: int = 12, ttl: int = 3600) -> None:
        #: One "turn" is a user message plus its reply, hence the doubling.
        self._max_messages = max(1, max_turns) * 2
        self._ttl = ttl
        self._rooms: dict[str, deque[_Entry]] = {}

    def _prune(self, key: str, now: float) -> deque[_Entry]:
        entries = self._rooms.setdefault(key, deque(maxlen=self._max_messages))
        if self._ttl > 0:
            while entries and now - entries[0].at > self._ttl:
                entries.popleft()
        return entries

    def add(self, key: str, role: str, content: str, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._prune(key, now).append(_Entry({"role": role, "content": content}, now))

    def get(self, key: str, *, now: float | None = None) -> list[Message]:
        now = time.monotonic() if now is None else now
        return [entry.message for entry in self._prune(key, now)]

    def clear(self, key: str) -> int:
        """Forget a conversation. Returns how many messages were dropped."""
        entries = self._rooms.pop(key, None)
        return len(entries) if entries else 0
