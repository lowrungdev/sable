"""Per-conversation rolling chat history, and recent messages by id.

In-process and intentionally simple: it is a cache, not a record. History is
lost on restart, which is the right default for a chat bot - swap this class for
a Redis- or SQLite-backed one if you need durability.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
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


@dataclass(frozen=True)
class CachedMessage:
    """A chat message we saw go past, kept so a reaction can refer to it."""

    author: str
    text: str


class MessageCache:
    """Recent messages per conversation, keyed by message id.

    A reaction event names the message it is attached to by id and nothing more:
    Talk does not include the text, and the bot API has no way to read a message
    back - that needs a user account, not bot credentials. So the only way to act
    on "the message someone reacted to" is to have kept it when it went past.

    Bounded and expiring, like :class:`History`, and populated only while a
    feature needs it. Reacting to a message older than the cache is a miss, which
    the caller reports rather than guessing.
    """

    def __init__(self, max_messages: int = 200, ttl: int = 3600) -> None:
        self._max = max(1, max_messages)
        self._ttl = ttl
        self._rooms: dict[str, OrderedDict[int, tuple[CachedMessage, float]]] = {}

    def add(
        self,
        room: str,
        message_id: int,
        author: str,
        text: str,
        *,
        now: float | None = None,
    ) -> None:
        if not message_id or not text:
            return
        now = time.monotonic() if now is None else now
        room_cache = self._rooms.setdefault(room, OrderedDict())
        room_cache[message_id] = (CachedMessage(author, text), now)
        room_cache.move_to_end(message_id)
        while len(room_cache) > self._max:
            room_cache.popitem(last=False)

    def get(self, room: str, message_id: int, *, now: float | None = None) -> CachedMessage | None:
        now = time.monotonic() if now is None else now
        room_cache = self._rooms.get(room)
        if not room_cache:
            return None
        entry = room_cache.get(message_id)
        if entry is None:
            return None
        message, at = entry
        if self._ttl > 0 and now - at > self._ttl:
            del room_cache[message_id]
            return None
        return message

    def clear(self, room: str) -> int:
        room_cache = self._rooms.pop(room, None)
        return len(room_cache) if room_cache else 0
