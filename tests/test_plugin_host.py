"""The real worker, as a subprocess, driven over its pipes the way the core drives it."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import signal
import sys
import textwrap
from collections.abc import AsyncIterator, Coroutine, Iterable
from pathlib import Path
from typing import Any

import pytest

import sable

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the plugin worker is POSIX-only (process groups, pipes)"
)

#: Where the ``sable`` package under test lives; the same computation the core does.
SABLE_PARENT = str(Path(sable.__file__).resolve().parent.parent)

#: The spawn command from the contract: isolated mode, the package directory put first
#: on the path by a one-line bootstrap, the entry path as the argument.
BOOTSTRAP = (
    f"import sys; sys.path.insert(0, {SABLE_PARENT!r}); from sable.plugin_host import main; main()"
)

TIMEOUT = 10.0


def worker_command(entry: Path, cpu: tuple[int, int | None] | None = None) -> list[str]:
    """The spawn command; ``cpu`` is the (soft seconds, hard seconds or None for unlimited)
    RLIMIT_CPU the core's bootstrap would set before handing over."""
    code = BOOTSTRAP
    if cpu is not None:
        soft, hard = cpu
        hard_text = "resource.RLIM_INFINITY" if hard is None else str(hard)
        limit = f"resource.setrlimit(resource.RLIMIT_CPU, ({soft}, {hard_text}))"
        code = f"import resource; {limit}; {code}"
    return [sys.executable, "-I", "-c", code, str(entry)]


def write_plugin(directory: Path, source: str, stem: str = "demo") -> Path:
    path = directory / f"{stem}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


class Worker:
    """Just enough of a core to drive one worker process."""

    def __init__(self, process: asyncio.subprocess.Process, entry: Path) -> None:
        self.process = process
        self.entry = entry
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr_task = asyncio.create_task(process.stderr.read())

    async def send(self, message: dict[str, Any]) -> None:
        await self.send_raw(json.dumps(message, separators=(",", ":")).encode() + b"\n")

    async def send_raw(self, data: bytes) -> None:
        self._stdin.write(data)
        await self._stdin.drain()

    async def recv(self) -> dict[str, Any]:
        line = await asyncio.wait_for(self._stdout.readline(), TIMEOUT)
        assert line, "the worker closed stdout"
        message = json.loads(line)
        assert isinstance(message, dict)
        return message

    async def load(
        self, settings: dict[str, Any] | None = None, plugin: str = "demo", rid: int = 1
    ) -> dict[str, Any]:
        await self.send(
            {
                "op": "load",
                "id": rid,
                "plugin": plugin,
                "path": str(self.entry),
                "settings": settings or {},
            }
        )
        return await self.recv()

    async def request_call(self, handler: str, rid: int, **ctx: Any) -> None:
        context: dict[str, Any] = {
            "trigger": "command",
            "name": handler.split(":", 1)[-1],
            "args": "",
            "argv": [],
            "room": "abcd1234",
            "actor_id": "alice",
            "actor_name": "Alice",
            "is_admin": False,
            "message_id": 42,
            "text": "!x",
            "match": "",
        }
        context.update(ctx)
        await self.send({"op": "call", "id": rid, "handler": handler, "ctx": context})

    async def call(self, handler: str, rid: int = 10, **ctx: Any) -> dict[str, Any]:
        await self.request_call(handler, rid, **ctx)
        return await self.recv()

    async def finish(self) -> int:
        """Close stdin and wait for the worker to exit; returns its status."""
        self._stdin.close()
        return await asyncio.wait_for(self.process.wait(), TIMEOUT)

    async def stderr(self) -> str:
        return (await asyncio.wait_for(self._stderr_task, TIMEOUT)).decode(errors="replace")

    async def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        await self.process.wait()


@pytest.fixture
async def start(tmp_path: Path) -> AsyncIterator[Any]:
    workers: list[Worker] = []
    numbers = itertools.count()

    async def spawn(
        source: str,
        files: dict[str, str] | None = None,
        cpu: tuple[int, int | None] | None = None,
    ) -> Worker:
        directory = tmp_path / f"worker{next(numbers)}"
        directory.mkdir()
        entry = write_plugin(directory, source)
        for name, text in (files or {}).items():
            (directory / name).write_text(textwrap.dedent(text), encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            *worker_command(entry, cpu),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=directory,
            # Built from nothing, like the core's: no PYTHON* and no secrets reach it.
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
            start_new_session=True,
        )
        worker = Worker(process, entry)
        workers.append(worker)
        return worker

    yield spawn
    for worker in workers:
        await worker.kill()


async def started(
    start: Any,
    source: str,
    files: dict[str, str] | None = None,
    settings: dict[str, Any] | None = None,
) -> Worker:
    """A worker with the plugin loaded successfully."""
    worker: Worker = await start(source, files)
    reply = await worker.load(settings)
    assert reply["op"] == "loaded", reply
    return worker


async def run_all(cases: Iterable[Coroutine[Any, Any, None]]) -> None:
    """Run table cases at once (start-up dominates their cost) and report every failure."""
    outcomes = await asyncio.gather(*cases, return_exceptions=True)
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    if failures:
        raise AssertionError(f"{len(failures)} case(s) failed: {failures!r}")


GREETER = """
    from sable.plugin_api import Context, command

    @command("hello", aliases=("hi",), help="Say hello", usage="hello [name]")
    async def hello(ctx: Context) -> str | None:
        return f"Hello, {ctx.args or ctx.actor_name}!"
"""


# --- load -----------------------------------------------------------------------------


async def test_load_reports_the_declarations(start: Any) -> None:
    worker = await start(GREETER)
    assert await worker.load() == {
        "op": "loaded",
        "id": 1,
        "declared": {
            "commands": [
                {"name": "hello", "aliases": ["hi"], "help": "Say hello", "usage": "hello [name]"}
            ],
            "phrases": [],
            "schedules": [],
            "has_check": False,
        },
    }


async def test_happy_path_call(start: Any) -> None:
    worker = await started(start, GREETER)
    assert await worker.call("command:hello", rid=7, args="world") == {
        "op": "result",
        "id": 7,
        "ok": True,
        "reply": "Hello, world!",
    }
    reply = await worker.call("command:hello", rid=8)
    assert reply["reply"] == "Hello, Alice!"


async def test_the_context_carries_everything_the_core_sent(start: Any) -> None:
    worker = await started(
        start,
        """
        import json
        from sable.plugin_api import Context, command

        @command("dump")
        async def dump(ctx: Context) -> str:
            return json.dumps({
                "plugin": ctx.plugin, "trigger": ctx.trigger, "name": ctx.name,
                "args": ctx.args, "argv": ctx.argv, "room": ctx.room,
                "actor_id": ctx.actor_id, "actor_name": ctx.actor_name,
                "user_id": ctx.user_id,
                "is_admin": ctx.is_admin, "message_id": ctx.message_id,
                "text": ctx.text, "match": ctx.match, "log": ctx.log.name,
            })
        """,
    )
    reply = await worker.call(
        "command:dump",
        args="a b",
        argv=["a", "b"],
        is_admin=True,
        text="!dump a b",
        actor_name="Zoë ☃",
        actor_id="users/alice",
        user_id="alice",
    )
    assert json.loads(reply["reply"]) == {
        "plugin": "demo",
        "trigger": "command",
        "name": "dump",
        "args": "a b",
        "argv": ["a", "b"],
        "room": "abcd1234",
        "actor_id": "users/alice",
        "actor_name": "Zoë ☃",
        "user_id": "alice",
        "is_admin": True,
        "message_id": 42,
        "text": "!dump a b",
        "match": "",
        "log": "sable.plugin.demo",
    }
    # A core that does not send user_id (a guest, a schedule, an older core) gets "".
    reply = await worker.call("command:dump", rid=11)
    assert json.loads(reply["reply"])["user_id"] == ""


async def test_a_handler_may_return_none_for_silence(start: Any) -> None:
    worker = await started(
        start,
        """
        from sable.plugin_api import command

        @command("quiet")
        async def quiet(ctx):
            return None
        """,
    )
    assert (await worker.call("command:quiet"))["reply"] is None


async def test_helper_modules_in_the_plugin_directory_import(start: Any) -> None:
    worker = await started(
        start,
        """
        from helpers import shout
        from sable.plugin_api import command

        @command("shout")
        async def shout_it(ctx):
            return shout(ctx.args)
        """,
        files={"helpers.py": "def shout(text):\n    return text.upper() + '!'\n"},
    )
    assert (await worker.call("command:shout", args="hey"))["reply"] == "HEY!"


async def test_the_worker_environment_is_lean_and_leaves_no_bytecode(start: Any) -> None:
    worker = await started(
        start,
        """
        import json, os, sys
        import helpers
        from sable.plugin_api import command

        @command("where")
        async def where(ctx):
            return json.dumps({
                "path0_is_plugin_dir": sys.path[0] == os.path.dirname(os.path.abspath(__file__)),
                "no_bytecode": sys.dont_write_bytecode,
                "pycache": os.path.exists(os.path.join(os.path.dirname(__file__), "__pycache__")),
                # Importing sable must not drag in the package metadata machinery (~90 ms).
                "metadata_imported": "importlib.metadata" in sys.modules,
            })
        """,
        files={"helpers.py": "VALUE = 1\n"},
    )
    reply = await worker.call("command:where")
    assert json.loads(reply["reply"]) == {
        "path0_is_plugin_dir": True,
        "no_bytecode": True,
        "pycache": False,
        "metadata_imported": False,
    }


async def test_a_second_load_is_refused(start: Any) -> None:
    worker = await started(start, GREETER)
    reply = await worker.load(rid=2)
    assert reply["op"] == "error"
    assert reply["id"] == 2
    assert "already" in reply["error"]
    assert (await worker.call("command:hello"))["ok"] is True


# --- load failures --------------------------------------------------------------------

#: label -> (source, helper files, fragments the error message must contain). Every case
#: is a real worker; they are started together because process start-up is most of the cost.
LOAD_FAILURES: dict[str, tuple[str, dict[str, str], tuple[str, ...]]] = {
    "import error": (
        "import no_such_module_anywhere\n",
        {},
        ("ModuleNotFoundError", "no_such_module_anywhere"),
    ),
    "exception at import": (
        "x = 1\nraise RuntimeError('boom at import')\n",
        {},
        ("boom at import", "demo.py:2"),
    ),
    "exception in a helper": (
        "import helpers\n",
        {"helpers.py": "1 / 0\n"},
        ("ZeroDivisionError", "helpers.py:1"),
    ),
    "syntax error": ("def broken(:\n    pass\n", {}, ("syntax error", "demo.py", "line 1")),
    "syntax error in a helper": (
        "import helpers\n",
        {"helpers.py": "x = = 1\n"},
        ("syntax error", "helpers.py"),
    ),
    "sys.exit at import": ("import sys\nsys.exit(3)\n", {}, ("SystemExit",)),
    "bad command name": (
        """
        from sable.plugin_api import command

        @command("Not Valid")
        async def f(ctx):
            return None
        """,
        {},
        ("invalid declaration", "command name", "'Not Valid'", "demo.py:4"),
    ),
    "sync handler": (
        """
        from sable.plugin_api import command

        @command("a")
        def f(ctx):
            return None
        """,
        {},
        ("invalid declaration", "async def"),
    ),
    "duplicate command": (
        """
        from sable.plugin_api import command

        @command("a", aliases=("b",))
        async def one(ctx):
            return None

        @command("b")
        async def two(ctx):
            return None
        """,
        {},
        ("'b' is declared more than once",),
    ),
    "check is not a function": ("check = 5\n", {}, ("check",)),
}


async def test_every_kind_of_load_failure_becomes_an_error_reply(start: Any) -> None:
    async def one(label: str, source: str, files: dict[str, str], needles: tuple[str, ...]) -> None:
        worker: Worker = await start(source, files)
        reply = await worker.load()
        assert reply["op"] == "error", (label, reply)
        assert reply["id"] == 1, label
        for needle in needles:
            assert needle in reply["error"], (label, reply["error"])
        assert "Traceback" not in reply["error"], label
        # Still alive and honest: it refuses calls rather than dying.
        assert (await worker.call("command:anything"))["ok"] is False, label

    await run_all(one(label, *case) for label, case in LOAD_FAILURES.items())


async def test_bad_load_requests_are_error_replies_and_a_good_load_still_works(
    start: Any,
) -> None:
    worker = await start(GREETER)
    await worker.send({"op": "load", "id": 1, "plugin": "demo", "path": 5, "settings": {}})
    reply = await worker.recv()
    assert (reply["op"], reply["id"]) == ("error", 1)
    await worker.send(
        {"op": "load", "id": 2, "plugin": "demo", "path": "/nonexistent/demo.py", "settings": {}}
    )
    reply = await worker.recv()
    assert (reply["op"], reply["id"]) == ("error", 2)
    assert "import failed" in reply["error"]
    assert (await worker.load(rid=3))["op"] == "loaded"


async def test_a_failed_plugin_that_wrote_to_stdout_did_not_corrupt_the_reply(start: Any) -> None:
    worker = await start("print('before the failure')\nraise RuntimeError('x')\n")
    reply = await worker.load()
    assert reply["op"] == "error"
    await worker.finish()
    assert "before the failure" in await worker.stderr()


# --- check ----------------------------------------------------------------------------

#: label -> (source, settings, expected result fields). All have a ``check``.
CHECKS: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {
    "ok": ("def check(settings):\n    return None\n", {}, {"ok": True}),
    "error string": (
        """
        def check(settings):
            if "api_key" not in settings:
                return "settings.api_key is required"
        """,
        {},
        {"ok": False, "error": "settings.api_key is required"},
    ),
    "sees the settings": (
        """
        def check(settings):
            return None if settings.get("api_key") == "k" else "wrong key"
        """,
        {"api_key": "k"},
        {"ok": True},
    ),
    "async rejecting": (
        """
        import asyncio

        async def check(settings):
            await asyncio.sleep(0)
            return "async says no"
        """,
        {},
        {"ok": False, "error": "async says no"},
    ),
    "async accepting": (
        """
        import asyncio

        async def check(settings):
            await asyncio.sleep(0)
        """,
        {},
        {"ok": True},
    ),
    "plugin error": (
        """
        from sable.plugin_api import PluginError

        def check(settings):
            raise PluginError("fix the settings")
        """,
        {},
        {"ok": False, "error": "fix the settings"},
    ),
    "raises": (
        "def check(settings):\n    raise KeyError('api_key')\n",
        {},
        {"ok": False},
    ),
    "returns a bool": ("def check(settings):\n    return True\n", {}, {"ok": False}),
    "returns an empty string": ("def check(settings):\n    return ''\n", {}, {"ok": False}),
}


async def test_check_results(start: Any) -> None:
    async def one(label: str, source: str, settings: dict[str, Any], want: dict[str, Any]) -> None:
        worker: Worker = await start(source)
        loaded = await worker.load(settings)
        assert loaded["op"] == "loaded", (label, loaded)
        assert loaded["declared"]["has_check"] is True, label
        await worker.send({"op": "check", "id": 2})
        reply = await worker.recv()
        assert reply["op"] == "result", (label, reply)
        assert reply["id"] == 2, label
        for key, value in want.items():
            assert reply[key] == value, (label, reply)
        if not want["ok"]:
            assert reply["error"], (label, reply)
        if label == "raises":
            assert "KeyError" in reply["error"]
            await worker.finish()
            assert "Traceback" in await worker.stderr()
        if label == "returns a bool":
            assert "string or None" in reply["error"]

    await run_all(one(label, *case) for label, case in CHECKS.items())


async def test_check_without_a_check_function_is_ok(start: Any) -> None:
    worker = await started(start, GREETER)
    await worker.send({"op": "check", "id": 2})
    assert await worker.recv() == {"op": "result", "id": 2, "ok": True}


async def test_check_before_load_fails_cleanly(start: Any) -> None:
    worker = await start(GREETER)
    await worker.send({"op": "check", "id": 2})
    reply = await worker.recv()
    assert (reply["ok"], reply["id"]) == (False, 2)


# --- settings are frozen --------------------------------------------------------------


async def test_settings_are_deeply_frozen_in_handlers_and_check(start: Any) -> None:
    worker = await started(
        start,
        """
        import json
        from sable.plugin_api import command

        def check(settings):
            try:
                settings["new"] = 1
            except TypeError:
                return None
            return "check could mutate settings"

        @command("probe")
        async def probe(ctx):
            outcome = {}
            for label, mutate in {
                "top": lambda: ctx.settings.__setitem__("new", 1),
                "nested": lambda: ctx.settings["db"].__setitem__("host", "evil"),
                "list": lambda: ctx.settings["hosts"].append("evil"),
            }.items():
                try:
                    mutate()
                    outcome[label] = "mutated"
                except (TypeError, AttributeError):
                    outcome[label] = "frozen"
            outcome["hosts"] = list(ctx.settings["hosts"])
            outcome["db"] = dict(ctx.settings["db"])
            return json.dumps(outcome)
        """,
        settings={"db": {"host": "localhost"}, "hosts": ["a", "b"]},
    )
    await worker.send({"op": "check", "id": 2})
    assert await worker.recv() == {"op": "result", "id": 2, "ok": True}
    reply = await worker.call("command:probe")
    assert json.loads(reply["reply"]) == {
        "top": "frozen",
        "nested": "frozen",
        "list": "frozen",
        "hosts": ["a", "b"],
        "db": {"host": "localhost"},
    }


# --- failure mapping ------------------------------------------------------------------


FAILING = """
    import sys
    from sable.plugin_api import PluginError, command

    @command("usage")
    async def usage(ctx):
        raise PluginError("Usage: !usage <thing>")

    @command("empty")
    async def empty(ctx):
        raise PluginError("  ")

    @command("crash")
    async def crash(ctx):
        raise RuntimeError("kaboom with a secret detail")

    @command("quit")
    async def quit_(ctx):
        sys.exit(4)

    @command("number")
    async def number(ctx):
        return 42

    @command("bytes")
    async def as_bytes(ctx):
        return b"hi"

    @command("huge")
    async def huge(ctx):
        return "x" * (2 * 1024 * 1024)

    @command("ok")
    async def ok(ctx):
        return "still here"
"""


async def test_plugin_errors_and_crashes_map_to_the_right_results(start: Any) -> None:
    worker = await started(start, FAILING)

    assert await worker.call("command:usage", rid=3) == {
        "op": "result",
        "id": 3,
        "ok": False,
        "error": "Usage: !usage <thing>",
        "user_visible": True,
    }

    # A PluginError with nothing to say is not shown: an empty chat message helps nobody.
    reply = await worker.call("command:empty")
    assert (reply["ok"], reply["user_visible"]) == (False, False)

    reply = await worker.call("command:crash", rid=4)
    assert reply["ok"] is False
    assert reply["user_visible"] is False
    assert "RuntimeError" in reply["error"]

    # SystemExit from a handler must not end the worker.
    reply = await worker.call("command:quit")
    assert (reply["ok"], reply["user_visible"]) == (False, False)
    assert "SystemExit" in reply["error"]

    assert (await worker.call("command:ok", rid=5))["reply"] == "still here"
    await worker.finish()
    err = await worker.stderr()
    assert "Traceback" in err
    assert "kaboom with a secret detail" in err


async def test_results_that_are_not_a_string_or_too_large_are_hidden_errors(start: Any) -> None:
    worker = await started(start, FAILING)
    for handler in ("number", "bytes"):
        reply = await worker.call(f"command:{handler}")
        assert (reply["ok"], reply["user_visible"]) == (False, False), handler
        assert "expected str or None" in reply["error"], handler

    reply = await worker.call("command:huge", rid=6)
    assert reply == {
        "op": "result",
        "id": 6,
        "ok": False,
        "error": "the result was too large to send",
        "user_visible": False,
    }
    assert (await worker.call("command:ok"))["ok"] is True


BAD_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("command:nope", {}),
    ("phrase:greet", {}),
    ("weird", {}),
    ("command:ok", {"trigger": "carrier-pigeon"}),
    ("command:ok", {"argv": "not a list"}),
    ("command:ok", {"argv": [1, 2]}),
    ("command:ok", {"args": 5}),
    ("command:ok", {"is_admin": "yes"}),
    ("command:ok", {"message_id": "7"}),
    ("command:ok", {"message_id": True}),
    ("command:ok", {"name": None}),
]


async def test_a_bad_call_gets_a_hidden_error_and_the_worker_lives(start: Any) -> None:
    worker = await started(start, FAILING)
    for index, (handler, ctx) in enumerate(BAD_CALLS):
        reply = await worker.call(handler, rid=100 + index, **ctx)
        assert (reply["id"], reply["ok"], reply["user_visible"]) == (100 + index, False, False), (
            handler,
            ctx,
        )
    assert (await worker.call("command:ok", rid=10))["ok"] is True


async def test_a_call_without_ctx_or_before_load_is_a_hidden_error(start: Any) -> None:
    worker = await start(FAILING)
    early = await worker.call("command:ok", rid=2)
    assert (early["ok"], early["user_visible"]) == (False, False)
    assert (await worker.load(rid=3))["op"] == "loaded"
    await worker.send({"op": "call", "id": 4, "handler": "command:ok"})
    reply = await worker.recv()
    assert (reply["id"], reply["ok"]) == (4, False)


async def test_garbage_on_stdin_is_ignored(start: Any) -> None:
    worker = await started(start, FAILING)
    await worker.send_raw(b"not json at all\n")
    await worker.send_raw(b"[1, 2, 3]\n")
    await worker.send_raw(b"\n")
    await worker.send({"op": "no-such-op", "id": 1})
    await worker.send({"op": "call", "handler": "command:ok", "ctx": {}})  # no id
    await worker.send({"op": "act_result", "id": "nonexistent", "ok": True})
    await worker.send({"op": "act_result", "id": ["unhashable"], "ok": True})
    assert (await worker.call("command:ok", rid=5))["reply"] == "still here"


# --- stdout belongs to the protocol ---------------------------------------------------


async def test_print_and_raw_fd_writes_do_not_corrupt_the_protocol(start: Any) -> None:
    worker = await start(
        """
        import os, subprocess, sys
        from sable.plugin_api import command

        print("import-time print")
        os.write(1, b"import-time raw write\\n")

        @command("noisy")
        async def noisy(ctx):
            print("handler print")
            sys.stdout.write("no newline")
            os.write(1, b"raw fd write\\n")
            subprocess.run(["echo", "child writes to inherited stdout"], check=True)
            sys.stdout.flush()
            return "clean"
        """
    )
    assert (await worker.load())["op"] == "loaded"
    assert (await worker.call("command:noisy"))["reply"] == "clean"
    await worker.finish()
    err = await worker.stderr()
    for noise in (
        "import-time print",
        "import-time raw write",
        "handler print",
        "no newline",
        "raw fd write",
        "child writes to inherited stdout",
    ):
        assert noise in err


async def test_input_in_a_plugin_cannot_steal_the_protocol(start: Any) -> None:
    worker = await started(
        start,
        """
        from sable.plugin_api import command

        @command("ask")
        async def ask(ctx):
            try:
                input()
            except EOFError:
                return "got EOF"
            return "read something"
        """,
    )
    assert (await worker.call("command:ask"))["reply"] == "got EOF"
    assert (await worker.call("command:ask", rid=11))["reply"] == "got EOF"


async def test_plugin_logging_goes_to_stderr(start: Any) -> None:
    worker = await started(
        start,
        """
        from sable.plugin_api import command

        @command("log")
        async def log(ctx):
            ctx.log.warning("careful: %s", "now")
            return None
        """,
    )
    await worker.call("command:log")
    await worker.finish()
    assert "WARNING careful: now" in await worker.stderr()


# --- actions --------------------------------------------------------------------------


ACTORS = """
    from sable.plugin_api import PluginActionError, command

    @command("act")
    async def act(ctx):
        await ctx.reply("one")
        await ctx.send("otherroom", "two", silent=True)
        await ctx.react("👍")
        return "done"

    @command("refused")
    async def refused(ctx):
        try:
            await ctx.send("forbidden", "x")
        except PluginActionError as exc:
            return f"caught: {exc}"
        return "not refused"

    @command("unhandled")
    async def unhandled(ctx):
        await ctx.send("forbidden", "x")
        return "unreachable"
"""


async def test_reply_send_and_react_round_trip_as_act_and_act_result(start: Any) -> None:
    worker = await started(start, ACTORS)
    await worker.request_call("command:act", 20)
    expected = [
        ("reply", {"text": "one", "silent": False}),
        ("send", {"room": "otherroom", "text": "two", "silent": True}),
        ("react", {"emoji": "👍"}),
    ]
    ids = []
    for action, args in expected:
        act = await worker.recv()
        assert act["op"] == "act"
        assert act["call"] == 20
        assert (act["action"], act["args"]) == (action, args)
        ids.append(act["id"])
        # The handler must be blocked on the verdict: nothing else arrives first.
        await worker.send({"op": "act_result", "id": act["id"], "ok": True})
    assert len(set(ids)) == 3
    assert await worker.recv() == {"op": "result", "id": 20, "ok": True, "reply": "done"}


async def test_the_handler_waits_for_each_act_result(start: Any) -> None:
    worker = await started(start, ACTORS)
    await worker.request_call("command:act", 21)
    first = await worker.recv()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(worker.recv(), 0.3)
    await worker.send({"op": "act_result", "id": first["id"], "ok": True})
    second = await worker.recv()
    assert second["action"] == "send"


async def test_a_refused_act_raises_plugin_action_error_in_the_handler(start: Any) -> None:
    worker = await started(start, ACTORS)
    await worker.request_call("command:refused", 22)
    act = await worker.recv()
    assert act["action"] == "send"
    await worker.send(
        {"op": "act_result", "id": act["id"], "ok": False, "error": "room not allowed"}
    )
    assert (await worker.recv())["reply"] == "caught: room not allowed"


async def test_an_unhandled_refused_act_is_a_hidden_crash(start: Any) -> None:
    worker = await started(start, ACTORS)
    await worker.request_call("command:unhandled", 23)
    act = await worker.recv()
    await worker.send({"op": "act_result", "id": act["id"], "ok": False})
    reply = await worker.recv()
    assert (reply["ok"], reply["user_visible"]) == (False, False)
    assert "PluginActionError" in reply["error"]
    assert (await worker.call("command:refused", rid=24)) is not None


async def test_act_result_may_arrive_for_one_call_while_another_runs(start: Any) -> None:
    worker = await started(start, ACTORS)
    await worker.request_call("command:act", 30)
    await worker.request_call("command:refused", 31)
    acts = {(await worker.recv())["call"] for _ in range(2)}
    assert acts == {30, 31}


async def test_an_oversized_act_is_refused_inside_the_handler(start: Any) -> None:
    worker = await started(
        start,
        """
        from sable.plugin_api import PluginActionError, command

        @command("big")
        async def big(ctx):
            try:
                await ctx.reply("x" * (2 * 1024 * 1024))
            except PluginActionError as exc:
                return f"refused: {exc}"
            return "sent?"
        """,
    )
    reply = await worker.call("command:big")
    assert reply["reply"] == "refused: the message is too large to send"


# --- concurrency, shutdown, EOF -------------------------------------------------------


async def test_calls_run_concurrently(start: Any) -> None:
    worker = await started(
        start,
        """
        import asyncio
        from sable.plugin_api import command

        gate = asyncio.Event()

        @command("wait")
        async def wait(ctx):
            await gate.wait()
            return "released"

        @command("open")
        async def open_(ctx):
            gate.set()
            return "opened"
        """,
    )
    await worker.request_call("command:wait", 1)
    await worker.request_call("command:open", 2)
    first, second = await worker.recv(), await worker.recv()
    # The second request answers first, and only then is the first released.
    assert (first["id"], first["reply"]) == (2, "opened")
    assert (second["id"], second["reply"]) == (1, "released")


async def test_a_slow_call_does_not_block_check_or_other_calls(start: Any) -> None:
    worker = await started(
        start,
        """
        import asyncio
        from sable.plugin_api import command

        def check(settings):
            return None

        @command("slow")
        async def slow(ctx):
            await asyncio.sleep(30)

        @command("fast")
        async def fast(ctx):
            return "fast"
        """,
    )
    await worker.request_call("command:slow", 1)
    assert (await worker.call("command:fast", rid=2))["reply"] == "fast"
    await worker.send({"op": "check", "id": 3})
    assert (await worker.recv())["id"] == 3


async def test_shutdown_exits_cleanly_even_with_a_call_in_flight(start: Any) -> None:
    worker = await started(
        start,
        """
        import asyncio
        from sable.plugin_api import command

        @command("slow")
        async def slow(ctx):
            await asyncio.sleep(30)
        """,
    )
    await worker.request_call("command:slow", 1)
    await worker.send({"op": "shutdown"})
    assert await asyncio.wait_for(worker.process.wait(), TIMEOUT) == 0
    assert "Traceback" not in await worker.stderr()


async def test_shutdown_before_load_exits(start: Any) -> None:
    worker = await start(GREETER)
    await worker.send({"op": "shutdown"})
    assert await asyncio.wait_for(worker.process.wait(), TIMEOUT) == 0


async def test_stdin_eof_exits_cleanly(start: Any) -> None:
    worker = await started(start, GREETER)
    assert await worker.finish() == 0
    assert "Traceback" not in await worker.stderr()


async def test_stdin_eof_with_a_call_in_flight_exits_cleanly(start: Any) -> None:
    worker = await started(start, ACTORS)
    await worker.request_call("command:act", 1)
    await worker.recv()  # the first act, left unanswered
    assert await worker.finish() == 0
    assert "Traceback" not in await worker.stderr()


async def test_an_oversized_request_line_ends_the_worker_with_a_status(start: Any) -> None:
    worker = await started(start, GREETER)
    # The worker may hang up before all of it has been written.
    with contextlib.suppress(ConnectionError):
        await worker.send_raw(b"x" * (5 * 1024 * 1024) + b"\n")
    assert await asyncio.wait_for(worker.process.wait(), TIMEOUT) == 2


# --- a call's lifetime ----------------------------------------------------------------


async def test_actions_after_the_handler_returned_fail_and_leftover_tasks_are_cancelled(
    start: Any,
) -> None:
    worker = await started(
        start,
        """
        import asyncio
        from sable.plugin_api import PluginActionError, command

        state = {}

        @command("leave")
        async def leave(ctx):
            async def later():
                await asyncio.sleep(0.05)
                await ctx.reply("far too late")

            async def spawner():
                await asyncio.sleep(0)
                state["grandchild"] = asyncio.create_task(later())
                await asyncio.sleep(30)

            state["ctx"] = ctx
            state["sleeper"] = asyncio.create_task(later())
            state["parent"] = asyncio.create_task(spawner())
            await asyncio.sleep(0.01)  # let the spawner create its own child
            state["forgotten"] = asyncio.create_task(ctx.reply("never even started"))
            return "left"

        @command("probe")
        async def probe(ctx):
            try:
                await state["ctx"].reply("x")
            except PluginActionError as exc:
                late = str(exc)
            else:
                late = "NOT REFUSED"
            for name in ("send", "react"):
                try:
                    await (
                        state["ctx"].send("room", "x") if name == "send"
                        else state["ctx"].react("x")
                    )
                except PluginActionError:
                    pass
                else:
                    late = "NOT REFUSED"
            await asyncio.sleep(0.1)  # a task that survived would have posted by now
            return "|".join([late] + [
                f"{key}={state[key].cancelled()}"
                for key in ("sleeper", "forgotten", "parent", "grandchild")
            ])
        """,
    )
    assert (await worker.call("command:leave", rid=1)) == {
        "op": "result",
        "id": 1,
        "ok": True,
        "reply": "left",
    }
    reply = await worker.call("command:probe", rid=2)
    # Had a leftover task survived, its act would arrive here ahead of this result.
    assert reply["op"] == "result", reply
    text = reply["reply"]
    assert "after the handler returned" in text
    assert text.endswith("|sleeper=True|forgotten=True|parent=True|grandchild=True"), text
    assert "NOT REFUSED" not in text


# --- hardening ------------------------------------------------------------------------


PEEK = """
    import json
    from sable.plugin_api import command

    @command("peek")
    async def peek(ctx):
        outcome = {}
        for label, pid in ctx.settings["targets"].items():
            for name in ("environ", "mem", "maps"):
                try:
                    with open(f"/proc/{pid}/{name}", "rb") as handle:
                        if name != "mem":  # mem would need a seek to a mapped address
                            handle.read(16)
                    outcome[f"{label}.{name}"] = "readable"
                except OSError as exc:
                    outcome[f"{label}.{name}"] = type(exc).__name__
        return json.dumps(outcome)
"""


async def test_a_worker_cannot_read_a_sibling_workers_memory_or_environment(start: Any) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read any process; the protection is for unprivileged workers")
    victim: Worker = await start(
        "from sable.plugin_api import command\n@command('x')\nasync def x(ctx):\n    return None\n"
    )
    assert (await victim.load({"secret": "hunter2"}))["op"] == "loaded"
    # The control: an ordinary process of the same user, which is exactly what a worker
    # would be without the protection. If even that cannot be read (ptrace restrictions,
    # a sandbox), the experiment proves nothing here.
    control = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", "import time; time.sleep(30)", env={"SECRET": "x"}
    )
    try:
        attacker = await started(
            start,
            PEEK,
            settings={"targets": {"victim": victim.process.pid, "control": control.pid}},
        )
        outcome = json.loads((await attacker.call("command:peek"))["reply"])
    finally:
        control.kill()
        await control.wait()
    if outcome["control.environ"] != "readable" or outcome["control.maps"] != "readable":
        pytest.skip(f"this environment does not let a process read a sibling at all: {outcome}")
    assert outcome["victim.environ"] == "PermissionError", outcome
    assert outcome["victim.mem"] == "PermissionError", outcome
    assert outcome["victim.maps"] == "PermissionError", outcome


# --- CPU allowance --------------------------------------------------------------------

BURN = """
    import time
    from sable.plugin_api import command

    @command("burn")
    async def burn(ctx):
        end = time.process_time() + float(ctx.args)
        while time.process_time() < end:
            pass
        return "burned"
"""


async def test_the_cpu_budget_is_per_call_not_for_the_lifetime(start: Any) -> None:
    # A 1 second budget (soft limit, no hard limit, as the core sets it), and a plugin that
    # in total uses well over that, a little at a time: every call must succeed.
    healthy: Worker = await start(BURN, cpu=(1, None))
    assert (await healthy.load())["op"] == "loaded"
    # One call that alone burns more than the budget is what the limit is for.
    runaway: Worker = await start(BURN, cpu=(1, None))
    assert (await runaway.load())["op"] == "loaded"

    async def many() -> None:
        for rid in range(5):
            reply = await healthy.call("command:burn", rid=rid, args="0.35")
            assert reply["reply"] == "burned", (rid, reply)

    async def one() -> None:
        await runaway.request_call("command:burn", 1, args="5")
        # No result ever comes: the kernel ends the worker with SIGXCPU.
        assert await asyncio.wait_for(runaway.process.wait(), 6 * TIMEOUT) == -signal.SIGXCPU

    await asyncio.gather(many(), one())
    assert healthy.process.returncode is None, "the healthy worker died"
