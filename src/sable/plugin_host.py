"""The worker process: imports one plugin and answers the core over stdin/stdout.

The core starts this as ``python -I -c <bootstrap> <entry path>`` and talks to
it in newline-delimited JSON (the protocol is specified with the core, in
``plugins.py``). Two properties matter more than any feature here:

* **stdout belongs to the protocol.** The first thing :func:`main` does is
  duplicate the real stdout and stdin file descriptors for the protocol, then
  point fd 1 and ``sys.stdout`` at stderr and fd 0 at ``/dev/null``. A stray
  ``print()``, a child process writing to inherited fd 1, or ``input()`` in a
  plugin can then neither corrupt a reply nor swallow a request.
* **The loop never dies from plugin code.** Import errors, bad declarations,
  exceptions, ``sys.exit()`` and garbage return values all become a reply the
  core can read. The traceback of an unexpected exception goes to stderr, where
  the core logs it.

Imports nothing from ``sable`` except :mod:`sable.plugin_api`.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import inspect
import json
import logging
import math
import os
import re
import sys
import time
import traceback
from collections.abc import Coroutine, Mapping, Sequence
from typing import Any, BinaryIO

from sable import plugin_api
from sable.plugin_api import Context, PluginActionError, PluginDeclarationError, PluginError

__all__ = ["main"]

log = logging.getLogger("sable.plugin_host")

#: The core treats a longer line from the worker as a protocol violation and
#: kills it, so the worker refuses to write one.
MAX_OUT_LINE = 1 << 20
#: Longest request line accepted. Settings are capped at 64 KiB by the core and
#: a chat message is far smaller; this only bounds what a broken core can make
#: the worker buffer.
MAX_IN_LINE = 4 << 20
#: Longest error text sent back for a crash (it is for the log, not the chat).
MAX_ERROR = 300
#: Longest load/check error. These are read by the operator, so they get room.
MAX_LOAD_ERROR = 2000

_TRIGGERS = ("command", "phrase", "schedule")
_STR_FIELDS = ("args", "room", "actor_id", "actor_name", "user_id", "text", "match")

#: prctl(2) option: whether other processes of the same user may read this one's
#: memory, environment and so on through /proc and ptrace.
PR_SET_DUMPABLE = 4


class _CallError(Exception):
    """A request the worker cannot act on; answered as a non-visible error."""


class _Out:
    """The protocol's side of stdout: one JSON object per line."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._closed = False

    def send(self, message: Mapping[str, Any]) -> None:
        """Write one message. ``ValueError`` if it cannot be sent as a legal line.

        A vanished core (broken pipe) is not an error here: stdin is closing too
        and the main loop ends on that.
        """
        line = json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(line) > MAX_OUT_LINE:
            raise ValueError("message too large")
        if self._closed:
            return
        try:
            self._stream.write(line)
            self._stream.flush()
        except OSError:
            self._closed = True


#: The call whose handler is running, so tasks it starts can be found and cancelled
#: when the call ends. Copied into every task the handler creates.
_ACTIVE_CALL: contextvars.ContextVar[_CallTransport | None] = contextvars.ContextVar(
    "sable_plugin_active_call", default=None
)


class _CallTransport:
    """One call's channel for actions, which closes when the handler returns."""

    def __init__(self, host: _Host, call_id: int) -> None:
        self._host = host
        self._call_id = call_id
        self._finished = False
        self._children: set[asyncio.Future[Any]] = set()

    async def act(self, action: str, args: Mapping[str, Any]) -> None:
        if self._finished:
            raise PluginActionError(
                f"cannot {action} after the handler returned: await every action before returning"
            )
        await self._host.act(self._call_id, action, dict(args))

    def adopt(self, task: asyncio.Future[Any]) -> None:
        """Remember a task the handler started (see :func:`_task_factory`)."""
        self._children.add(task)
        task.add_done_callback(self._children.discard)

    def finish(self) -> None:
        """The call is over: refuse further actions and stop what it left running."""
        self._finished = True
        current = asyncio.current_task()
        for task in list(self._children):
            if task is not current:
                task.cancel()
        self._children.clear()


def _task_factory(loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> asyncio.Future[Any]:
    """Create tasks as usual, but file those started inside a call under that call.

    ``asyncio`` has no parent/child relation between tasks; the call context
    variable, which a new task inherits, is what ties a fire-and-forget
    ``create_task`` in a handler back to the call so it can be cancelled with it.
    """
    task = asyncio.Task(coro, loop=loop, **kwargs)
    call = _ACTIVE_CALL.get()
    if call is not None:
        call.adopt(task)
    return task


class _CpuBudget:
    """Gives every call its own CPU allowance.

    The core starts the worker with ``RLIMIT_CPU`` as a budget for one call, but
    the kernel counts CPU time over the whole life of the process, so a busy
    plugin would eventually be killed with SIGXCPU however well it behaved. The
    soft limit is moved forward at the start of each load, check and call to
    "CPU used so far + the budget". With several calls in flight the newest start
    sets the deadline for all of them; the core's wall-clock timeout is the real
    limit, this is a backstop for a plugin spinning inside one call.

    Needs the hard limit to be high enough (the core sets it to unlimited): a
    process cannot raise its hard limit, so a finite one still ends the worker.
    """

    def __init__(self) -> None:
        self._budget: int | None = None
        self._hard = 0
        self._warned = False
        try:
            import resource

            soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
            if soft != resource.RLIM_INFINITY:
                self._budget = soft
                self._hard = hard
        except (ImportError, ValueError, OSError):
            pass

    def rearm(self) -> None:
        if self._budget is None:
            return
        import resource

        target = math.ceil(time.process_time()) + self._budget
        if self._hard != resource.RLIM_INFINITY:
            target = min(target, self._hard)
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (target, self._hard))
        except (ValueError, OSError) as exc:
            if not self._warned:  # once: this runs on every call
                self._warned = True
                log.warning("cannot re-arm the CPU limit: %s", exc)


class _Host:
    def __init__(self, out: _Out, cpu: _CpuBudget | None = None) -> None:
        self._out = out
        self._cpu = cpu or _CpuBudget()
        self._plugin = ""
        self._loaded = False
        self._settings: Mapping[str, Any] = {}
        self._handlers: dict[str, plugin_api.Handler] = {}
        self._check: Any = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[str, asyncio.Future[tuple[bool, str]]] = {}
        self._act_seq = 0

    # -- the request loop --------------------------------------------------------------

    async def serve(self, reader: asyncio.StreamReader) -> int:
        """Handle requests until shutdown or EOF; returns the exit status."""
        status = 0
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.error("request line longer than %d bytes: giving up", MAX_IN_LINE)
                    status = 2
                    break
                if not line:
                    break  # EOF: the core is gone or closed us down.
                try:
                    if not self._dispatch(line):
                        break
                except Exception:
                    log.exception("plugin host: unexpected failure handling a request")
        finally:
            await self._stop()
        return status

    def _dispatch(self, line: bytes) -> bool:
        """Route one request line. ``False`` means shut down."""
        if not line.strip():
            return True
        try:
            message = json.loads(line)
        except ValueError:
            log.error("ignoring a request that is not JSON")
            return True
        if not isinstance(message, dict):
            log.error("ignoring a request that is not an object")
            return True
        op = message.get("op")
        if op == "shutdown":
            return False
        if op == "load":
            self._on_load(message)
        elif op == "check":
            self._spawn(self._on_check(message))
        elif op == "call":
            self._spawn(self._on_call(message))
        elif op == "act_result":
            self._on_act_result(message)
        else:
            log.error("ignoring unknown op %r", str(op)[:40])
        return True

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        # Held in a set so the task is not garbage collected mid-call.
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- load --------------------------------------------------------------------------

    def _on_load(self, message: dict[str, Any]) -> None:
        rid = _request_id(message)
        if rid is None:
            return
        self._cpu.rearm()
        plugin, path, settings = message.get("plugin"), message.get("path"), message.get("settings")
        if self._loaded:
            self._load_failed(rid, "this worker has already loaded a plugin")
        elif not (isinstance(plugin, str) and isinstance(path, str) and isinstance(settings, dict)):
            self._load_failed(rid, "load needs a plugin name, a path and a settings object")
        else:
            try:
                declared = self._import(plugin, path, settings)
            except BaseException as exc:  # a plugin may raise anything, SystemExit included
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if not isinstance(exc, PluginDeclarationError | SyntaxError):
                    traceback.print_exc()
                self._load_failed(rid, _describe_load_error(exc, os.path.dirname(path)))
            else:
                self._loaded = True
                self._send({"op": "loaded", "id": rid, "declared": declared})

    def _load_failed(self, rid: int, error: str) -> None:
        self._send({"op": "error", "id": rid, "error": error[:MAX_LOAD_ERROR]})

    def _import(self, plugin: str, path: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Import the entry file and describe what it declared."""
        plugin_api.reset_declarations()
        directory = os.path.dirname(os.path.abspath(path))
        # First, so the plugin's own helper modules win over anything installed.
        sys.path.insert(0, directory)
        module_name = "sable_plugin_" + re.sub(r"\W", "_", plugin)
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import {path}: not a Python file")
        module = importlib.util.module_from_spec(spec)
        # Registered before running, like a normal import: dataclasses, pickle
        # and typing helpers look the module up by name while it executes.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            check = getattr(module, "check", None)
            if check is not None and not callable(check):
                raise PluginDeclarationError("'check' must be a function: check(settings)")
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        found = plugin_api.declarations()
        self._plugin = plugin
        self._settings = plugin_api.freeze(settings)
        self._check = check
        self._handlers = {f"command:{decl.name}": decl.handler for decl in found.commands}
        return {
            "commands": [
                {"name": d.name, "aliases": list(d.aliases), "help": d.help, "usage": d.usage}
                for d in found.commands
            ],
            # Steps 2 and 3 fill these; the keys are part of the wire format already.
            "phrases": [],
            "schedules": [],
            "has_check": check is not None,
        }

    # -- check -------------------------------------------------------------------------

    async def _on_check(self, message: dict[str, Any]) -> None:
        rid = _request_id(message)
        if rid is None:
            return
        if not self._loaded:
            self._result(rid, ok=False, error="no plugin is loaded")
            return
        self._cpu.rearm()
        if self._check is None:
            self._result(rid, ok=True)
            return
        try:
            verdict = self._check(self._settings)
            if inspect.isawaitable(verdict):
                verdict = await verdict
        except PluginError as exc:
            self._result(rid, ok=False, error=str(exc) or "check() rejected the settings")
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            traceback.print_exc()
            self._result(rid, ok=False, error=f"check() raised {_one_line(exc)}")
        else:
            if verdict is None:
                self._result(rid, ok=True)
            elif isinstance(verdict, str):
                self._result(rid, ok=False, error=verdict or "check() rejected the settings")
            else:
                self._result(
                    rid,
                    ok=False,
                    error=f"check() must return a string or None, not {type(verdict).__name__}",
                )

    # -- call --------------------------------------------------------------------------

    async def _on_call(self, message: dict[str, Any]) -> None:
        rid = _request_id(message)
        if rid is None:
            return
        self._cpu.rearm()
        transport = _CallTransport(self, rid)
        try:
            handler = self._handler(message)
            ctx = self._context(message, transport)
        except _CallError as exc:
            self._result(rid, ok=False, error=str(exc), user_visible=False)
            return
        _ACTIVE_CALL.set(transport)  # this task's own copy of the context
        try:
            value = await handler(ctx)
        except PluginError as exc:
            text = str(exc).strip()
            if text:
                self._result(rid, ok=False, error=text, user_visible=True)
            else:
                self._result(
                    rid, ok=False, error="PluginError without a message", user_visible=False
                )
        except BaseException as exc:  # SystemExit from a handler must not end the worker
            if isinstance(exc, asyncio.CancelledError):
                raise
            traceback.print_exc()
            self._result(rid, ok=False, error=_one_line(exc, MAX_ERROR), user_visible=False)
        else:
            if value is None or isinstance(value, str):
                self._result(rid, ok=True, reply=value)
            else:
                self._result(
                    rid,
                    ok=False,
                    error=f"handler returned {type(value).__name__}, expected str or None",
                    user_visible=False,
                )
        finally:
            # No await sits between the result and this, so nothing the handler left
            # behind can post an action for a call the core has already closed.
            transport.finish()

    def _handler(self, message: dict[str, Any]) -> plugin_api.Handler:
        if not self._loaded:
            raise _CallError("no plugin is loaded")
        handler = message.get("handler")
        found = self._handlers.get(handler) if isinstance(handler, str) else None
        if found is None:
            raise _CallError(f"unknown handler {str(handler)[:60]!r}")
        return found

    def _context(self, message: dict[str, Any], transport: _CallTransport) -> Context:
        wire = message.get("ctx")
        if not isinstance(wire, dict):
            raise _CallError("call has no ctx object")
        trigger = wire.get("trigger")
        name = wire.get("name")
        if trigger not in _TRIGGERS or not isinstance(name, str):
            raise _CallError("ctx needs a valid trigger and a name")
        strings = {key: wire.get(key, "") for key in _STR_FIELDS}
        for key, value in strings.items():
            if not isinstance(value, str):
                raise _CallError(f"ctx.{key} must be a string")
        argv = wire.get("argv", [])
        if not isinstance(argv, list) or not all(isinstance(word, str) for word in argv):
            raise _CallError("ctx.argv must be a list of strings")
        is_admin = wire.get("is_admin", False)
        message_id = wire.get("message_id", 0)
        if not isinstance(is_admin, bool):
            raise _CallError("ctx.is_admin must be a boolean")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise _CallError("ctx.message_id must be an integer")
        return Context(
            plugin=self._plugin,
            trigger=trigger,
            name=name,
            argv=list(argv),
            is_admin=is_admin,
            message_id=message_id,
            # The loaded settings, not a per-call copy: they cannot change under a handler.
            settings=self._settings,
            log=logging.getLogger(f"sable.plugin.{self._plugin}"),
            _transport=transport,
            **strings,
        )

    # -- actions -----------------------------------------------------------------------

    async def act(self, call_id: int, action: str, args: dict[str, Any]) -> None:
        """Ask the core to do something and wait for its verdict."""
        self._act_seq += 1
        act_id = f"a{self._act_seq}"
        future: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        self._pending[act_id] = future
        try:
            try:
                self._send(
                    {"op": "act", "id": act_id, "call": call_id, "action": action, "args": args}
                )
            except ValueError:
                raise PluginActionError("the message is too large to send") from None
            ok, error = await future
        finally:
            self._pending.pop(act_id, None)
        if not ok:
            raise PluginActionError(error or f"{action} was refused")

    def _on_act_result(self, message: dict[str, Any]) -> None:
        act_id = message.get("id")
        future = self._pending.get(act_id) if isinstance(act_id, str) else None
        if future is None or future.done():
            log.error("ignoring an act_result for an unknown action")
            return
        error = message.get("error")
        future.set_result((message.get("ok") is True, error if isinstance(error, str) else ""))

    # -- replies -----------------------------------------------------------------------

    def _result(self, rid: int, *, ok: bool, **fields: Any) -> None:
        message: dict[str, Any] = {"op": "result", "id": rid, "ok": ok, **fields}
        try:
            self._send(message)
        except ValueError:
            # Too large (or not serialisable): say so rather than leave the core waiting.
            self._send(
                {
                    "op": "result",
                    "id": rid,
                    "ok": False,
                    "error": "the result was too large to send",
                    "user_visible": False,
                }
            )

    def _send(self, message: Mapping[str, Any]) -> None:
        self._out.send(message)


# --- helpers --------------------------------------------------------------------------


def _request_id(message: Mapping[str, Any]) -> int | None:
    rid = message.get("id")
    if isinstance(rid, int) and not isinstance(rid, bool):
        return rid
    log.error("ignoring a %r request without an integer id", str(message.get("op"))[:20])
    return None


def _one_line(exc: BaseException, limit: int = MAX_LOAD_ERROR) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return " ".join(text.split())[:limit]


def _describe_load_error(exc: BaseException, directory: str) -> str:
    """A message an operator can act on: what failed and where in the plugin."""
    root = os.path.abspath(directory)
    if isinstance(exc, PluginDeclarationError):
        what = f"invalid declaration: {exc}"
    elif isinstance(exc, SyntaxError):
        name = os.path.relpath(exc.filename, root) if exc.filename else "the plugin"
        return f"syntax error in {name} line {exc.lineno}: {exc.msg}"
    else:
        what = f"import failed: {_one_line(exc)}"
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        inside = os.path.abspath(frame.filename)
        if inside.startswith(root + os.sep):
            return f"{what} (at {os.path.relpath(inside, root)}:{frame.lineno})"
    return what


# --- process entry --------------------------------------------------------------------


async def _open_reader(fd: int) -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_IN_LINE, loop=loop)
    pipe = os.fdopen(fd, "rb", buffering=0)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader, loop=loop), pipe)
    return reader


async def _serve(out: _Out, stdin_fd: int, cpu: _CpuBudget) -> int:
    asyncio.get_running_loop().set_task_factory(_task_factory)
    return await _Host(out, cpu).serve(await _open_reader(stdin_fd))


def _harden() -> None:
    """Make this process a poor target for its siblings. Best effort, never fatal.

    All workers run as the same user, and that user may read another process's
    ``/proc/<pid>/mem``, ``environ`` and ``maps`` unless the process is marked
    non-dumpable. Without this, one plugin could read another's settings (an API
    key, say) out of its memory. ``exec`` resets the flag to dumpable, so it has
    to be set here, by the worker itself, before any plugin code is imported.
    """
    # The interpreter's own import system, too: nothing the plugin imports gets a
    # .pyc written next to it. PYTHONDONTWRITEBYTECODE does nothing under -I.
    sys.dont_write_bytecode = True
    # First, while /proc/self is still ours to write: after the prctl below it
    # belongs to root. If the host ever has to be sacrificed, it goes first.
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write("1000")
    except OSError:
        pass
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE) failed")
    except (OSError, AttributeError) as exc:
        log.warning("cannot mark the worker non-dumpable: %s", exc)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the worker until the core says shutdown or closes our stdin.

    ``argv`` is accepted for the bootstrap's sake and ignored: the plugin's path
    arrives in the ``load`` request.
    """
    # Before anything else can print: take private copies of the protocol's
    # descriptors, then give fd 0 and fd 1 to something harmless.
    stdin_fd = os.dup(0)
    out = _Out(os.fdopen(os.dup(1), "wb"))
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    # Before anything of the plugin's is imported. The CPU allowance is read now,
    # while it is still what the core set at spawn.
    _harden()
    cpu = _CpuBudget()
    status = asyncio.run(_serve(out, stdin_fd, cpu))
    if status:
        raise SystemExit(status)
