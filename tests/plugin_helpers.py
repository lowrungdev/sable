"""Shared scaffolding for the plugin tests: a plugins directory, a fake clock, and a
bot wired to a plugin manager that drives ``fake_worker.py`` instead of the real host."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from conftest import ROOM, TALK, FakeLLM, make_config
from sable.bot import Bot
from sable.commands import registry
from sable.config import Config
from sable.plugins import CommandFactory, PluginManager, PluginRecord

FAKE = Path(__file__).with_name("fake_worker.py")

#: Real worker processes need POSIX process groups and resource limits.
posix_only = pytest.mark.skipif(os.name != "posix", reason="plugin workers are POSIX only")


def fake_command(record: PluginRecord, cpu_seconds: int) -> list[str]:
    """Run the scriptable fake instead of the real host."""
    return [sys.executable, "-I", "-S", str(FAKE), str(record.entry)]


#: A fixed, deterministic start for the fake wall clock: a Monday (matters for
#: cron's weekday field), midnight UTC.
WALL_START = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


class FakeClock:
    """A monotonic clock a test moves by hand - and, in lockstep, a wall-clock
    datetime, for schedules: cron needs real calendar time, which a monotonic
    float cannot give it, but both advance together from one ``.advance()`` call,
    so a test never has to keep two fakes in sync by hand.
    """

    def __init__(self, start: float = 1000.0, wall_start: datetime = WALL_START) -> None:
        self.now = start
        self._wall = wall_start

    def __call__(self) -> float:
        return self.now

    def wall(self) -> datetime:
        """The matching wall-clock moment, for ``PluginManager(wall_clock=...)``."""
        return self._wall

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self._wall += timedelta(seconds=seconds)


def write_plugin(
    root: Path,
    name: str,
    *,
    directory: str | None = None,
    rooms: Sequence[str] | None = (ROOM,),
    users: Sequence[str] | None = None,
    admins_only: bool | None = None,
    enabled: bool | None = None,
    settings: dict[str, Any] | None = None,
    source: str = "x = 1\n",
    raw_yaml: str | None = None,
) -> Path:
    """Write ``<root>/<directory or name>/<name>.py`` and its settings file."""
    folder = root / (directory if directory is not None else name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.py").write_text(source, encoding="utf-8")
    if raw_yaml is None:
        document: dict[str, Any] = {}
        if enabled is not None:
            document["enabled"] = enabled
        access: dict[str, Any] = {}
        if rooms is not None:
            access["rooms"] = list(rooms)
        if users is not None:
            access["users"] = list(users)
        if admins_only is not None:
            access["admins_only"] = admins_only
        if access:
            document["access"] = access
        if settings is not None:
            document["settings"] = settings
        raw_yaml = yaml.safe_dump(document)
    (folder / f"{name}_settings.yaml").write_text(raw_yaml, encoding="utf-8")
    return folder


def message_route(room: str = ROOM) -> respx.Route:
    return respx.post(f"{TALK}/chat/{room}").mock(
        return_value=httpx.Response(201, json={"ocs": {"data": {"id": 1}}})
    )


def reaction_route(room: str = ROOM, message_id: int = 100) -> respx.Route:
    return respx.post(f"{TALK}/reaction/{room}/{message_id}").mock(
        return_value=httpx.Response(201, json={"ocs": {"data": {}}})
    )


def sent(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


def texts(route: respx.Route) -> list[str]:
    return [body["message"] for body in sent(route)]


@dataclass
class Rig:
    """A bot with a plugin manager attached, over a directory in tmp_path."""

    root: Path
    config: Config
    bot: Bot
    manager: PluginManager
    clock: FakeClock

    def record(self, name: str) -> PluginRecord:
        return next(r for r in self.manager.records if r.name == name)


class RigFactory:
    """Builds rigs and closes every one of them when the test is over."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.opened: list[Rig] = []

    async def __call__(
        self,
        root: Path,
        *,
        call_timeout: float = 5.0,
        load_timeout: float = 5.0,
        attach: bool = True,
        command: CommandFactory = fake_command,
        **config: Any,
    ) -> Rig:
        cfg = make_config(
            plugins_dir=str(root), plugins_timeout=max(1, int(call_timeout)), **config
        )
        clock = FakeClock()
        bot = Bot(cfg, http_client=self.http, llm=FakeLLM())  # type: ignore[arg-type]
        manager = PluginManager(
            cfg,
            timeout=call_timeout,
            load_timeout=load_timeout,
            command=command,
            clock=clock,
            wall_clock=clock.wall,
        )
        await manager.load_all(bot.registry)
        if attach:
            bot.attach_plugins(manager)
        rig = Rig(root, cfg, bot, manager, clock)
        self.opened.append(rig)
        return rig

    async def close(self) -> None:
        for rig in self.opened:
            await rig.manager.aclose()


def builtin_names() -> set[str]:
    """What the module-level registry holds, for proving that nothing mutates it."""
    return {command.name for command in registry.visible()} | set(registry._commands)
