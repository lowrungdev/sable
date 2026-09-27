from __future__ import annotations

from sable.history import History


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
