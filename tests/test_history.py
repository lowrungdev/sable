from __future__ import annotations

from sable.history import History, MessageCache


def test_keeps_messages_per_room() -> None:
    history = History(max_turns=5)
    history.add("a", "user", "one")
    history.add("b", "user", "two")
    assert history.get("a") == [{"role": "user", "content": "one"}]
    assert history.get("b") == [{"role": "user", "content": "two"}]


def test_trims_to_twice_the_turn_count() -> None:
    history = History(max_turns=2)
    for i in range(10):
        history.add("a", "user", f"m{i}")
    contents = [m["content"] for m in history.get("a")]
    assert contents == ["m6", "m7", "m8", "m9"]


def test_expires_old_messages() -> None:
    history = History(max_turns=10, ttl=60)
    history.add("a", "user", "stale", now=0.0)
    history.add("a", "user", "fresh", now=100.0)
    assert [m["content"] for m in history.get("a", now=120.0)] == ["fresh"]


def test_ttl_of_zero_never_expires() -> None:
    history = History(max_turns=10, ttl=0)
    history.add("a", "user", "ancient", now=0.0)
    assert len(history.get("a", now=10**9)) == 1


def test_clear_reports_how_much_it_dropped() -> None:
    history = History()
    history.add("a", "user", "x")
    history.add("a", "assistant", "y")
    assert history.clear("a") == 2
    assert history.get("a") == []
    assert history.clear("a") == 0


# --------------------------------------------------------------------------- #
# MessageCache
# --------------------------------------------------------------------------- #


def test_the_cache_returns_what_it_was_given() -> None:
    cache = MessageCache()
    cache.add("room", 100, "Bob", "hello there")
    got = cache.get("room", 100)
    assert got is not None
    assert (got.author, got.text) == ("Bob", "hello there")


def test_an_unknown_message_or_room_is_a_miss() -> None:
    cache = MessageCache()
    cache.add("room", 100, "Bob", "hello")
    assert cache.get("room", 999) is None
    assert cache.get("other", 100) is None


def test_the_cache_evicts_the_oldest_beyond_its_size() -> None:
    cache = MessageCache(max_messages=3)
    for i in range(1, 6):
        cache.add("room", i, "Bob", f"message {i}")
    assert cache.get("room", 1) is None
    assert cache.get("room", 2) is None
    assert cache.get("room", 5) is not None


def test_rooms_are_separate() -> None:
    cache = MessageCache(max_messages=1)
    cache.add("a", 1, "Bob", "in a")
    cache.add("b", 2, "Bob", "in b")
    assert cache.get("a", 1) is not None
    assert cache.get("b", 2) is not None


def test_entries_expire() -> None:
    cache = MessageCache(ttl=60)
    cache.add("room", 100, "Bob", "stale", now=0.0)
    assert cache.get("room", 100, now=30.0) is not None
    assert cache.get("room", 100, now=120.0) is None


def test_a_ttl_of_zero_never_expires() -> None:
    cache = MessageCache(ttl=0)
    cache.add("room", 100, "Bob", "ancient", now=0.0)
    assert cache.get("room", 100, now=10**9) is not None


def test_empty_text_or_a_missing_id_is_not_cached() -> None:
    cache = MessageCache()
    cache.add("room", 0, "Bob", "no id")
    cache.add("room", 100, "Bob", "")
    assert cache.get("room", 0) is None
    assert cache.get("room", 100) is None


def test_clear_reports_what_it_dropped() -> None:
    cache = MessageCache()
    cache.add("room", 1, "Bob", "one")
    cache.add("room", 2, "Bob", "two")
    assert cache.clear("room") == 2
    assert cache.get("room", 1) is None
