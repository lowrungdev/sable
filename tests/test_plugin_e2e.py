"""Plugins end to end: the real worker (sable.plugin_host), not the fake.

A few of these only, because each starts a real Python process: what matters here
is that the two halves of the protocol agree with each other, and that what the
core guarantees holds for the real thing.
"""

from __future__ import annotations

import logging
import os
import re
import resource
import textwrap
from pathlib import Path

import respx

from conftest import event, make_config
from plugin_helpers import message_route, posix_only, texts, write_plugin
from sable.__main__ import main
from sable.app import create_app
from sable.commands import Registry
from sable.plugins import Status, default_command

pytestmark = posix_only

WEATHER = textwrap.dedent(
    """
    from helpers import shout
    from sable.plugin_api import PluginError, command


    @command("weather", aliases=("wx",), help="Forecast for a city", usage="weather <city>")
    async def weather(ctx):
        if not ctx.args:
            raise PluginError("Which city?")
        if ctx.args == "boom":
            raise RuntimeError("kaboom")
        await ctx.reply(f"checking {ctx.args}")
        return shout(f"sunny in {ctx.args} ({ctx.settings['unit']}, {ctx.actor_id})")


    def check(settings):
        if settings.get("unit") not in ("C", "F"):
            return "unit must be C or F"
    """
)

HELPERS = "def shout(text):\n    return text.upper()\n"


def weather_plugin(root: Path, **kwargs) -> Path:
    folder = write_plugin(
        root, "weather", source=WEATHER, settings=kwargs.pop("settings", {"unit": "C"}), **kwargs
    )
    (folder / "helpers.py").write_text(HELPERS, encoding="utf-8")
    return folder


@respx.mock
async def test_a_real_plugin_answers_a_command(rigs, tmp_path) -> None:
    weather_plugin(tmp_path)
    rig = await rigs(tmp_path, command=default_command)
    assert rig.record("weather").status is Status.ACTIVE, rig.record("weather").reason
    route = message_route()
    await rig.bot.handle(event("!weather Berlin"))
    await rig.bot.handle(event("!wx Paris", message_id=101))
    assert texts(route) == [
        "checking Berlin",
        "SUNNY IN BERLIN (C, USERS/ALICE)",
        "checking Paris",
        "SUNNY IN PARIS (C, USERS/ALICE)",
    ]


@respx.mock
async def test_a_real_plugins_error_and_crash_reach_the_room_differently(
    rigs, tmp_path, caplog
) -> None:
    weather_plugin(tmp_path)
    rig = await rigs(tmp_path, command=default_command)
    route = message_route()
    with caplog.at_level(logging.INFO):
        await rig.bot.handle(event("!weather"))
        await rig.bot.handle(event("!weather boom", message_id=101))
    assert texts(route) == ["Which city?", "⚠️ Sorry - the `weather` plugin crashed"]
    assert "kaboom" not in " ".join(texts(route))
    # The traceback is in the log, under the plugin's name.
    assert "plugin weather: " in caplog.text
    assert "kaboom" in caplog.text


async def test_a_real_check_can_reject_the_settings(rigs, tmp_path) -> None:
    weather_plugin(tmp_path, settings={"unit": "K"})
    rig = await rigs(tmp_path, command=default_command)
    record = rig.record("weather")
    assert record.status is Status.FAILED
    assert "unit must be C or F" in record.reason


async def test_a_real_import_error_fails_the_plugin_with_the_reason(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "broken", source="import no_such_module_anywhere\n")
    rig = await rigs(tmp_path, command=default_command)
    record = rig.record("broken")
    assert record.status is Status.FAILED
    assert "no_such_module_anywhere" in record.reason


async def test_an_inactive_plugin_is_validated_but_its_code_never_runs(rigs, tmp_path) -> None:
    marker = tmp_path / "ran-inactive"
    active_marker = tmp_path / "ran-active"
    source = (
        "from sable.plugin_api import command\n"
        "open({marker!r}, 'w').write('x')\n"
        "@command('{name}')\n"
        "async def go(ctx):\n    return 'x'\n"
    )
    plugins = tmp_path / "plugins"
    write_plugin(
        plugins,
        "sleeper",
        rooms=None,
        source=source.format(marker=str(marker), name="sleep"),
    )
    write_plugin(
        plugins,
        "waker",
        source=source.format(marker=str(active_marker), name="wake"),
    )
    write_plugin(
        plugins,
        "off",
        enabled=False,
        source=source.format(marker=str(marker), name="off"),
    )
    rig = await rigs(plugins, command=default_command)
    assert rig.record("sleeper").status is Status.INACTIVE
    assert rig.record("off").status is Status.DISABLED
    assert rig.record("waker").status is Status.ACTIVE
    assert active_marker.exists()  # the control: an active plugin is imported
    assert not marker.exists()


@respx.mock
async def test_the_real_worker_gets_a_scrubbed_environment_and_limits(
    rigs, tmp_path, monkeypatch
) -> None:
    secret = "TOPSECRET-app-password"
    monkeypatch.setenv("SABLE_NEXTCLOUD_PASSWORD", secret)
    monkeypatch.setenv("SABLE_NOTIFY_TOKEN", secret)
    source = textwrap.dedent(
        """
        import os, resource
        from sable.plugin_api import command

        @command("look")
        async def look(ctx):
            limits = {n: resource.getrlimit(getattr(resource, n))
                      for n in ("RLIMIT_AS", "RLIMIT_NOFILE", "RLIMIT_CORE", "RLIMIT_CPU")}
            return repr((sorted(os.environ), os.getcwd(), limits))
        """
    )
    folder = write_plugin(tmp_path, "look", source=source)
    rig = await rigs(tmp_path, command=default_command, call_timeout=7)
    route = message_route()
    await rig.bot.handle(event("!look"))
    body = texts(route)[0]
    assert secret not in body
    assert "SABLE_" not in body
    assert str(folder.resolve()) in body
    assert "'RLIMIT_AS': (1073741824, 1073741824)" in body
    assert "'RLIMIT_NOFILE': (64, 64)" in body
    assert "'RLIMIT_CORE': (0, 0)" in body
    # The soft limit is the per-call budget (7 s x 10), re-armed at every call so a
    # little above it; the hard limit is unlimited.
    cpu = re.search(r"'RLIMIT_CPU': \((\d+), (-?\d+)\)", body)
    assert cpu is not None
    assert 70 <= int(cpu.group(1)) <= 75
    assert int(cpu.group(2)) == resource.RLIM_INFINITY


@respx.mock
async def test_a_real_plugin_that_prints_does_not_break_the_protocol(
    rigs, tmp_path, caplog
) -> None:
    source = textwrap.dedent(
        """
        from sable.plugin_api import command

        @command("noisy")
        async def noisy(ctx):
            print("this goes to stdout")
            return "still fine"
        """
    )
    write_plugin(tmp_path, "noisy", source=source)
    rig = await rigs(tmp_path, command=default_command)
    route = message_route()
    with caplog.at_level(logging.INFO, logger="sable.plugins"):
        await rig.bot.handle(event("!noisy"))
        await rig.manager.aclose()
    assert texts(route) == ["still fine"]
    assert "plugin noisy: this goes to stdout" in caplog.messages


@respx.mock
async def test_a_real_handler_that_never_returns_is_killed(rigs, tmp_path) -> None:
    source = textwrap.dedent(
        """
        import asyncio
        from sable.plugin_api import command

        @command("stuck")
        async def stuck(ctx):
            await asyncio.sleep(60)

        @command("fine")
        async def fine(ctx):
            return "fine"
        """
    )
    write_plugin(tmp_path, "stuck", source=source)
    rig = await rigs(tmp_path, command=default_command, call_timeout=0.7)
    route = message_route()
    await rig.bot.handle(event("!stuck"))
    await rig.bot.handle(event("!fine", message_id=101))
    assert texts(route) == ["⚠️ Sorry - the `stuck` plugin took too long", "fine"]


@respx.mock
async def test_a_real_plugin_may_not_send_to_a_foreign_room(rigs, tmp_path) -> None:
    source = textwrap.dedent(
        """
        from sable.plugin_api import PluginActionError, command

        @command("leak")
        async def leak(ctx):
            try:
                await ctx.send("efgh5678", "psst")
            except PluginActionError as exc:
                return f"refused: {exc}"
            return "sent"
        """
    )
    write_plugin(tmp_path, "leak", source=source)
    rig = await rigs(tmp_path, command=default_command)
    route = message_route()
    other = message_route("efgh5678")
    await rig.bot.handle(event("!leak"))
    assert texts(route) == ["refused: this plugin may not post to that conversation"]
    assert other.call_count == 0


# --------------------------------------------------------------------------- #
# Through the app and the command line, which build the manager themselves
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_app_starts_the_real_plugins_and_stops_their_workers(tmp_path, caplog) -> None:
    weather_plugin(tmp_path)
    app = create_app(make_config(plugins_dir=str(tmp_path)), receive=False)
    route = message_route()
    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            await app.state.bot.handle(event("!weather Rome"))
            worker = app.state.plugins.records[0].worker
            assert not worker.needs_restart
    assert "  plugins:        1 active (" + str(tmp_path) + ")" in caplog.messages
    assert texts(route)[-1] == "SUNNY IN ROME (C, USERS/ALICE)"
    assert worker.needs_restart is False  # closed, not restarting
    assert worker._session.dead


def test_check_runs_the_real_handshake(monkeypatch, capsys, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)
    root = tmp_path / "plugins"
    weather_plugin(root)
    write_plugin(root, "broken", source="import no_such_module_anywhere\n")
    monkeypatch.setenv("SABLE_NEXTCLOUD_URL", "https://cloud.example.org")
    monkeypatch.setenv("SABLE_NEXTCLOUD_USER", "sable")
    monkeypatch.setenv("SABLE_NEXTCLOUD_PASSWORD", "app-password-1234")
    monkeypatch.setenv("SABLE_PLUGINS_DIR", str(root))
    assert main(["--check", "--env-file", str(tmp_path / "none.env")]) == 0
    out = capsys.readouterr().out
    assert "1 active, 1 failed" in out
    assert "weather: active (commands: weather, wx)" in out
    assert "broken: failed: import failed: ModuleNotFoundError" in out
    monkeypatch.setenv("SABLE_PLUGINS_STRICT", "true")
    assert main(["--check", "--env-file", str(tmp_path / "none.env")]) == 2


async def test_each_plugin_gets_a_worker_of_its_own(rigs, tmp_path) -> None:
    source = textwrap.dedent(
        """
        from sable.plugin_api import command

        @command("who-{n}")
        async def who(ctx):
            return "x"
        """
    )
    write_plugin(tmp_path, "one", source=source.format(n="one"))
    write_plugin(tmp_path, "two", source=source.format(n="two"))
    rig = await rigs(tmp_path, command=default_command)
    pids = {r.worker._session.proc.pid for r in rig.manager.records}
    assert len(pids) == 2


async def test_check_keeps_the_workers_stderr_out_of_the_log_at_info(tmp_path, caplog) -> None:
    from sable.commands import Registry
    from sable.plugins import check_plugins

    source = (
        "import sys\n"
        "from sable.plugin_api import command\n"
        "sys.stderr.write('noisy import line' + chr(10))\n"
        "@command('quiet')\n"
        "async def quiet(ctx):\n    return 'x'\n"
    )
    write_plugin(tmp_path, "noisy", source=source)
    with caplog.at_level(logging.INFO):
        lines, refused = await check_plugins(make_config(plugins_dir=str(tmp_path)), Registry())
    assert not refused
    assert "noisy import line" not in caplog.text
    assert any("noisy: active" in line for line in lines)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="sable.plugins"):
        await check_plugins(make_config(plugins_dir=str(tmp_path)), Registry())
    assert "plugin noisy: noisy import line" in caplog.text


async def test_a_relative_plugins_directory_works_with_the_real_worker(
    rigs, tmp_path, monkeypatch
) -> None:
    from sable.plugins import PluginManager

    weather_plugin(tmp_path / "plugins")
    monkeypatch.chdir(tmp_path)
    manager = PluginManager(make_config(plugins_dir="plugins"))
    try:
        await manager.load_all(Registry())
        record = manager.records[0]
        assert record.status is Status.ACTIVE, record.reason
    finally:
        await manager.aclose()
