"""The worker transport: what the core does when a worker hangs, crashes or lies.

Driven by tests/fake_worker.py, a scriptable stand-in for the real host, so that
every kind of bad behaviour can be produced on demand.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import resource
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import respx

from conftest import ROOM, TALK, event
from plugin_helpers import (
    message_route,
    posix_only,
    reaction_route,
    sent,
    texts,
    write_plugin,
)
from sable.plugins import (
    CallOutcome,
    PluginFailure,
    Status,
    _exit_description,
    bootstrap_source,
    disable_process_inspection,
)

pytestmark = posix_only

OTHER = "efgh5678"
SRC = str(Path(__file__).resolve().parent.parent / "src")
TESTS = str(Path(__file__).resolve().parent)


def payload(args: str) -> dict[str, Any]:
    return {
        "plugin": "p",
        "trigger": "command",
        "name": "hi",
        "args": args,
        "argv": args.split(),
        "room": ROOM,
        "actor_id": "users/alice",
        "actor_name": "Alice",
        "is_admin": False,
        "message_id": 100,
        "text": f"!hi {args}",
        "match": "",
    }


async def quiet_sink(action: str, args: dict[str, Any]) -> str | None:
    return None


async def raw_call(rig: Any, args: str, name: str = "p") -> CallOutcome:
    """Call the plugin's worker directly, with a sink that accepts everything."""
    worker = rig.record(name).worker
    return await worker.call("command:hi", payload(args), quiet_sink)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def gone(pid: int, seconds: float = 5.0) -> bool:
    """Wait for a process to be gone. Short polls: a killed child is reaped a moment later."""
    for _ in range(int(seconds / 0.02)):
        if not alive(pid):
            return True
        await asyncio.sleep(0.02)
    return False


async def eventually(check, seconds: float = 3.0) -> bool:
    for _ in range(int(seconds / 0.02)):
        if check():
            return True
        await asyncio.sleep(0.02)
    return False


# --------------------------------------------------------------------------- #
# A call that takes too long
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_call_past_the_timeout_kills_the_worker_and_the_next_call_restarts_it(
    rigs, tmp_path
) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=0.4)
    pid = int((await raw_call(rig, "pid")).reply or 0)
    route = message_route()

    await rig.bot.handle(event("!hi hang"))
    assert texts(route) == ["⚠️ Sorry - the `p` plugin took too long"]
    assert await gone(pid)
    assert rig.record("p").state == "restarting"
    assert rig.record("p").status is Status.ACTIVE

    await rig.bot.handle(event("!hi echo back again", message_id=101))
    assert texts(route)[1] == "back again"
    assert rig.record("p").state == "active"
    assert int((await raw_call(rig, "pid")).reply or 0) != pid
    assert rig.record("p").worker.restart_count == 1
    assert "took longer than 0.4" in rig.record("p").last_error


@respx.mock
async def test_the_whole_process_group_dies_with_a_timed_out_worker(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=0.4)
    child = int((await raw_call(rig, "child")).reply or 0)
    assert alive(child)
    with pytest.raises(PluginFailure, match="took too long"):
        await raw_call(rig, "hang")
    assert await gone(child)


async def test_a_worker_is_its_own_session_and_process_group(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    info = json.loads((await raw_call(rig, "session")).reply or "{}")
    assert info["sid"] == info["pid"] == info["pgid"]
    assert info["pgid"] != os.getpgrp()


async def test_a_worker_runs_in_its_plugins_directory(rigs, tmp_path) -> None:
    folder = write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    assert (await raw_call(rig, "cwd")).reply == str(folder.resolve())


@respx.mock
async def test_a_worker_that_crashes_mid_call_is_reported_and_restarted(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi crash"))
    assert texts(route) == ["⚠️ Sorry - the `p` plugin crashed"]
    await rig.bot.handle(event("!hi echo alive", message_id=101))
    assert texts(route)[1] == "alive"


async def test_a_worker_killed_while_idle_is_restarted_by_the_next_call(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    pid = int((await raw_call(rig, "pid")).reply or 0)
    os.kill(pid, signal.SIGKILL)
    assert await gone(pid)
    assert await eventually(lambda: rig.record("p").state == "restarting")
    assert (await raw_call(rig, "echo hello")).reply == "hello"


# --------------------------------------------------------------------------- #
# The circuit breaker
# --------------------------------------------------------------------------- #


async def hang(rig: Any) -> str:
    try:
        await raw_call(rig, "hang")
    except PluginFailure as exc:
        return str(exc)
    return "did not fail"


async def test_old_restarts_are_forgiven_after_five_minutes(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=0.2)
    for _ in range(3):
        assert "took too long" in await hang(rig)
    rig.clock.advance(301)
    for _ in range(2):
        assert "took too long" in await hang(rig)
    # Five deaths, but only two restarts inside the window: still allowed one more.
    assert (await raw_call(rig, "echo fine")).reply == "fine"
    assert rig.record("p").status is Status.ACTIVE


# --------------------------------------------------------------------------- #
# A worker that breaks the protocol
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("garbage", "not JSON"),
        ("notobject", "not a JSON object"),
        ("badop", "unknown op"),
        ("oversize", "longer than"),
        ("unknownid", "request nobody made"),
    ],
)
@respx.mock
async def test_a_protocol_violation_kills_the_worker_and_counts_as_a_crash(
    rigs, tmp_path, caplog, command, why
) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    pid = int((await raw_call(rig, "pid")).reply or 0)
    route = message_route()
    with caplog.at_level(logging.WARNING):
        await rig.bot.handle(event(f"!hi {command}"))
    assert texts(route) == ["⚠️ Sorry - the `p` plugin crashed"]
    assert "protocol violation" in rig.record("p").last_error
    assert why in rig.record("p").last_error
    assert "protocol violation" in caplog.text
    assert await gone(pid)
    # It comes back for the next call, on the restart budget.
    assert (await raw_call(rig, "echo again")).reply == "again"
    assert rig.record("p").worker.restart_count == 1


@respx.mock
async def test_a_flood_of_actions_is_cut_off(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi flood 1000"))
    assert len(texts(route)[:-1]) <= 10
    assert "the `p` plugin crashed" in texts(route)[-1]
    assert "flood" in rig.record("p").last_error


async def test_a_worker_that_floods_stderr_without_newlines_is_not_a_problem(
    rigs, tmp_path, caplog
) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    with caplog.at_level(logging.INFO, logger="sable.plugins"):
        assert (await raw_call(rig, "stderrblob")).reply == "wrote"
        await rig.manager.aclose()
    lines = [m for m in caplog.messages if m.startswith("plugin p: B")]
    assert lines
    assert all(len(m) <= len("plugin p: ") + 1000 for m in lines)


async def test_stderr_is_logged_per_line_capped_and_cleaned(rigs, tmp_path, caplog) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    with caplog.at_level(logging.INFO, logger="sable.plugins"):
        await raw_call(rig, "stderr 3")
        await rig.manager.aclose()
    assert any(m.startswith("plugin p: line 0 ") for m in caplog.messages)
    assert not any("\x1b" in m for m in caplog.messages)
    long = [m for m in caplog.messages if m.startswith("plugin p: LLLL")]
    assert len(long) == 1
    assert len(long[0]) == len("plugin p: ") + 1000


async def test_stderr_is_rate_limited(rigs, tmp_path, caplog) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    with caplog.at_level(logging.INFO, logger="sable.plugins"):
        await raw_call(rig, "stderr 1000")
        await rig.manager.aclose()
    ours = [m for m in caplog.messages if m.startswith("plugin p:")]
    assert len(ours) < 260
    assert sum("too much output" in m for m in ours) == 1


# --------------------------------------------------------------------------- #
# What a plugin may do to the chat
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_call_may_make_ten_actions_and_the_last_reply_counts_as_one(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi acts 9"))
    assert len(texts(route)) == 10
    assert texts(route)[-1] == "9 of 9 accepted"


@respx.mock
async def test_actions_past_the_tenth_are_refused(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi acts 15"))
    assert texts(route) == [f"part {i}" for i in range(10)]
    # The eleventh action, the final reply, is refused as well.
    assert rig.record("p").state == "active"


@respx.mock
async def test_a_call_may_post_twenty_thousand_characters(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi bigacts"))
    # 9000 + 9000, then what is left of the budget, then nothing.
    assert [len(t) for t in texts(route)] == [9000, 9000, 2000]


@respx.mock
async def test_a_huge_return_value_is_cut_to_the_budget(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi huge 100000"))
    assert len(texts(route)[0]) == 20_000


@respx.mock
async def test_talks_own_ceiling_still_applies_to_a_plugins_reply(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, max_message_chars=500)
    route = message_route()
    await rig.bot.handle(event("!hi huge 5000"))
    assert len(texts(route)[0]) == 500
    assert texts(route)[0].endswith("_[truncated]_")


@respx.mock
async def test_mass_mentions_in_a_plugins_text_are_defanged(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi mention"))
    body = texts(route)[0]
    assert "@all" not in body
    assert "@​all" in body


@respx.mock
async def test_a_plugin_may_not_send_to_a_room_that_is_not_its_own(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", rooms=[ROOM])
    rig = await rigs(tmp_path)
    here = message_route()
    other = message_route(OTHER)
    await rig.bot.handle(event(f"!hi send {OTHER} sneaky"))
    assert other.call_count == 0
    answer = json.loads(texts(here)[0])
    assert answer["ok"] is False
    assert "may not post" in answer["error"]


@respx.mock
@pytest.mark.parametrize("target", ["NOT-A-TOKEN", "abc", "../abcd1234", "abcd1234/../x"])
async def test_a_plugin_may_not_send_to_something_that_is_not_a_token(
    rigs, tmp_path, target
) -> None:
    write_plugin(tmp_path, "p", rooms=["*"])
    rig = await rigs(tmp_path)
    here = message_route()
    await rig.bot.handle(event(f"!hi send {target} x"))
    assert json.loads(texts(here)[0])["ok"] is False


@respx.mock
async def test_a_plugin_may_send_to_another_of_its_rooms(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", rooms=[ROOM, OTHER])
    rig = await rigs(tmp_path)
    here = message_route()
    other = message_route(OTHER)
    await rig.bot.handle(event(f"!hi send {OTHER} @all hello"))
    assert [b["message"] for b in sent(other)] == ["@​all hello"]
    assert json.loads(texts(here)[0]) == {"op": "act_result", "id": "a1", "ok": True}


@respx.mock
async def test_a_star_plugin_may_send_anywhere_sable_follows(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", rooms=["*"])
    rig = await rigs(tmp_path)
    message_route()
    other = message_route(OTHER)
    await rig.bot.handle(event(f"!hi send {OTHER} hi"))
    assert other.call_count == 1


@respx.mock
async def test_send_respects_allowed_rooms_even_for_a_listed_room(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", rooms=[ROOM, OTHER])
    rig = await rigs(tmp_path, allowed_rooms=[ROOM])
    here = message_route()
    other = message_route(OTHER)
    await rig.bot.handle(event(f"!hi send {OTHER} hi"))
    assert other.call_count == 0
    assert json.loads(texts(here)[0])["ok"] is False


@respx.mock
async def test_a_plugin_may_react_to_the_message_that_triggered_it(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    message_route()
    reaction = reaction_route(message_id=100)
    await rig.bot.handle(event("!hi react 👍"))
    assert [json.loads(c.request.content) for c in reaction.calls] == [{"reaction": "👍"}]


@respx.mock
async def test_a_reaction_that_is_not_an_emoji_is_refused(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    here = message_route()
    reaction = reaction_route()
    await rig.bot.handle(event("!hi react " + "x" * 50))
    assert reaction.call_count == 0
    assert json.loads(texts(here)[0])["ok"] is False


@respx.mock
async def test_an_unknown_action_is_refused_not_obeyed(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    here = message_route()
    await rig.bot.handle(event("!hi badaction"))
    answer = json.loads(texts(here)[0])
    assert answer["ok"] is False
    assert "unknown action" in answer["error"]


@respx.mock
async def test_an_empty_reply_is_ignored_without_complaint(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    here = message_route()
    await rig.bot.handle(event("!hi emptyreply"))
    assert len(texts(here)) == 1  # only the final reply, which reports that the act was fine
    assert json.loads(texts(here)[0])["ok"] is True


@respx.mock
async def test_the_silent_flag_reaches_talk(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    here = message_route()
    await rig.bot.handle(event("!hi silent"))
    assert sent(here) == [{"message": "shh", "silent": True}]


@respx.mock
async def test_a_failed_post_is_the_plugins_problem_not_a_crash(rigs, tmp_path) -> None:
    import httpx

    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    route = respx.post(f"{TALK}/chat/{ROOM}").mock(return_value=httpx.Response(500, text="nope"))
    await rig.bot.handle(event("!hi acts 2"))
    assert route.call_count >= 2
    assert rig.record("p").state == "active"
    assert (await raw_call(rig, "echo fine")).reply == "fine"


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


async def test_at_most_four_calls_are_in_flight_per_plugin(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    outcomes = await asyncio.gather(*(raw_call(rig, "sleep 0.25") for _ in range(8)))
    seen = [int((o.reply or "").rsplit(" ", 1)[1]) for o in outcomes]
    assert max(seen) == 4


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


async def test_no_secret_of_the_parent_reaches_a_worker(rigs, tmp_path, monkeypatch) -> None:
    secret = "TOPSECRET-app-password"
    monkeypatch.setenv("SABLE_NEXTCLOUD_PASSWORD", secret)
    monkeypatch.setenv("SABLE_LLM_API_KEY", secret)
    monkeypatch.setenv("SABLE_NOTIFY_TOKEN", secret)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secret)
    monkeypatch.setenv("PYTHONPATH", "/evil")
    monkeypatch.setenv("LD_PRELOAD", "/evil.so")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, timezone="Europe/Berlin")
    env = json.loads((await raw_call(rig, "env")).reply or "{}")
    assert secret not in json.dumps(env)
    assert not [key for key in env if key.startswith("SABLE_")]
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert env["TZ"] == "Europe/Berlin"
    assert env["LANG"] == "C.UTF-8"
    assert set(env) <= {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "TZ"}


async def test_the_certificate_locations_do_reach_a_worker(rigs, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    monkeypatch.setenv("SSL_CERT_DIR", "/etc/ssl/certs")
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    env = json.loads((await raw_call(rig, "env")).reply or "{}")
    assert env["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"
    assert env["SSL_CERT_DIR"] == "/etc/ssl/certs"


async def test_the_settings_reach_the_worker_as_written(rigs, tmp_path) -> None:
    settings = {
        "city": "Berlin",
        "days": [1, 2, 3],
        "nested": {"on": True, "ratio": 0.5, "x": None},
    }
    write_plugin(tmp_path, "p", settings=settings)
    rig = await rigs(tmp_path)
    assert json.loads((await raw_call(rig, "settings")).reply or "{}") == settings


def test_the_core_makes_itself_undumpable_and_says_so() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("prctl is Linux only")
    libc = ctypes.CDLL(None)
    try:
        assert disable_process_inspection() == ""
        assert libc.prctl(3, 0, 0, 0, 0) == 0  # PR_GET_DUMPABLE
    finally:
        libc.prctl(4, 1, 0, 0, 0)  # leave the test process as it was found


DRIVER = textwrap.dedent(
    """
    import asyncio, ctypes, json, sys
    sys.path.insert(0, {src!r})
    sys.path.insert(0, {tests!r})
    from sable.commands import Registry
    from sable.config import Config
    from sable.plugins import PluginManager
    from plugin_helpers import fake_command

    async def quiet(action, args):
        return None

    async def main(root, mode):
        config = Config(
            nextcloud_url="https://cloud.example.org",
            nextcloud_user="sable",
            nextcloud_password="x",
            plugins_dir=root,
        )
        manager = PluginManager(config, timeout=10, command=fake_command)
        await manager.load_all(Registry())
        if mode == "control":
            # Undo the protection, to show the worker could read this process without it.
            ctypes.CDLL(None).prctl(4, 1, 0, 0, 0)
        ctx = dict(plugin="p", trigger="command", name="hi", args="proc", argv=["proc"],
                   room="abcd1234", actor_id="users/a", actor_name="A", is_admin=False,
                   message_id=1, text="", match="")
        outcome = await manager.records[0].worker.call("command:hi", ctx, quiet)
        print(outcome.reply)
        await manager.aclose()

    asyncio.run(main(sys.argv[1], sys.argv[2]))
    """
)


def run_core(tmp_path: Path, root: Path, mode: str) -> dict[str, Any]:
    """Run a separate core process that holds a secret in its own environment.

    /proc/<pid>/environ shows the environment a process was started with, so the
    secret has to be in the environment of a process that is not this one.
    """
    script = tmp_path / f"driver_{mode}.py"
    script.write_text(DRIVER.format(src=SRC, tests=TESTS), encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - our own interpreter and a script we just wrote
        [sys.executable, str(script), str(root), mode],
        env={"SABLE_NEXTCLOUD_PASSWORD": "TOPSECRET-app-password", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_worker_cannot_read_its_parents_environment_or_memory(tmp_path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root holds CAP_SYS_PTRACE, which no process flag can take away")
    root = tmp_path / "plugins"
    write_plugin(root, "p")

    control = run_core(tmp_path, root, "control")
    if control["environ"].get("secret") is not True:
        pytest.skip(
            "this platform denies reading a parent's /proc even with the protection off "
            f"(ptrace restrictions): nothing for the protection to prove ({control})"
        )

    protected = run_core(tmp_path, root, "protected")
    assert "denied" in protected["environ"], protected
    assert protected["environ"]["denied"] == "PermissionError"

    # Memory is a separate door: it is only proved if the control could walk through it.
    if "read" not in control["mem"]:
        pytest.skip(
            "the environment half is proved; memory could not be read even without the "
            f"protection (kernel or Yama policy), so there is nothing to compare: {control}"
        )
    assert "read" not in protected["mem"], protected
    assert protected["mem"].get("denied") == "PermissionError", protected


def test_the_bootstrap_applies_the_documented_resource_limits(tmp_path) -> None:
    (tmp_path / "limits_host.py").write_text(
        textwrap.dedent(
            """
            import json, resource

            def main():
                names = ("RLIMIT_AS", "RLIMIT_NOFILE", "RLIMIT_CORE", "RLIMIT_CPU")
                print(json.dumps({n: resource.getrlimit(getattr(resource, n)) for n in names}))
            """
        )
    )
    done = subprocess.run(  # noqa: S603 - our own interpreter and a script we just wrote
        [sys.executable, "-I", "-c", bootstrap_source(str(tmp_path), 12, "limits_host")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    limits = json.loads(done.stdout)
    assert limits["RLIMIT_AS"] == [1 << 30, 1 << 30]
    assert limits["RLIMIT_NOFILE"] == [64, 64]
    assert limits["RLIMIT_CORE"] == [0, 0]
    # A per-call soft budget; the hard limit stays unlimited so that it can be re-armed.
    assert limits["RLIMIT_CPU"] == [12, resource.RLIM_INFINITY]


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #


async def test_closing_the_manager_ends_every_worker_and_its_children(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "one")
    write_plugin(tmp_path, "two", settings={"declare": {"commands": [{"name": "other"}]}})
    rig = await rigs(tmp_path)
    pids = [int((await raw_call(rig, "pid", name)).reply or 0) for name in ("one", "two")]
    child = int((await raw_call(rig, "child", "one")).reply or 0)
    await rig.manager.aclose()
    for pid in [*pids, child]:
        assert await gone(pid)
    await rig.manager.aclose()  # twice is fine


async def test_a_call_after_shutdown_fails_cleanly(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    await rig.manager.aclose()
    with pytest.raises(PluginFailure, match="shutting down"):
        await raw_call(rig, "echo x")


# --------------------------------------------------------------------------- #
# The circuit breaker, half open
# --------------------------------------------------------------------------- #


async def trip(rig: Any) -> None:
    """Four deaths in a row, then the call that finds the breaker open."""
    for _ in range(4):
        await hang_quickly(rig)
    with pytest.raises(PluginFailure, match="switched off"):
        await raw_call(rig, "echo x")


@respx.mock
async def test_three_restarts_in_five_minutes_then_the_plugin_is_switched_off(
    rigs, tmp_path
) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=0.2, admin_users=["maser"])
    for _ in range(4):  # the first start and three restarts
        assert "took too long" in await hang(rig)
    record = rig.record("p")
    assert record.state == "restarting"  # not yet: the fourth death has not been counted
    # The next call would be a fourth restart.
    with pytest.raises(PluginFailure, match="switched off"):
        await raw_call(rig, "echo x")
    assert record.effective_status is Status.FAILED
    assert record.state == "switched off, retrying in 5 min"
    assert "more than 3 times in 5 minutes" in record.last_error
    assert record.worker.tripped
    assert record.worker.blocked

    # While it is off its commands answer like commands that do not exist.
    route = message_route()
    await rig.bot.handle(event("!hi echo x"))
    assert texts(route) == ["I have no `hi` command. Try `!help`."]
    await rig.bot.handle(event("!plugins p", actor_id="users/maser", message_id=101))
    detail = texts(route)[1]
    assert "switched off, retrying in 5 min" in detail
    assert "Restarts since sable started: 3" in detail
    assert "Command `" not in detail  # nothing offered that does not work
    await rig.bot.handle(event("!plugins", actor_id="users/maser", message_id=102))
    assert "1 failed" in texts(route)[2]


async def test_the_minutes_left_count_down(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    await trip(rig)
    rig.clock.advance(120)
    assert rig.record("p").state == "switched off, retrying in 3 min"
    rig.clock.advance(179)
    assert rig.record("p").state == "switched off, retrying in 1 min"
    rig.clock.advance(2)
    assert rig.record("p").state == "switched off, retrying on the next use"


async def test_after_the_cooldown_one_trial_call_closes_the_breaker_for_good(
    rigs, tmp_path
) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    await trip(rig)
    rig.clock.advance(301)
    # The trial works.
    assert (await raw_call(rig, "echo trial")).reply == "trial"
    record = rig.record("p")
    assert record.state == "active"
    assert not record.worker.tripped
    # And the past is forgiven: three more restarts are allowed again.
    for _ in range(3):
        await hang_quickly(rig)
    assert (await raw_call(rig, "echo still fine")).reply == "still fine"


async def test_a_failed_trial_switches_the_plugin_off_for_another_cooldown(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    await trip(rig)
    rig.clock.advance(301)
    await hang_quickly(rig)  # the trial itself
    record = rig.record("p")
    assert record.worker.blocked
    assert record.state == "switched off, retrying in 5 min"
    with pytest.raises(PluginFailure, match="switched off"):
        await raw_call(rig, "echo x")
    rig.clock.advance(301)
    assert (await raw_call(rig, "echo x")).reply == "x"


async def test_only_one_trial_call_goes_through(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=3)
    for _ in range(4):
        await hang_quickly(rig)
    with pytest.raises(PluginFailure, match="switched off"):
        await raw_call(rig, "echo x")
    rig.clock.advance(301)
    results = await asyncio.gather(
        raw_call(rig, "sleep 0.3"), raw_call(rig, "echo second"), return_exceptions=True
    )
    ok = [r for r in results if isinstance(r, CallOutcome)]
    refused = [r for r in results if isinstance(r, PluginFailure)]
    assert len(ok) == 1
    assert len(refused) == 1
    assert "switched off" in str(refused[0])


async def hang_quickly(rig: Any) -> None:
    """A death that does not wait out the timeout."""
    with pytest.raises(PluginFailure, match="crashed"):
        await raw_call(rig, "crash")


@respx.mock
async def test_a_call_in_flight_when_another_times_out_is_told_the_worker_restarted(
    rigs, tmp_path
) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=0.5)
    first = asyncio.create_task(raw_call(rig, "hang"))
    await asyncio.sleep(0.2)  # so that its own deadline is the later one
    second = asyncio.create_task(raw_call(rig, "sleep 30"))
    results = await asyncio.gather(first, second, return_exceptions=True)
    messages = sorted(str(r) for r in results)
    assert messages == [
        "the `p` plugin took too long",
        "the `p` plugin's worker was restarted (another call took too long)",
    ]
    # One incident is one hit: the next call is the first restart, and only that.
    assert (await raw_call(rig, "echo again")).reply == "again"
    assert rig.record("p").worker.restart_count == 1
    assert len(rig.record("p").worker._restarts) == 1


async def test_a_restart_that_declares_something_else_fails_the_plugin_visibly(
    rigs, tmp_path
) -> None:
    flag = tmp_path / "flag"
    declared = {"commands": [{"name": "hi"}]}
    other = {"commands": [{"name": "hi"}, {"name": "extra"}]}
    write_plugin(
        tmp_path / "plugins",
        "p",
        settings={"flip": str(flag), "declare": declared, "declare_after": other},
    )
    rig = await rigs(tmp_path / "plugins")
    pid = int((await raw_call(rig, "pid")).reply or 0)
    os.kill(pid, signal.SIGKILL)
    assert await gone(pid)
    assert await eventually(lambda: rig.record("p").worker.needs_restart)
    with pytest.raises(PluginFailure):
        await raw_call(rig, "echo x")
    record = rig.record("p")
    assert record.status is Status.FAILED
    assert "different after a restart" in record.reason
    assert record.state.startswith("failed:")
    with pytest.raises(PluginFailure):
        await raw_call(rig, "echo x")


# --------------------------------------------------------------------------- #
# Telling death from silence
# --------------------------------------------------------------------------- #


async def test_a_worker_that_exits_is_noticed_at_once_even_if_a_child_holds_its_pipe(
    rigs, tmp_path
) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=20)
    started = time.monotonic()
    with pytest.raises(PluginFailure, match="crashed"):
        await raw_call(rig, "orphan 3")
    # The child holds the pipe for three seconds; the death is noticed in a fraction of that.
    assert time.monotonic() - started < 1.0
    assert "exited with status 5" in rig.record("p").last_error


async def test_a_worker_killed_by_a_signal_is_reported_by_the_signals_name(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    with pytest.raises(PluginFailure, match="crashed"):
        await raw_call(rig, "sigterm")
    assert "killed by SIGTERM" in rig.record("p").last_error
    assert await eventually(lambda: rig.record("p").state == "restarting")


# --------------------------------------------------------------------------- #
# Late messages are dropped, not punished
# --------------------------------------------------------------------------- #


@respx.mock
async def test_an_action_after_the_result_is_dropped_with_a_warning(rigs, tmp_path, caplog) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    route = message_route()
    with caplog.at_level(logging.WARNING, logger="sable.plugins"):
        await rig.bot.handle(event("!hi lateact"))
        assert await eventually(lambda: "dropped an action" in caplog.text)
    assert texts(route) == ["done"]
    assert "plugin p: dropped an action that arrived after its call had finished" in caplog.text
    # No kill, no breaker hit: the very same worker answers the next call.
    assert rig.record("p").state == "active"
    assert rig.record("p").worker.restart_count == 0
    assert (await raw_call(rig, "echo alive")).reply == "alive"
    assert rig.record("p").worker.restart_count == 0


async def test_a_second_result_for_a_finished_call_is_dropped(rigs, tmp_path, caplog) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    with caplog.at_level(logging.WARNING, logger="sable.plugins"):
        assert (await raw_call(rig, "dupresult")).reply == "first"
        assert await eventually(lambda: "dropped a result" in caplog.text)
    assert rig.record("p").worker.restart_count == 0
    assert (await raw_call(rig, "echo alive")).reply == "alive"


async def test_late_messages_in_a_flood_are_a_violation_after_all(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    worker = rig.record("p").worker
    session = worker._session
    for _ in range(200):
        worker._late(session, "an action")
    assert worker._late(session, "an action") is not None


async def test_an_action_for_a_call_nobody_made_is_still_a_violation(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    worker = rig.record("p").worker
    line = json.dumps(
        {"op": "act", "id": "a1", "call": 424242, "action": "reply", "args": {"text": "x"}}
    ).encode()
    assert "nobody made" in (worker._handle_line(worker._session, line) or "")


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #


async def test_shutdown_is_bounded_when_the_worker_has_stopped_reading(
    rigs, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("sable.plugins.SHUTDOWN_GRACE", 0.5)
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=60)
    pid = int((await raw_call(rig, "pid")).reply or 0)
    stopped = asyncio.create_task(raw_call(rig, "stop"))
    await asyncio.sleep(0.2)
    # Far more than a pipe holds: the writer blocks, holding the write lock.
    blocked = asyncio.create_task(raw_call(rig, "echo " + "x" * 3_000_000))
    await asyncio.sleep(0.2)
    started = time.monotonic()
    await rig.manager.aclose()
    assert time.monotonic() - started < 3.0
    assert await gone(pid)
    results = await asyncio.gather(stopped, blocked, return_exceptions=True)
    assert all(isinstance(r, PluginFailure) for r in results)
    assert all("shutting down" in str(r) for r in results)


async def test_calls_in_flight_at_shutdown_are_told_sable_is_shutting_down(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=60)
    running = asyncio.create_task(raw_call(rig, "hang"))
    await asyncio.sleep(0.2)
    await rig.manager.aclose()
    with pytest.raises(PluginFailure, match="sable is shutting down"):
        await running


async def test_shutdown_while_a_restart_is_spawning_leaves_nothing_running(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    pid = int((await raw_call(rig, "pid")).reply or 0)
    os.kill(pid, signal.SIGKILL)
    assert await gone(pid)
    assert await eventually(lambda: rig.record("p").worker.needs_restart)
    call = asyncio.create_task(raw_call(rig, "echo x"))
    await asyncio.sleep(0)  # let it reach the spawn
    await rig.manager.aclose()
    with pytest.raises(PluginFailure, match="shutting down"):
        await call
    session = rig.record("p").worker._session
    assert session.dead
    assert await gone(session.proc.pid)


# --------------------------------------------------------------------------- #
# Secrets in settings do not leak through what a worker says
# --------------------------------------------------------------------------- #

SECRET = "hunter2-SECRET-VALUE"


@respx.mock
async def test_a_failure_that_quotes_a_setting_is_redacted_everywhere(
    rigs, tmp_path, caplog
) -> None:
    write_plugin(tmp_path, "p", settings={"api_key": SECRET})
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    with caplog.at_level(logging.INFO):
        await rig.bot.handle(event("!hi leak"))
        await rig.bot.handle(event("!plugins p", actor_id="users/maser", message_id=101))
        await rig.bot.handle(event("!plugins", actor_id="users/maser", message_id=102))
        await rig.manager.aclose()
    assert SECRET not in caplog.text
    assert SECRET not in " ".join(texts(route))
    assert SECRET not in "\n".join(rig.manager.check_lines())
    assert "Last error: RuntimeError: bad key ***" in texts(route)[1]
    assert "plugin p: the hi handler failed: RuntimeError: bad key ***" in caplog.text
    assert "plugin p: Traceback: RuntimeError: bad key ***" in caplog.text


async def test_a_load_error_that_quotes_a_setting_is_redacted(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings={"api_key": SECRET, "load": "error-leak"})
    rig = await rigs(tmp_path)
    record = rig.record("p")
    assert record.status is Status.FAILED
    assert record.reason == "bad key ***"


async def test_short_values_are_left_alone(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "p", settings={"unit": "C", "mode": "on", "api_key": SECRET})
    rig = await rigs(tmp_path)
    assert rig.record("p").secrets == [SECRET]
    assert rig.record("p").redact("unit C on") == "unit C on"


async def test_a_visible_plugin_error_is_the_plugins_own_words(rigs, tmp_path) -> None:
    """What a plugin raises as PluginError is addressed to the chat and is not redacted:
    a plugin may well say the city it was configured with."""
    write_plugin(tmp_path, "p", settings={"city": "Berlin"})
    rig = await rigs(tmp_path)
    outcome = await raw_call(rig, "pluginerror No weather in Berlin today")
    assert outcome.user_visible
    assert outcome.error == "No weather in Berlin today"


# --------------------------------------------------------------------------- #
# stderr
# --------------------------------------------------------------------------- #


async def test_a_flood_of_bare_newlines_on_stderr_is_cheap_and_logged_once(
    rigs, tmp_path, caplog
) -> None:
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path)
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="sable.plugins"):
        assert (await raw_call(rig, "nlflood")).reply == "wrote"
        await rig.manager.aclose()
    assert time.monotonic() - started < 5.0
    assert len([m for m in caplog.messages if m.startswith("plugin p:")]) <= 2


# --------------------------------------------------------------------------- #
# Limits the worker could not be given
# --------------------------------------------------------------------------- #


def test_the_bootstrap_says_so_when_a_limit_cannot_be_set(tmp_path) -> None:
    (tmp_path / "quiet_host.py").write_text("def main():\n    pass\n")

    def lower_the_ceiling() -> None:
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))

    done = subprocess.run(  # noqa: S603 - our own interpreter and a script we just wrote
        [sys.executable, "-I", "-c", bootstrap_source(str(tmp_path), 12, "quiet_host")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        preexec_fn=lower_the_ceiling,
    )
    assert done.returncode == 0, done.stderr
    assert "could not set RLIMIT_NOFILE" in done.stderr


@pytest.mark.parametrize(
    ("code", "words"),
    [
        (0, "exited with status 0"),
        (3, "exited with status 3"),
        (-9, "killed by SIGKILL"),
        (-11, "killed by SIGSEGV"),
        (-24, "killed by SIGXCPU"),
        (-15, "killed by SIGTERM"),
        (-999, "killed by signal 999"),
        (None, "did not exit"),
    ],
)
def test_exit_statuses_are_described_in_words(code, words) -> None:
    assert words in _exit_description(code)


async def test_without_pidfd_the_exit_is_still_noticed_promptly(
    rigs, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("sable.plugins._open_pidfd", lambda pid: None)
    write_plugin(tmp_path, "p")
    rig = await rigs(tmp_path, call_timeout=20)
    started = time.monotonic()
    with pytest.raises(PluginFailure, match="crashed"):
        await raw_call(rig, "orphan 3")
    assert time.monotonic() - started < 1.5
    assert "exited with status 5" in rig.record("p").last_error
