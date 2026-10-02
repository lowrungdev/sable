"""Scheduled triggers: cron and every= parsing, fire detection on a fake clock,
fan-out across rooms, and the manager's reporting. A few tests at the end run
against the real worker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import textwrap
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import respx

from conftest import ROOM, event, make_config
from plugin_helpers import message_route, posix_only, sent, texts, write_plugin
from sable.commands import Registry
from sable.config import Config
from sable.plugin_api import (
    PluginDeclarationError,
    declarations,
    parse_cron,
    parse_every,
    reset_declarations,
    schedule,
)
from sable.plugins import (
    MAX_TOTAL_SCHEDULES,
    MIN_EVERY_SECONDS,
    Status,
    default_command,
)

OTHER = "efgh5678"
THIRD = "zzzz9999"


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    reset_declarations()
    yield
    reset_declarations()


# --------------------------------------------------------------------------- #
# parse_cron
# --------------------------------------------------------------------------- #


def test_a_plain_cron_parses_and_matches() -> None:
    spec = parse_cron("0 8 * * 1-5")
    assert spec.matches(datetime(2024, 1, 1, 8, 0))  # a Monday
    assert not spec.matches(datetime(2024, 1, 1, 8, 1))
    assert not spec.matches(datetime(2024, 1, 1, 9, 0))
    assert not spec.matches(datetime(2024, 1, 6, 8, 0))  # a Saturday


def test_steps_ranges_and_lists() -> None:
    spec = parse_cron("*/15 * * * *")
    assert {m for m in range(60) if spec.matches(datetime(2024, 1, 1, 0, m))} == {0, 15, 30, 45}
    spec = parse_cron("0 9-17 * * *")
    assert spec.matches(datetime(2024, 1, 1, 9, 0))
    assert spec.matches(datetime(2024, 1, 1, 17, 0))
    assert not spec.matches(datetime(2024, 1, 1, 8, 0))
    assert not spec.matches(datetime(2024, 1, 1, 18, 0))
    spec = parse_cron("0 0 1,15 * *")
    assert spec.matches(datetime(2024, 3, 1, 0, 0))
    assert spec.matches(datetime(2024, 3, 15, 0, 0))
    assert not spec.matches(datetime(2024, 3, 16, 0, 0))
    spec = parse_cron("0 0 */10 * *")
    assert {d for d in range(1, 32) if spec.matches(datetime(2024, 1, d, 0, 0))} == {
        1,
        11,
        21,
        31,
    }


def test_a_bare_number_with_a_step_is_an_implicit_range_to_the_fields_maximum() -> None:
    # Real crontab's reading of "0/5": every 5 minutes STARTING at 0, not just
    # minute 0 alone. The implicit range runs to the field's own maximum.
    spec = parse_cron("0/5 * * * *")
    assert {m for m in range(60) if spec.matches(datetime(2024, 1, 1, 0, m))} == set(
        range(0, 60, 5)
    )
    # 10/20 for hours (0-23): 10, then 30 - clipped away by the field's own range.
    spec = parse_cron("* 10/20 * * *")
    assert {h for h in range(24) if spec.matches(datetime(2024, 1, 1, h, 0))} == {10}
    # A bare number with NO step still means just that one value, as always.
    spec = parse_cron("5 * * * *")
    assert {m for m in range(60) if spec.matches(datetime(2024, 1, 1, 0, m))} == {5}


@pytest.mark.parametrize(
    "expr",
    [
        "0 8 * *",  # too few fields
        "0 8 * * * *",  # too many
        "60 * * * *",  # minute out of range
        "* 24 * * *",  # hour out of range
        "* * 0 * *",  # day out of range (1-31)
        "* * 32 * *",  # day out of range
        "* * * 0 *",  # month out of range (1-12)
        "* * * 13 *",  # month out of range
        "* * * * 8",  # weekday out of range (0-7)
        "* * * * mon",  # names not supported: numeric only
        "a * * * *",  # not a number
        "*/0 * * * *",  # a step of zero
        "5-1 * * * *",  # a backwards range
        "1,,3 * * * *",  # an empty entry
        "",  # empty entirely
        "   ",
    ],
)
def test_invalid_cron_expressions_are_refused(expr: str) -> None:
    with pytest.raises(PluginDeclarationError):
        parse_cron(expr)


def test_weekday_zero_and_seven_both_mean_sunday() -> None:
    sunday = datetime(2024, 1, 7, 0, 0)
    assert parse_cron("0 0 * * 0").matches(sunday)
    assert parse_cron("0 0 * * 7").matches(sunday)


def test_the_day_or_weekday_quirk_is_an_or_when_both_are_restricted() -> None:
    # The first of the month OR a Monday: either is enough once both are restricted.
    spec = parse_cron("0 0 1 * 1")
    assert spec.matches(datetime(2024, 3, 1, 0, 0))  # the 1st, a Friday
    assert spec.matches(datetime(2024, 3, 4, 0, 0))  # a Monday, not the 1st
    assert not spec.matches(datetime(2024, 3, 5, 0, 0))  # neither


def test_a_wildcard_day_or_weekday_is_a_plain_and() -> None:
    spec = parse_cron("0 0 1 * *")  # weekday wildcard: only the day matters
    assert spec.matches(datetime(2024, 3, 1, 0, 0))
    assert not spec.matches(datetime(2024, 3, 4, 0, 0))
    spec = parse_cron("0 0 * * 1")  # day wildcard: only the weekday matters
    assert spec.matches(datetime(2024, 3, 4, 0, 0))
    assert not spec.matches(datetime(2024, 3, 1, 0, 0))


def test_an_impossible_date_never_matches_and_does_not_hang() -> None:
    spec = parse_cron("0 8 31 2 *")  # the 31st of February: never happens
    started = time.perf_counter()
    assert spec.next_after(datetime(2024, 1, 1, 0, 0), horizon_minutes=60_000) is None
    assert time.perf_counter() - started < 2.0
    # Nor does February the 30th, nor any other day that month never has.
    spec = parse_cron("0 8 30 2 *")
    assert spec.next_after(datetime(2024, 1, 1, 0, 0), horizon_minutes=60_000) is None


def test_next_after_finds_the_first_matching_minute() -> None:
    spec = parse_cron("30 8 * * *")
    nxt = spec.next_after(datetime(2024, 1, 1, 8, 0))
    assert nxt == datetime(2024, 1, 1, 8, 30)
    nxt2 = spec.next_after(nxt)
    assert nxt2 == datetime(2024, 1, 2, 8, 30)


def test_cron_must_be_a_string() -> None:
    with pytest.raises(PluginDeclarationError, match="string"):
        parse_cron(5)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# parse_every
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(60, 60), ("60s", 60), ("1m", 60), ("1h", 3600), ("1d", 86400), (120, 120)],
)
def test_every_parses_seconds_and_durations(value, seconds) -> None:
    assert parse_every(value) == seconds


@pytest.mark.parametrize("value", [1, 30, "30s", "59s", 0, -60])
def test_every_below_the_minimum_is_refused(value) -> None:
    with pytest.raises(PluginDeclarationError, match="minute"):
        parse_every(value)


def test_every_exactly_the_minimum_is_fine() -> None:
    assert parse_every(MIN_EVERY_SECONDS) == MIN_EVERY_SECONDS
    assert parse_every(f"{MIN_EVERY_SECONDS}s") == MIN_EVERY_SECONDS


def test_every_above_a_week_is_refused() -> None:
    with pytest.raises(PluginDeclarationError, match="week"):
        parse_every(7 * 24 * 3600 + 1)


@pytest.mark.parametrize("value", [True, False, 1.5, None, [], "soon", "5"])
def test_every_rejects_bad_types_and_text(value) -> None:
    with pytest.raises(PluginDeclarationError):
        parse_every(value)


# --------------------------------------------------------------------------- #
# @schedule
# --------------------------------------------------------------------------- #


def test_schedule_needs_exactly_one_of_cron_or_every() -> None:
    with pytest.raises(PluginDeclarationError, match="exactly one"):
        schedule()
    with pytest.raises(PluginDeclarationError, match="exactly one"):
        schedule(cron="0 8 * * *", every="1h")
    assert declarations().schedules == []


def test_a_positional_argument_is_a_clear_error() -> None:
    with pytest.raises(PluginDeclarationError, match="needs arguments"):
        schedule("0 8 * * *")  # type: ignore[call-overload]


def test_a_cron_schedule_declares_correctly() -> None:
    @schedule(cron="0 8 * * 1-5")
    async def standup(ctx):
        return None

    [decl] = declarations().schedules
    assert decl.id == "standup"
    assert decl.cron == "0 8 * * 1-5"
    assert decl.every is None
    assert decl.handler is standup


def test_an_every_schedule_declares_correctly() -> None:
    @schedule(every="10m")
    async def poll(ctx):
        return None

    decl = declarations().schedules[0]
    assert decl.cron is None
    assert decl.every == 600


def test_a_bad_cron_is_refused_at_declaration_time() -> None:
    with pytest.raises(PluginDeclarationError):
        schedule(cron="nonsense")
    assert declarations().schedules == []


def test_the_handler_must_be_async_and_take_one_argument() -> None:
    with pytest.raises(PluginDeclarationError, match="async def"):

        @schedule(every="1m")
        def sync(ctx):
            return None

    with pytest.raises(PluginDeclarationError, match="exactly one argument"):

        @schedule(every="1m")
        async def two(ctx, extra):
            return None


def test_a_handler_id_may_not_repeat_across_kinds() -> None:
    @schedule(every="1m")
    async def job(ctx):
        return None

    with pytest.raises(PluginDeclarationError, match="more than once"):
        schedule(every="2m")(job)


def test_schedules_share_the_handler_cap_with_commands_and_phrases() -> None:
    from sable.plugin_api import command

    for number in range(31):

        @command(f"c{number}")
        async def one(ctx):
            return None

    @schedule(every="1m")
    async def last(ctx):
        return None

    assert declarations().count == 32
    with pytest.raises(PluginDeclarationError, match="at most 32"):

        @schedule(every="2m")
        async def too_many(ctx):
            return None


def test_a_rejected_schedule_leaves_the_registry_as_it_was() -> None:
    @schedule(every="1m")
    async def job(ctx):
        return None

    snapshot = declarations()
    with pytest.raises(PluginDeclarationError):
        schedule()
    assert declarations().schedules == snapshot.schedules


# --------------------------------------------------------------------------- #
# Rig helpers
# --------------------------------------------------------------------------- #


def cron_handler(handler: str = "job", expr: str = "0 8 * * 1-5") -> dict[str, Any]:
    return {"id": handler, "cron": expr, "every": None}


def every_handler(handler: str = "job", seconds: int = 60) -> dict[str, Any]:
    return {"id": handler, "cron": None, "every": seconds}


def schedules_only(
    *schedules: dict[str, Any], behaviours: dict[str, str] | None = None, **extra: Any
) -> dict[str, Any]:
    """Settings for the fake worker: a plugin that declares only these schedules."""
    settings: dict[str, Any] = {
        "declare": {"commands": [], "phrases": [], "schedules": list(schedules)},
        **extra,
    }
    if behaviours:
        settings["schedule"] = behaviours
    return settings


def said(route) -> list[str]:
    return texts(route)


# --------------------------------------------------------------------------- #
# Declarations as the core accepts them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sched", "why"),
    [
        ({"id": "job", "cron": None, "every": None}, "exactly one"),
        ({"id": "job", "cron": "0 8 * * *", "every": 60}, "exactly one"),
        ({"id": "job", "cron": "nonsense", "every": None}, "space-separated"),
        ({"id": "job", "cron": None, "every": 30}, "greater than or equal to"),
        ({"id": "job", "cron": None, "every": 10**9}, "less than or equal to"),
        ({"id": "not an id", "cron": None, "every": 60}, "legal handler id"),
        ({"id": "", "cron": None, "every": 60}, "legal handler id"),
        ({k: v for k, v in every_handler().items() if k != "id"}, "id"),
    ],
)
async def test_an_invalid_schedule_declaration_fails_the_plugin(rigs, tmp_path, sched, why) -> None:
    write_plugin(tmp_path, "liar", settings=schedules_only(sched))
    rig = await rigs(tmp_path)
    record = rig.record("liar")
    assert record.status is Status.FAILED, record.state
    assert why in record.reason


async def test_a_schedule_id_colliding_with_a_phrase_id_fails_the_plugin(rigs, tmp_path) -> None:
    settings = {
        "declare": {
            "commands": [],
            "phrases": [{"id": "x", "any": ["gm"], "whole_words": True, "cooldown": 0}],
            "schedules": [every_handler("x")],
        }
    }
    write_plugin(tmp_path, "liar", settings=settings)
    rig = await rigs(tmp_path)
    assert rig.record("liar").status is Status.FAILED
    assert "declared twice" in rig.record("liar").reason


async def test_a_plugin_with_only_schedules_is_valid(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=schedules_only(every_handler()))
    rig = await rigs(tmp_path)
    assert rig.record("greeter").status is Status.ACTIVE
    assert rig.manager.commands() == []


# --------------------------------------------------------------------------- #
# Fire detection: cron
# --------------------------------------------------------------------------- #


@respx.mock
async def test_cron_fires_exactly_at_the_matching_minute(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(cron_handler(expr="1 0 * * *")))
    rig = await rigs(tmp_path)
    route = message_route()
    base = rig.clock.wall()  # 2024-01-01 00:00 UTC, a Monday
    await rig.manager.scheduler_tick(base)
    await rig.manager.wait_for_schedules()
    assert route.call_count == 0  # not yet 00:01
    await rig.manager.scheduler_tick(base + timedelta(minutes=1))
    await rig.manager.wait_for_schedules()
    assert said(route) == ["p/job fired in abcd1234"]


@respx.mock
async def test_cron_does_not_double_fire_within_the_same_minute(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(cron_handler(expr="0 0 * * *")))
    rig = await rigs(tmp_path)
    route = message_route()
    moment = rig.clock.wall()
    for _ in range(3):  # the tick loop runs repeatedly; the minute does not change
        await rig.manager.scheduler_tick(moment)
    await rig.manager.wait_for_schedules()
    assert route.call_count == 1


@respx.mock
async def test_cron_does_not_replay_after_downtime(rigs, tmp_path) -> None:
    """Missed runs are never replayed: skipping straight past many matching
    minutes without a tick in between fires only for the minute actually checked."""
    write_plugin(tmp_path, "p", settings=schedules_only(cron_handler(expr="0 * * * *")))
    rig = await rigs(tmp_path)
    route = message_route()
    moment = rig.clock.wall()
    # Jump 100 hours ahead - 100 missed matching minutes - in one step, as if the
    # bot had been down the whole time.
    await rig.manager.scheduler_tick(moment + timedelta(hours=100))
    await rig.manager.wait_for_schedules()
    assert route.call_count == 1


@respx.mock
async def test_a_clock_adjustment_within_the_minute_cannot_double_fire(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(cron_handler(expr="0 0 * * *")))
    rig = await rigs(tmp_path)
    route = message_route()
    moment = rig.clock.wall()
    await rig.manager.scheduler_tick(moment)
    await rig.manager.scheduler_tick(moment - timedelta(seconds=10))  # time stepped back
    await rig.manager.wait_for_schedules()
    assert route.call_count == 1


# --------------------------------------------------------------------------- #
# Fire detection: every=
# --------------------------------------------------------------------------- #


@respx.mock
async def test_every_fires_after_the_interval_from_load_not_a_fixed_epoch(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(every_handler(seconds=60)))
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.manager.scheduler_tick()
    assert route.call_count == 0
    rig.clock.advance(59)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert route.call_count == 0
    rig.clock.advance(1)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert route.call_count == 1
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert route.call_count == 2


@respx.mock
async def test_every_does_not_replay_missed_intervals(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(every_handler(seconds=60)))
    rig = await rigs(tmp_path)
    route = message_route()
    rig.clock.advance(10 * 60)  # ten missed intervals, as if nobody was checking
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert route.call_count == 1  # one fire, not ten
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert route.call_count == 2  # back to firing normally afterwards


@respx.mock
async def test_two_every_schedules_loaded_together_do_not_have_to_share_a_phase(
    rigs, tmp_path
) -> None:
    """Both still use the same manager-wide monotonic clock; nothing here claims
    they fire at different moments, only that each counts its own interval from
    its own plugin's load time, independently."""
    write_plugin(tmp_path, "a", directory="a", settings=schedules_only(every_handler(seconds=60)))
    write_plugin(tmp_path, "b", directory="b", settings=schedules_only(every_handler(seconds=90)))
    rig = await rigs(tmp_path)
    route = message_route()
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert sorted(said(route)) == ["a/job fired in abcd1234"]
    rig.clock.advance(30)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert sorted(said(route)[1:]) == ["b/job fired in abcd1234"]


# --------------------------------------------------------------------------- #
# Timezone correctness
# --------------------------------------------------------------------------- #


async def test_the_default_wall_clock_follows_sable_timezone(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(every_handler()))
    rig = await rigs(tmp_path, timezone="Europe/Berlin")
    # The fake clock the rig wires in overrides this for determinism; check the
    # real default that production (and --check) actually uses.
    moment = rig.manager._default_wall_clock()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == datetime.now(ZoneInfo("Europe/Berlin")).utcoffset()


@respx.mock
async def test_a_cron_schedule_fires_by_the_wall_clocks_own_zone(rigs, tmp_path) -> None:
    """CronSpec reads whatever local fields the given moment carries; the
    scheduler is correct for any zone as long as it is handed a moment already in
    it - which _default_wall_clock (tested above) and this fake both do."""
    zoned = ZoneInfo("Pacific/Kiritimati")  # UTC+14: about as far from UTC as it gets
    write_plugin(tmp_path, "p", settings=schedules_only(cron_handler(expr="30 8 * * *")))
    rig = await rigs(tmp_path)
    moment = datetime(2024, 6, 1, 8, 30, tzinfo=zoned)
    route = message_route()
    await rig.manager.scheduler_tick(moment)
    await rig.manager.wait_for_schedules()
    assert route.call_count == 1
    # The same instant, read as a UTC wall clock, is 18:30 - nowhere near 8:30 - so
    # if the zone were being discarded this would already have fired for a
    # different reason (or not fired for the right one).
    as_utc = moment.astimezone(UTC).replace(tzinfo=None)
    assert as_utc.hour == 18


@respx.mock
async def test_a_cron_does_not_double_fire_across_a_dst_fall_back(rigs, tmp_path) -> None:
    """Europe/Berlin falls back on 2024-10-27: local 02:00-03:00 happens twice (once
    at UTC+2, once at UTC+1 an hour of real time later). A cron matching 02:30 must
    still fire exactly once - the dedupe key has to be local wall time, not the UTC
    instant, or the second pass through that local minute fires it again."""
    zoned = ZoneInfo("Europe/Berlin")
    write_plugin(tmp_path, "p", settings=schedules_only(cron_handler(expr="30 2 * * *")))
    rig = await rigs(tmp_path)
    route = message_route()
    first_pass = datetime(2024, 10, 27, 2, 30, tzinfo=zoned, fold=0)
    second_pass = datetime(2024, 10, 27, 2, 30, tzinfo=zoned, fold=1)
    # Confirm this zone/date genuinely is the ambiguous DST transition, not a
    # no-op that would make the test meaningless.
    assert first_pass.utcoffset() != second_pass.utcoffset()
    assert first_pass.astimezone(UTC) != second_pass.astimezone(UTC)
    await rig.manager.scheduler_tick(first_pass)
    await rig.manager.scheduler_tick(second_pass)
    await rig.manager.wait_for_schedules()
    assert route.call_count == 1


# --------------------------------------------------------------------------- #
# Who gets scheduled at all
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rooms", "enabled"),
    [(None, True), ([], True), ([ROOM], False)],
)
async def test_inactive_and_disabled_plugins_are_never_scheduled(
    rigs, tmp_path, rooms, enabled
) -> None:
    write_plugin(
        tmp_path, "p", rooms=rooms, enabled=enabled, settings=schedules_only(every_handler())
    )
    rig = await rigs(tmp_path)
    assert rig.manager._schedules == []


async def test_a_plugin_that_failed_to_load_is_never_scheduled(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(every_handler(), load="error"))
    rig = await rigs(tmp_path)
    assert rig.record("p").status is Status.FAILED
    assert rig.manager._schedules == []


@respx.mock
async def test_a_switched_off_plugin_skips_its_fires_without_posting(
    rigs, tmp_path, caplog
) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(every_handler(seconds=60)))
    rig = await rigs(tmp_path)
    route = message_route()
    worker = rig.record("p").worker
    worker._tripped_until = rig.clock() + 300  # the breaker is open
    rig.clock.advance(60)
    with caplog.at_level(logging.INFO):
        await rig.manager.scheduler_tick()
        await rig.manager.wait_for_schedules()
    assert route.call_count == 0
    record = next(r for r in caplog.records if "switched off; skipped" in r.getMessage())
    assert record.levelno == logging.INFO  # routine, not alarming


@respx.mock
async def test_a_switched_off_plugin_resumes_once_its_breaker_half_opens(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(every_handler(seconds=60)))
    rig = await rigs(tmp_path)
    route = message_route()
    worker = rig.record("p").worker
    worker._tripped_until = rig.clock() + 300
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert route.call_count == 0  # skipped while switched off; bookkeeping still advanced
    rig.clock.advance(301)  # past the cooldown: the breaker is ready for a trial
    rig.clock.advance(60)  # and another interval has elapsed
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert route.call_count == 1
    assert not worker.tripped


@respx.mock
async def test_a_switched_off_plugin_does_not_burst_fire_on_recovery(rigs, tmp_path) -> None:
    """Skipped intervals are not replayed just because the plugin came back."""
    write_plugin(tmp_path, "p", settings=schedules_only(every_handler(seconds=60)))
    rig = await rigs(tmp_path)
    route = message_route()
    worker = rig.record("p").worker
    worker._tripped_until = rig.clock() + 300
    for _ in range(4):  # four missed intervals (240s), still inside the cooldown
        rig.clock.advance(60)
        await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert route.call_count == 0
    rig.clock.advance(100)  # past the 300s cooldown now
    await rig.manager.scheduler_tick()  # a 5th interval has also elapsed
    await rig.manager.wait_for_schedules()
    assert route.call_count == 1  # one, not five


# --------------------------------------------------------------------------- #
# Fan-out
# --------------------------------------------------------------------------- #


@respx.mock
async def test_fan_out_to_every_configured_room(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", rooms=[ROOM, OTHER], settings=schedules_only(every_handler()))
    rig = await rigs(tmp_path)
    here = message_route()
    there = message_route(OTHER)
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert said(here) == ["p/job fired in abcd1234"]
    assert said(there) == ["p/job fired in efgh5678"]


@respx.mock
async def test_fan_out_star_expands_to_rooms_currently_followed(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", rooms=["*"], settings=schedules_only(every_handler()))
    rig = await rigs(tmp_path)
    rig.manager.known_rooms = lambda: [ROOM, OTHER]
    here = message_route()
    there = message_route(OTHER)
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert here.call_count == 1
    assert there.call_count == 1


@respx.mock
async def test_star_with_nothing_known_skips_and_warns_once(rigs, tmp_path, caplog) -> None:
    write_plugin(tmp_path, "p", rooms=["*"], settings=schedules_only(every_handler(seconds=60)))
    rig = await rigs(tmp_path)  # known_rooms defaults to returning []
    route = message_route()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            rig.clock.advance(60)
            await rig.manager.scheduler_tick()
        await rig.manager.wait_for_schedules()
    assert route.call_count == 0
    warnings = [m for m in caplog.messages if "no room the account follows is known yet" in m]
    assert len(warnings) == 1  # said once, not every tick


@respx.mock
async def test_the_warning_can_fire_again_after_rooms_become_known_and_then_unknown(
    rigs, tmp_path, caplog
) -> None:
    write_plugin(tmp_path, "p", rooms=["*"], settings=schedules_only(every_handler(seconds=60)))
    rig = await rigs(tmp_path)
    route = message_route()
    rig.manager.known_rooms = list
    with caplog.at_level(logging.WARNING):
        rig.clock.advance(60)
        await rig.manager.scheduler_tick()
        rig.manager.known_rooms = lambda: [ROOM]
        rig.clock.advance(60)
        await rig.manager.scheduler_tick()
        # Let that fire's cohort drain before the next tick: otherwise the new
        # still-draining backlog check (not a "rooms unknown" case at all) would
        # skip the third tick instead of it hitting the rooms-unknown path again.
        await rig.manager.wait_for_schedules()
        rig.manager.known_rooms = list
        rig.clock.advance(60)
        await rig.manager.scheduler_tick()
        await rig.manager.wait_for_schedules()
    assert route.call_count == 1
    warnings = [m for m in caplog.messages if "no room the account follows is known yet" in m]
    assert len(warnings) == 2  # once, then again after it recovered in between


@respx.mock
async def test_a_room_not_allowed_is_excluded_from_the_fan_out(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", rooms=[ROOM, OTHER], settings=schedules_only(every_handler()))
    rig = await rigs(tmp_path, allowed_rooms=[ROOM])
    here = message_route()
    there = message_route(OTHER)
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert here.call_count == 1
    assert there.call_count == 0


@respx.mock
async def test_named_rooms_all_excluded_warns_once_and_can_warn_again(
    rigs, tmp_path, caplog, monkeypatch
) -> None:
    """A schedule with an explicit (non-'*') room list that SABLE_ALLOWED_ROOMS has
    entirely excluded must not just silently never fire again forever: it should
    warn once, the same debounce pattern as the '*' case already has, and be able
    to warn again later if it empties out a second time after recovering. Config is
    frozen, so the allow-list itself cannot be mutated mid-test - room_allowed is
    patched on the class instead, standing in for "the operator changed it"."""
    write_plugin(tmp_path, "p", rooms=[ROOM, OTHER], settings=schedules_only(every_handler()))
    rig = await rigs(tmp_path, allowed_rooms=[THIRD])  # neither ROOM nor OTHER is allowed
    route = message_route()
    with caplog.at_level(logging.WARNING):
        rig.clock.advance(60)
        await rig.manager.scheduler_tick()
        rig.clock.advance(60)
        await rig.manager.scheduler_tick()
        await rig.manager.wait_for_schedules()
    assert route.call_count == 0
    warnings = [m for m in caplog.messages if "all excluded by" in m]
    assert len(warnings) == 1  # not once per tick

    # Widen the allow-list so it resolves, confirming it can recover...
    caplog.clear()
    monkeypatch.setattr(Config, "room_allowed", lambda self, token: True)
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert route.call_count > 0

    # ...and then warn again if it empties out a second time.
    caplog.clear()
    monkeypatch.setattr(Config, "room_allowed", lambda self, token: token == THIRD)
    with caplog.at_level(logging.WARNING):
        rig.clock.advance(60)
        await rig.manager.scheduler_tick()
        await rig.manager.wait_for_schedules()
    warnings = [m for m in caplog.messages if "all excluded by" in m]
    assert len(warnings) == 1  # once again, for this second occurrence


@respx.mock
async def test_star_fan_out_also_respects_allowed_rooms(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", rooms=["*"], settings=schedules_only(every_handler()))
    rig = await rigs(tmp_path, allowed_rooms=[ROOM])
    rig.manager.known_rooms = lambda: [ROOM, OTHER, THIRD]
    here = message_route()
    there = message_route(OTHER)
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert here.call_count == 1
    assert there.call_count == 0


# --------------------------------------------------------------------------- #
# Per-room independence, and failures are for the log
# --------------------------------------------------------------------------- #


@respx.mock
async def test_one_rooms_failure_does_not_affect_another(rigs, tmp_path, caplog) -> None:
    write_plugin(
        tmp_path,
        "p",
        rooms=[ROOM, OTHER],
        settings=schedules_only(
            every_handler(), behaviours={"job": "fail-for-one"}, fail_room=OTHER
        ),
    )
    rig = await rigs(tmp_path)
    here = message_route()
    there = message_route(OTHER)
    rig.clock.advance(60)
    with caplog.at_level(logging.INFO):
        await rig.manager.scheduler_tick()
        await rig.manager.wait_for_schedules()
    assert said(here) == ["ok in abcd1234"]
    assert there.call_count == 0
    assert "schedule job said: nope" in caplog.text


@pytest.mark.parametrize("behaviour", ["crash", "exception", "error"])
@respx.mock
async def test_a_failing_schedule_handler_is_never_posted(
    rigs, tmp_path, caplog, behaviour
) -> None:
    write_plugin(
        tmp_path, "p", settings=schedules_only(every_handler(), behaviours={"job": behaviour})
    )
    rig = await rigs(tmp_path)
    route = message_route()
    rig.clock.advance(60)
    with caplog.at_level(logging.INFO):
        await rig.manager.scheduler_tick()
        await rig.manager.wait_for_schedules()
    assert route.call_count == 0
    if behaviour == "error":
        assert "schedule job said: no thanks" in caplog.text
    else:
        assert "schedule job failed" in caplog.text or "schedule job crashed" in caplog.text


@respx.mock
async def test_a_schedule_that_times_out_is_not_posted(rigs, tmp_path, caplog) -> None:
    write_plugin(
        tmp_path, "p", settings=schedules_only(every_handler(), behaviours={"job": "hang"})
    )
    rig = await rigs(tmp_path, call_timeout=0.3)
    route = message_route()
    rig.clock.advance(60)
    with caplog.at_level(logging.WARNING):
        await rig.manager.scheduler_tick()
        await rig.manager.wait_for_schedules()
    assert route.call_count == 0
    assert "took too long" in caplog.text


# --------------------------------------------------------------------------- #
# reply / send / react for a schedule
# --------------------------------------------------------------------------- #


@respx.mock
async def test_reply_and_send_are_equivalent_for_a_schedule(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path, "p", settings=schedules_only(every_handler(), behaviours={"job": "reply-action"})
    )
    rig = await rigs(tmp_path)
    route = message_route()
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    # The act itself posts first; the handler's return value is a second, separate
    # message (reporting the act's own outcome, for this test's benefit).
    assert sent(route)[0]["message"] == "replied in abcd1234"
    assert said(route)[1] == json.dumps({"op": "act_result", "id": "a1", "ok": True})


@respx.mock
async def test_an_explicit_send_works_the_same_as_reply(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path, "p", settings=schedules_only(every_handler(), behaviours={"job": "send"})
    )
    rig = await rigs(tmp_path)
    route = message_route()
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert sent(route)[0]["message"] == "sent to abcd1234"


@respx.mock
async def test_a_schedule_may_not_send_to_a_room_that_is_not_its_own(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "p",
        rooms=[ROOM],
        settings=schedules_only(
            every_handler(), behaviours={"job": "send-foreign"}, foreign_room=OTHER
        ),
    )
    rig = await rigs(tmp_path)
    here = message_route()
    there = message_route(OTHER)
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    assert there.call_count == 0
    answer = json.loads(said(here)[0])
    assert answer["ok"] is False
    assert "may not post" in answer["error"]


@respx.mock
async def test_react_always_fails_for_a_schedule(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path, "p", settings=schedules_only(every_handler(), behaviours={"job": "react"})
    )
    rig = await rigs(tmp_path)
    route = message_route()
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    answer = json.loads(said(route)[0])
    assert answer["ok"] is False
    assert "triggering message" in answer["error"]


# --------------------------------------------------------------------------- #
# Concurrency cap interaction
# --------------------------------------------------------------------------- #


@respx.mock
async def test_fan_out_respects_the_four_in_flight_cap(rigs, tmp_path) -> None:
    rooms = ["abcd1234", "efgh5678", "zzzz9999", "qqqq1111", "wwww2222"]
    write_plugin(
        tmp_path,
        "p",
        rooms=rooms,
        settings=schedules_only(every_handler(), behaviours={"job": "sleep"}, sleep_seconds=0.25),
    )
    rig = await rigs(tmp_path)
    routes = {room: message_route(room) for room in rooms}
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    await rig.manager.wait_for_schedules()
    texts_seen = [
        json.loads(call.request.content)["message"]
        for route in routes.values()
        for call in route.calls
    ]
    assert len(texts_seen) == 5
    maxes = {int(t.rsplit(" ", 1)[1]) for t in texts_seen}
    assert max(maxes) == 4


@respx.mock
async def test_the_backlog_does_not_grow_without_bound_when_draining_is_slower_than_due(
    rigs, tmp_path
) -> None:
    """If a handler's time-per-room times its room count exceeds its own interval,
    firing again before the previous cohort has drained would pile tasks on top of
    it forever (this is possible even at the documented limits: 256 rooms, a
    60s minimum interval and a 4-in-flight cap already cannot drain a handler
    averaging ~1s/room within its own minimum legal interval). Each occurrence
    while the previous cohort is still running must be SKIPPED, not queued, so the
    tracked backlog never exceeds one cohort's worth of tasks no matter how many
    times the schedule comes due before draining."""
    rooms = [f"room{n:04d}" for n in range(8)]
    write_plugin(
        tmp_path,
        "p",
        rooms=rooms,
        settings=schedules_only(every_handler(), behaviours={"job": "sleep"}, sleep_seconds=0.3),
    )
    rig = await rigs(tmp_path)
    for room in rooms:
        message_route(room)
    backlog_sizes = []
    for _ in range(10):
        # "Due" again every tick, far faster than 8 rooms at 4-in-flight and 0.3s
        # each (two waves, ~0.6s) can possibly drain.
        rig.clock.advance(60)
        await rig.manager.scheduler_tick()
        backlog_sizes.append(len(rig.manager._fire_tasks))
        await asyncio.sleep(0)  # let the loop breathe without waiting out the cohort
    await rig.manager.wait_for_schedules()
    assert max(backlog_sizes) <= len(rooms)


# --------------------------------------------------------------------------- #
# !plugins / --check
# --------------------------------------------------------------------------- #


@respx.mock
async def test_plugins_lists_schedules_in_human_readable_form(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "p",
        settings=schedules_only(cron_handler("standup", "0 8 * * 1-5"), every_handler("poll", 600)),
    )
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins", actor_id="users/maser"))
    await rig.bot.handle(event("!plugins p", actor_id="users/maser", message_id=101))
    listing, detail = said(route)
    assert "schedules: `standup` (cron 0 8 * * 1-5), `poll` (every 10 minutes)" in listing
    assert "Schedule `standup`: cron 0 8 * * 1-5" in detail
    assert "Schedule `poll`: every 10 minutes" in detail
    assert "- next:" in detail  # cheap to compute, so shown


@respx.mock
async def test_plugins_detail_shows_the_next_cron_fire_times(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(cron_handler("job", "0 0 * * *")))
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins p", actor_id="users/maser"))
    detail = said(route)[0]
    base = rig.clock.wall()
    first = (base + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    second = (base + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    assert first in detail
    assert second in detail


@respx.mock
async def test_plugins_detail_shows_the_next_every_fire_times(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(every_handler("job", 600)))
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins p", actor_id="users/maser"))
    detail = said(route)[0]
    base = rig.clock.wall()
    first = (base + timedelta(seconds=600)).strftime("%Y-%m-%d %H:%M")
    assert first in detail


@respx.mock
async def test_plugins_detail_finds_a_far_future_cron_beyond_the_old_45_day_horizon(
    rigs, tmp_path
) -> None:
    """A perfectly ordinary low-frequency cron (here, June 1st - about 150 days
    from the fake clock's Jan 1st start) must still show a next fire time: the
    display horizon is wider than CronSpec.next_after's own 45-day default, or an
    ordinary yearly/quarterly schedule looks identical to a broken one."""
    write_plugin(tmp_path, "p", settings=schedules_only(cron_handler(expr="0 0 1 6 *")))
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins p", actor_id="users/maser"))
    detail = said(route)[0]
    assert "- next:" in detail
    assert "2024-06-01" in detail


@respx.mock
async def test_plugins_detail_says_so_when_a_cron_never_matches_anything(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(cron_handler(expr="0 8 31 2 *")))
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins p", actor_id="users/maser"))
    detail = said(route)[0]
    assert "no fire found in the next year" in detail
    assert "- next:" not in detail


@respx.mock
async def test_plugins_detail_names_a_schedule_dropped_by_the_combined_cap(
    rigs, tmp_path, monkeypatch
) -> None:
    """A schedule the global MAX_TOTAL_SCHEDULES cap dropped must say so plainly,
    rather than showing no 'next' line - which otherwise looks identical to an
    ordinary schedule whose next fire is just far away (see the far-future test
    above), with nothing to tell the two apart."""
    monkeypatch.setattr("sable.plugins.MAX_TOTAL_SCHEDULES", 1)
    write_plugin(
        tmp_path, "p", settings=schedules_only(every_handler("first"), every_handler("second"))
    )
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins p", actor_id="users/maser"))
    detail = said(route)[0]
    assert "Schedule `first`:" in detail
    assert "Schedule `second`: every 1 minute - past the combined schedule limit, not running" in (
        detail
    )


@posix_only
async def test_check_reports_schedules(tmp_path, monkeypatch) -> None:
    from sable.plugins import check_plugins

    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)
    source = textwrap.dedent(
        """
        from sable.plugin_api import schedule

        @schedule(cron="0 8 * * *")
        async def job(ctx):
            return "hi"
        """
    )
    write_plugin(tmp_path, "p", source=source)
    lines, refused = await check_plugins(make_config(plugins_dir=str(tmp_path)), Registry())
    assert not refused
    assert any("schedules: job (cron 0 8 * * *)" in line for line in lines)


# --------------------------------------------------------------------------- #
# The MAX_TOTAL_SCHEDULES cap
# --------------------------------------------------------------------------- #


async def test_the_total_schedule_cap_is_enforced(rigs, tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr("sable.plugins.MAX_TOTAL_SCHEDULES", 3)
    handlers = [every_handler(f"h{n}", 60) for n in range(5)]
    write_plugin(tmp_path, "p", settings=schedules_only(*handlers))
    with caplog.at_level(logging.WARNING):
        rig = await rigs(tmp_path)
    assert len(rig.manager._schedules) == 3
    assert rig.record("p").status is Status.ACTIVE  # the plugin itself still loads
    assert any("past the combined limit of 3" in m for m in caplog.messages)


def test_the_documented_cap_is_what_the_constant_says() -> None:
    assert MAX_TOTAL_SCHEDULES == 256


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #


@respx.mock
async def test_aclose_stops_the_scheduler_and_waits_for_in_flight_fires(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path, "p", settings=schedules_only(every_handler(), behaviours={"job": "sleep"})
    )
    rig = await rigs(tmp_path)
    message_route()
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    assert rig.manager._fire_tasks  # still running (0.2s default sleep)
    await rig.manager.aclose()
    assert not rig.manager._fire_tasks
    assert rig.manager._scheduler_task is None


@respx.mock
async def test_aclose_does_not_wait_past_shutdown_grace_for_a_hung_schedule(rigs, tmp_path) -> None:
    """A hung scheduled call must not delay shutdown by anywhere near its own call
    timeout (which can be minutes, per SABLE_PLUGINS_TIMEOUT): aclose() is bounded
    by SHUTDOWN_GRACE, the same grace a worker itself gets to exit - not by
    whatever a schedule's own handler happens to be sitting inside."""
    write_plugin(
        tmp_path, "p", settings=schedules_only(every_handler(), behaviours={"job": "hang"})
    )
    # A call timeout far longer than SHUTDOWN_GRACE (2.0s): if aclose() were still
    # bounded by it instead, this test would take ~30s, not a couple of seconds.
    rig = await rigs(tmp_path, call_timeout=30.0)
    message_route()
    rig.clock.advance(60)
    await rig.manager.scheduler_tick()
    assert rig.manager._fire_tasks  # the hung call is in flight
    started = time.monotonic()
    await rig.manager.aclose()
    elapsed = time.monotonic() - started
    assert elapsed < 10.0  # bounded by SHUTDOWN_GRACE (x2: schedules, then workers)


async def test_start_scheduler_is_a_noop_with_no_schedules(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")  # a plain command plugin, no schedules
    rig = await rigs(tmp_path)
    rig.manager.start_scheduler()
    assert rig.manager._scheduler_task is None


@respx.mock
async def test_start_scheduler_runs_in_the_background(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings=schedules_only(every_handler(seconds=60)))
    rig = await rigs(tmp_path)
    route = message_route()
    rig.manager.start_scheduler()
    try:
        assert rig.manager._scheduler_task is not None
        rig.clock.advance(60)
        # The real loop sleeps SCHEDULER_TICK_SECONDS between ticks; give it a
        # moment to notice without a real multi-second sleep in the test.
        for _ in range(50):
            await asyncio.sleep(0.05)
            if route.call_count:
                break
    finally:
        await rig.manager.aclose()
    assert route.call_count == 1


# --------------------------------------------------------------------------- #
# The real worker, end to end
# --------------------------------------------------------------------------- #

GREETER = textwrap.dedent(
    """
    from sable.plugin_api import PluginError, schedule


    @schedule(cron="0 8 * * *")
    async def standup(ctx):
        return f"standup in {ctx.room} (trigger {ctx.trigger}, admin {ctx.is_admin})"


    @schedule(every="60s")
    async def poll(ctx):
        await ctx.send(ctx.room, "polled")


    @schedule(every="60s")
    async def broken(ctx):
        raise PluginError("not today")


    @schedule(every="60s")
    async def crashes(ctx):
        raise RuntimeError("kaboom")
    """
)


@posix_only
@respx.mock
async def test_a_real_schedule_plugin_fires_through_the_real_worker(rigs, tmp_path, caplog) -> None:
    write_plugin(tmp_path, "greeter", source=GREETER, rooms=[ROOM, OTHER])
    rig = await rigs(tmp_path, command=default_command, admin_users=["maser"])
    assert rig.record("greeter").status is Status.ACTIVE, rig.record("greeter").reason
    here = message_route()
    there = message_route(OTHER)
    with caplog.at_level(logging.INFO):
        await rig.manager.scheduler_tick(rig.clock.wall() + timedelta(days=1, hours=8))
        rig.clock.advance(60)
        await rig.manager.scheduler_tick()
        await rig.manager.wait_for_schedules()
    assert sorted(said(here)) == [
        "polled",
        "standup in abcd1234 (trigger schedule, admin False)",
    ]
    assert sorted(said(there)) == [
        "polled",
        "standup in efgh5678 (trigger schedule, admin False)",
    ]
    assert "schedule broken said: not today" in caplog.text
    assert "kaboom" in caplog.text  # in the log, named, never in a room
    assert "not today" not in " ".join(said(here) + said(there))
    assert "kaboom" not in " ".join(said(here) + said(there))


@posix_only
async def test_a_real_schedule_with_a_bad_declaration_fails_with_a_readable_reason(
    rigs, tmp_path
) -> None:
    source = textwrap.dedent(
        """
        from sable.plugin_api import schedule

        @schedule(cron="not a cron")
        async def job(ctx):
            return "hi"
        """
    )
    write_plugin(tmp_path, "greeter", source=source)
    rig = await rigs(tmp_path, command=default_command)
    assert rig.record("greeter").status is Status.FAILED
    assert "space-separated" in rig.record("greeter").reason
