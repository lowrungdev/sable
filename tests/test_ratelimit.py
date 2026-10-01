from __future__ import annotations

from sable.ratelimit import ALLOWED, FIRST_REFUSAL, REFUSED, RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def limiter(limit: int = 3) -> tuple[RateLimiter, Clock]:
    clock = Clock()
    return RateLimiter(limit, clock=clock), clock


def test_the_limit_applies_per_person_within_a_minute() -> None:
    rl, _ = limiter(3)
    assert [rl.hit("users/alice") for _ in range(3)] == [ALLOWED] * 3
    assert rl.hit("users/alice") == FIRST_REFUSAL
    assert rl.hit("users/bob") == ALLOWED


def test_only_the_first_refusal_in_a_window_is_marked() -> None:
    rl, _ = limiter(1)
    rl.hit("a")
    assert [rl.hit("a") for _ in range(4)] == [FIRST_REFUSAL, REFUSED, REFUSED, REFUSED]


def test_the_window_slides() -> None:
    rl, clock = limiter(2)
    rl.hit("a")
    clock.now += 30
    rl.hit("a")
    assert rl.hit("a") == FIRST_REFUSAL
    clock.now += 31  # the first is now 61 s old, the second 31 s
    assert rl.hit("a") == ALLOWED
    assert rl.hit("a") == FIRST_REFUSAL


def test_a_refused_trigger_is_not_counted_so_stopping_restores_access() -> None:
    rl, clock = limiter(1)
    rl.hit("a")
    for _ in range(10):
        clock.now += 5
        rl.hit("a")
    clock.now = 1000.0 + 61
    assert rl.hit("a") == ALLOWED


def test_it_warns_again_in_a_later_window() -> None:
    rl, clock = limiter(1)
    rl.hit("a")
    assert rl.hit("a") == FIRST_REFUSAL
    clock.now += 61
    assert rl.hit("a") == ALLOWED
    assert rl.hit("a") == FIRST_REFUSAL


def test_zero_disables_it() -> None:
    rl, _ = limiter(0)
    assert all(rl.hit("a") == ALLOWED for _ in range(1000))
    assert len(rl) == 0


def test_idle_keys_are_forgotten() -> None:
    rl, clock = limiter(5)
    for n in range(100):
        rl.hit(f"user{n}")
    assert len(rl) == 100
    clock.now += 61
    rl.hit("someone-new")
    assert len(rl) == 1
    assert rl._warned == {}
