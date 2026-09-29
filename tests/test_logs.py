"""Which access lines are worth keeping."""

from __future__ import annotations

import logging

from sable.logs import HealthCheckFilter


def access(path: str, status: int) -> logging.LogRecord:
    """An access record shaped the way uvicorn makes them."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:51974", "GET", path, "1.1", status),
        exc_info=None,
    )


def test_a_probe_that_answered_is_dropped() -> None:
    assert not HealthCheckFilter().filter(access("/healthz", 200))


def test_a_probe_that_failed_is_kept() -> None:
    # The whole reason for filtering rather than turning the access log off:
    # a 401 once a health token is set, or a 503, is the line you want.
    assert HealthCheckFilter().filter(access("/healthz", 503))
    assert HealthCheckFilter().filter(access("/healthz", 401))


def test_every_other_route_is_kept() -> None:
    assert HealthCheckFilter().filter(access("/webhook", 200))
    assert HealthCheckFilter().filter(access("/notify", 201))


def test_a_query_string_does_not_hide_the_path() -> None:
    assert not HealthCheckFilter().filter(access("/healthz?probe=1", 200))


def test_a_path_that_merely_starts_the_same_is_kept() -> None:
    assert HealthCheckFilter().filter(access("/healthz-internal", 200))


def test_anything_not_shaped_like_an_access_line_is_kept() -> None:
    # uvicorn could change this, and letting a line through is the safe
    # direction to be wrong in.
    record = access("/healthz", 200)
    record.args = ("something", "else")
    assert HealthCheckFilter().filter(record)
    record.args = None
    assert HealthCheckFilter().filter(record)
