"""Plugins: finding them, checking them, running them, and deciding who may.

A plugin is a Python file (``<name>.py``) with a settings file next to it
(``<name>_settings.yaml``). It runs in a **worker process** of its own, never in
this one: the core starts it with a scrubbed environment and resource limits,
talks to it in newline-delimited JSON over its stdin and stdout, and treats every
byte it sends back as untrusted. This module is the core's half of that
conversation; :mod:`sable.plugin_host` is the worker's, and :mod:`sable.plugin_api`
is what a plugin author imports.

Layers, from the bottom up:

* **Discovery** (:func:`discover`) walks the plugins directory without ever
  following a symlink and without executing anything.
* **Settings** (:class:`PluginSettings`) are parsed with ``yaml.safe_load`` and
  validated against a schema that forbids unknown keys.
* **Worker** (:class:`Worker`) owns one subprocess: spawning, the wire protocol,
  the per-call timeout, protocol-violation handling, lazy restart and the
  circuit breaker.
* **PluginManager** ties them together: validation at startup, the access
  decision (:meth:`PluginManager.allows`), the limits on what a plugin may do to
  the chat, the registry entries and the ``!plugins`` report.

Wire protocol (core to worker, one JSON object per line)::

    {"op":"load","id":1,"plugin":"weather","path":"...","settings":{...}}
    {"op":"check","id":2}
    {"op":"call","id":3,"handler":"command:weather","ctx":{...}}
    {"op":"act_result","id":"a1","ok":true}
    {"op":"shutdown"}

and back::

    {"op":"loaded","id":1,"declared":{...}}   {"op":"error","id":1,"error":"..."}
    {"op":"result","id":3,"ok":true,"reply":"text or null"}
    {"op":"act","id":"a1","call":3,"action":"reply|send|react","args":{...}}
"""

from __future__ import annotations

import ast
import asyncio
import collections
import contextlib
import ctypes
import enum
import itertools
import json
import logging
import math
import os
import re
import signal
import stat
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from .commands import Access, Command, CommandError, Context, Registry
from .config import TOKEN_HINT, TOKEN_RE, Config
from .events import CONTROL_RE, REACTION_LIMIT, TalkEvent
from .mentions import defang_mentions
from .plugin_api import (
    DEFAULT_COOLDOWN,
    ID_RE,
    MAX_COOLDOWN,
    MAX_PHRASES,
    PluginDeclarationError,
    check_breadth,
    check_phrases,
    fold,
)

__all__ = [
    "Access",
    "CallOutcome",
    "PhraseHit",
    "PhraseMatcher",
    "PluginFailure",
    "PluginManager",
    "PluginRecord",
    "PluginSettings",
    "PluginStartupError",
    "Status",
    "Worker",
    "bootstrap_source",
    "discover",
    "worker_environment",
]

log = logging.getLogger(__name__)

_T = TypeVar("_T")

# --------------------------------------------------------------------------- #
# Limits. All of them are constants so that the documentation can state them.
# --------------------------------------------------------------------------- #

#: A plugin's name is the stem of its files, lowercased. ``\Z`` and not ``$``, as
#: with TOKEN_RE: the name ends up in log lines and chat.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}\Z")

#: Largest entry file, settings file and number of plugins taken from a directory.
MAX_ENTRY_BYTES = 256 * 1024
MAX_SETTINGS_BYTES = 64 * 1024
MAX_PLUGINS = 64
#: Settings files looked at in one scan, valid or not, so that a directory full of
#: rubbish cannot make startup read it all. The first ones in path order are taken.
MAX_PLUGIN_FILES = 256
#: Bounds on how much of the directory is even looked at, so a hostile or broken
#: mount cannot make startup wander.
MAX_DIRECTORIES = 256
MAX_FILES_PER_DIRECTORY = 256
#: The settings mapping handed to a plugin: how deep, and how many values.
MAX_SETTINGS_DEPTH = 16
MAX_SETTINGS_NODES = 10_000
MAX_ACCESS_ENTRIES = 256
#: How deeply the settings file may nest brackets or dashes, and how many brackets
#: it may hold. PyYAML takes a minute over 30,000 nested ``[``: this is checked in
#: one pass over the text before it is given to the parser.
MAX_YAML_DEPTH = 100
MAX_YAML_BRACKETS = 10_000
#: Entries read from one directory before the rest are ignored (names, sorted).
MAX_DIRECTORY_SCAN = 100_000

#: Worker resource limits, set inside the worker before it imports anything. For
#: memory, files and core dumps soft and hard are equal, so the plugin cannot raise
#: them. The wall-clock timeout is the real limit on a call.
RLIMIT_AS_BYTES = 1 << 30
RLIMIT_NOFILE_COUNT = 64
RLIMIT_CORE_BYTES = 0
#: The CPU budget of ONE call, as a multiple of the call timeout: a backstop for a
#: plugin that spins in a way the wall clock cannot interrupt. It is the soft limit
#: only. The worker re-arms it at the start of every load, check and call, which
#: needs the hard limit left unlimited: a finite hard limit would still end the
#: worker after that much CPU over its whole life.
RLIMIT_CPU_FACTOR = 10

#: A line from the worker longer than this is a protocol violation.
MAX_LINE_BYTES = 1 << 20
#: Concurrent calls in flight per plugin.
MAX_IN_FLIGHT = 4
#: What one call may do: actions (replies, sends, reactions) and characters of text.
MAX_ACTIONS_PER_CALL = 10
MAX_CHARS_PER_CALL = 20_000
#: More action messages than this in one call is a flood, and a violation.
MAX_ACT_MESSAGES_PER_CALL = MAX_ACTIONS_PER_CALL * 10
#: Phrase handlers that may fire for one message, round-robined across plugins so
#: that no one plugin's handlers can starve another's (see PluginManager.phrase_hits).
MAX_PHRASE_FIRES_PER_MESSAGE = 3
#: Cooldowns remembered at once. Past this, expired ones go first, then whichever
#: remaining entry is soonest to expire (never by insertion order: a long cooldown
#: claimed early must not be evicted ahead of a short one claimed later).
MAX_COOLDOWN_ENTRIES = 10_000
#: Only the leading characters of a message are ever folded and searched for
#: phrases. Unicode NFKC normalisation of a long run of combining marks over one
#: base character is quadratic in CPython - a crafted ~32,000 character message
#: (one base character plus thousands of alternating combining marks) can stall
#: normalisation for over a second. Capping the input before normalising, not
#: after, keeps the worst case under ~10ms; ASCII text (where NFKC is always the
#: identity) skips normalisation entirely and is matched in full. Talk's own limit
#: is 32,000 characters.
MAX_MATCH_TEXT = 4_000
#: Restarts allowed in the window before a plugin is switched off.
MAX_RESTARTS = 3
RESTART_WINDOW = 300.0
#: Once switched off, a plugin is let try again after this long: one trial call,
#: which closes the breaker if it works and opens it for another cooldown if not.
BREAKER_COOLDOWN = 300.0
#: Seconds a worker may take to import its plugin (and to answer a check).
LOAD_TIMEOUT = 10.0
#: Seconds a worker is given to exit after being asked to, before it is killed.
SHUTDOWN_GRACE = 2.0
#: What a worker's stderr may put in the log: line length, and lines per window.
STDERR_LINE_CAP = 1000
STDERR_LINES_PER_WINDOW = 200
STDERR_WINDOW = 10.0
#: Handlers of every kind in one plugin, and the lengths of what they carry.
MAX_HANDLERS = 32
MAX_HELP = 200
MAX_USAGE = 100
#: How long an error text from a worker may be, wherever it is shown.
MAX_ERROR_TEXT = 500
MAX_VISIBLE_TEXT = 2000

#: The PATH a worker gets. Fixed, never inherited.
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"

#: The package directory, so a worker can import ``sable.plugin_host``.
PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


class PluginFailure(RuntimeError):  # noqa: N818 - read as "the plugin failed", not an exception class
    """A plugin could not do what was asked. The text is fit for the chat."""


class PluginStartupError(RuntimeError):
    """SABLE_PLUGINS_STRICT is on and a plugin failed to load."""


class Status(enum.Enum):
    """Where a plugin stands. ``restarting`` is derived, see :attr:`PluginRecord.state`."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    DISABLED = "disabled"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _present(value: _T | None) -> _T:
    """``value``, which the caller knows is set; a bug if it is not."""
    if value is None:
        raise RuntimeError("an internal invariant of the plugin code does not hold")
    return value


def _safe(text: str, limit: int = 80) -> str:
    """Text from a file name or a worker, flattened to something safe to print."""
    return ascii(text)[1:-1][:limit]


def _one_line(text: str, limit: int = MAX_ERROR_TEXT) -> str:
    return " ".join(CONTROL_RE.sub(" ", text).split())[:limit]


def _encode_safe(text: str) -> str:
    """``text``, with anything that cannot be encoded as UTF-8 (most often a lone
    surrogate) replaced, so that printing or logging it can never raise.

    A backstop, not the defence: a phrase that holds a surrogate is refused at
    declaration time (see ``check_phrases`` in ``plugin_api.py``). This only keeps a
    plugin that somehow has one anyway (an old declaration, a validation this does
    not yet cover) from being able to crash ``!plugins <name>`` rather than merely
    display oddly.
    """
    return text.encode("utf-8", "replace").decode("utf-8")


def _visible_text(text: str) -> str:
    """Text a plugin wants said in the chat: control characters out, newlines kept."""
    kept = "".join(" " if CONTROL_RE.match(ch) and ch != "\n" else ch for ch in text)
    return kept.strip()[:MAX_VISIBLE_TEXT]


def _exit_description(code: int | None) -> str:
    """How a worker's exit status reads: a signal by name, otherwise the status."""
    if code is None:
        return "it did not exit"
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"it was killed by {name}"
    return f"it exited with status {code}"


def _open_pidfd(pid: int) -> int | None:
    """A file descriptor that becomes readable when the process exits (Linux 5.3+)."""
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        return None
    try:
        return int(opener(pid))
    except OSError:
        return None


async def _wait_for_exit(proc: asyncio.subprocess.Process, pidfd: int | None) -> int | None:
    """Wait until a process has exited, without waiting for its pipes to close.

    With a pidfd that is event driven; without one it polls a few times a second.
    Returns the exit status once asyncio has collected it, or None if that does not
    happen.
    """
    if pidfd is not None:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()

        def ready() -> None:
            if not done.done():
                done.set_result(None)

        loop.add_reader(pidfd, ready)
        try:
            await done
        finally:
            loop.remove_reader(pidfd)
            os.close(pidfd)
    else:
        while proc.returncode is None:  # noqa: ASYNC110 - no event to wait on without a pidfd
            await asyncio.sleep(0.1)
    for _ in range(100):
        if proc.returncode is not None:
            return proc.returncode
        await asyncio.sleep(0.02)
    return None


def _secret_values(settings: object) -> list[str]:
    """Every string in a settings mapping that could be a secret: four characters or more.

    Longest first, so that one value containing another is blanked whole.
    """
    found: set[str] = set()
    stack = [settings]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            if len(value.strip()) >= 4:
                found.add(value)
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, dict):
            stack.extend(value.values())
    return sorted(found, key=len, reverse=True)


def _yaml_prescan(text: str) -> str | None:
    """Reject nesting that would make PyYAML slow before it is asked to parse.

    One linear pass: unclosed ``[`` and ``{`` in a row, and ``- - - -`` chains at the
    start of a line. Strings that merely contain brackets count too, which is
    harmless below a hundred of them in a row.
    """
    depth = peak = total = 0
    for char in text:
        if char in "[{":
            depth += 1
            total += 1
            peak = max(peak, depth)
        elif char in "]}" and depth:
            depth -= 1
    if peak > MAX_YAML_DEPTH:
        return f"it nests brackets more than {MAX_YAML_DEPTH} levels deep"
    if total > MAX_YAML_BRACKETS:
        return f"it holds more than {MAX_YAML_BRACKETS} brackets"
    for line in text.splitlines():
        index = len(line) - len(line.lstrip())
        dashes = 0
        while line.startswith(("- ", "? "), index):
            dashes += 1
            index += 2
            if dashes > MAX_YAML_DEPTH:
                return f"it nests list items more than {MAX_YAML_DEPTH} levels deep"
    return None


def _pydantic_problems(exc: ValidationError) -> str:
    """What is wrong, by field, and never the offending input: settings hold secrets."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'the file'}: "
        + error["msg"].removeprefix("Value error, ")
        for error in exc.errors(include_input=False, include_url=False, include_context=False)
    )


def disable_process_inspection() -> str:
    """Make this process non-dumpable, so a same-user worker cannot read its memory.

    ``prctl(PR_SET_DUMPABLE, 0)`` makes ``/proc/<pid>/environ`` and ``/proc/<pid>/mem``
    unreadable to every other process that does not hold CAP_SYS_PTRACE, including
    one running as the same user: which is what a plugin is. Linux only, and best
    effort: returns the reason it could not, or an empty string.
    """
    if not sys.platform.startswith("linux"):
        return "it is only implemented on Linux"
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            return os.strerror(ctypes.get_errno())
        if libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
            return "the kernel kept the process dumpable"
    except (OSError, AttributeError) as exc:
        return str(exc)
    return ""


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def _json_problem(root: object) -> str | None:
    """Why this mapping cannot be handed to a worker as JSON, or None if it can."""
    stack: list[tuple[str, object, int]] = [("settings", root, 0)]
    nodes = 0
    while stack:
        path, value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_SETTINGS_NODES:
            return f"settings hold more than {MAX_SETTINGS_NODES} values"
        if depth > MAX_SETTINGS_DEPTH:
            return f"{path}: nested more than {MAX_SETTINGS_DEPTH} levels deep"
        if value is None or isinstance(value, bool | int | str):
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                return f"{path}: not a finite number"
            continue
        if isinstance(value, list):
            stack.extend((f"{path}[{i}]", item, depth + 1) for i, item in enumerate(value))
        elif isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    return f"{path}: keys must be text, not {type(key).__name__}"
                stack.append((f"{path}.{key}", item, depth + 1))
        else:
            return (
                f"{path}: a {type(value).__name__} is not allowed here - settings are plain "
                "JSON values (write dates and the like as quoted text)"
            )
    return None


class AccessSettings(BaseModel):
    """Who may trigger a plugin, and where."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Conversation tokens, or ``["*"]`` for every conversation the account
    #: follows. Empty: the plugin is inactive, validated and never run.
    rooms: list[str] = Field(default_factory=list, max_length=MAX_ACCESS_ENTRIES)
    #: Nextcloud user ids. Empty: anyone in those rooms.
    users: list[str] = Field(default_factory=list, max_length=MAX_ACCESS_ENTRIES)
    admins_only: bool = False

    @field_validator("rooms", "users", mode="before")
    @classmethod
    def _quote_hint(cls, entries: object) -> object:
        """YAML reads 12345678 as a number and yes as a boolean: say how to write them.

        A bare ``rooms:`` or ``users:`` is an empty list."""
        if entries is None:
            return []
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, bool):
                    word = "yes" if entry else "no"
                    raise ValueError(
                        f"{entry} is a YAML boolean, not text: if you meant the word, quote it "
                        f'(for example "{word}")'
                    )
                if isinstance(entry, int | float):
                    raise ValueError(
                        f"{entry} is a number, not text: quote it, it is a string - "
                        f'write "{entry}" (conversation tokens and user ids are always text)'
                    )
        return entries

    @field_validator("rooms")
    @classmethod
    def _rooms_are_tokens(cls, rooms: list[str]) -> list[str]:
        for room in rooms:
            if room != "*" and not TOKEN_RE.match(room):
                shown = _safe(room, 40)
                raise ValueError(
                    f"{shown!r} is not a conversation token: {TOKEN_HINT}. Quote it in the "
                    "YAML file, so that a token made of digits stays text"
                )
        return list(dict.fromkeys(rooms))

    @field_validator("users")
    @classmethod
    def _users_are_ids(cls, users: list[str]) -> list[str]:
        cleaned = [user.strip() for user in users]
        if any(not user or len(user) > 128 for user in cleaned):
            raise ValueError("user ids must be non-empty text of at most 128 characters")
        return list(dict.fromkeys(cleaned))


class PluginSettings(BaseModel):
    """The contents of ``<name>_settings.yaml``. Unknown keys are an error."""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = True
    access: AccessSettings = Field(default_factory=AccessSettings)
    #: Free-form, handed to the plugin read-only. Never shown by ``!plugins``.
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("access", "settings", mode="before")
    @classmethod
    def _a_mapping_or_nothing(cls, value: object) -> object:
        """A bare ``access:`` or ``settings:`` is empty, not an error; anything that is
        not a mapping says so in the file's words rather than the schema's."""
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("it must be a mapping (indented key: value lines)")
        return value

    @field_validator("settings")
    @classmethod
    def _settings_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        problem = _json_problem(value)
        if problem:
            raise ValueError(problem)
        return value


class SettingsError(ValueError):
    """The settings file is unusable. The text never quotes the file's values."""


def _read_regular_file(path: Path, cap: int) -> bytes:
    """Read a regular file of at most ``cap`` bytes, never following a symlink.

    Opened with O_NOFOLLOW and O_NONBLOCK and checked after opening, so that a file
    swapped for a symlink or a FIFO between discovery and now is refused rather
    than followed or waited on.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SettingsError(f"cannot open it ({exc.strerror or type(exc).__name__})") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SettingsError("it is not a regular file")
        if info.st_size > cap:
            raise SettingsError(f"it is {info.st_size} bytes, more than the {cap} allowed")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise SettingsError(f"it is more than {cap} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def load_settings(path: Path) -> PluginSettings:
    """Parse and validate a settings file. Raises :class:`SettingsError`."""
    try:
        text = _read_regular_file(path, MAX_SETTINGS_BYTES).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsError("the settings file is not UTF-8 text") from exc
    except SettingsError as exc:
        raise SettingsError(f"the settings file: {exc}") from exc
    if problem := _yaml_prescan(text):
        raise SettingsError(f"the settings file is not usable: {problem}")
    try:
        # Aliases and anchors are refused rather than expanded: a few kilobytes of
        # them expand into gigabytes, and nothing here needs them.
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise SettingsError("YAML aliases (&anchor, *alias) are not allowed")
        raw = yaml.safe_load(text)
    except SettingsError:
        raise
    except yaml.YAMLError as exc:
        # Not str(exc): that quotes the offending lines of the file.
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = _one_line(str(getattr(exc, "problem", "") or "unreadable"), 120)
        raise SettingsError(f"invalid YAML{where}: {problem}") from exc
    except RecursionError as exc:
        raise SettingsError("the settings are nested too deeply") from exc
    except (ValueError, OverflowError, TypeError, MemoryError):
        # The constructors raise these for a 5,000 digit integer, a date that does
        # not exist (2025-02-30) or a time zone offset of +99:99. Their messages are
        # about the value, so none of it is repeated.
        raise SettingsError(
            "a number, date or time in the file is out of range or cannot exist"
        ) from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SettingsError("the settings file must hold a mapping (enabled, access, settings)")
    try:
        return PluginSettings.model_validate(raw)
    except ValidationError as exc:
        raise SettingsError(_pydantic_problems(exc)) from exc


def check_syntax(path: Path) -> str | None:
    """Parse the entry file without running it. The reason it is not Python, or None."""
    try:
        data = _read_regular_file(path, MAX_ENTRY_BYTES)
    except SettingsError as exc:
        return f"the plugin file: {exc}"
    try:
        ast.parse(data, filename=path.name)
    except SyntaxError as exc:
        return f"syntax error in {_safe(path.name)} line {exc.lineno}: {_one_line(exc.msg, 120)}"
    except (ValueError, RecursionError, MemoryError) as exc:
        return (
            f"{_safe(path.name)} cannot be parsed: {_one_line(str(exc), 120) or type(exc).__name__}"
        )
    return None


# --------------------------------------------------------------------------- #
# What a plugin declares
# --------------------------------------------------------------------------- #


def _tidy(text: str) -> str:
    return " ".join(CONTROL_RE.sub(" ", text).split())


class DeclaredCommand(BaseModel):
    """A command a plugin says it handles. Re-checked here: the worker is untrusted."""

    model_config = ConfigDict(extra="ignore")

    name: str
    aliases: list[str] = Field(default_factory=list, max_length=MAX_HANDLERS)
    help: str = Field(default="", max_length=MAX_HELP)
    usage: str = Field(default="", max_length=MAX_USAGE)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError(f"{_safe(value, 40)!r} is not a legal command name")
        return value

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, value: list[str]) -> list[str]:
        for alias in value:
            if not NAME_RE.match(alias):
                raise ValueError(f"{_safe(alias, 40)!r} is not a legal command name")
        return value

    @field_validator("help", "usage")
    @classmethod
    def _text(cls, value: str) -> str:
        return _tidy(value)


class DeclaredPhrase(BaseModel):
    """A handler for messages containing some phrases. Re-checked here, by the same
    rules as the author's decorator: the worker is untrusted."""

    model_config = ConfigDict(extra="ignore")

    id: str
    any: list[str] = Field(min_length=1, max_length=MAX_PHRASES)
    whole_words: StrictBool = True
    cooldown: StrictInt = Field(default=DEFAULT_COOLDOWN, ge=0, le=MAX_COOLDOWN)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not ID_RE.fullmatch(value):
            raise ValueError(f"{_safe(value, 40)!r} is not a legal handler id")
        return value

    @field_validator("any")
    @classmethod
    def _phrases(cls, value: list[str]) -> list[str]:
        return list(check_phrases(value))

    @model_validator(mode="after")
    def _not_too_broad(self) -> DeclaredPhrase:
        try:
            check_breadth(self.any, self.whole_words)
        except PluginDeclarationError as exc:
            raise ValueError(str(exc)) from exc
        return self


class DeclaredSchedule(BaseModel):
    """Step 3. Parsed so the wire format is already settled; nothing acts on it yet."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(max_length=64)
    cron: str | None = Field(default=None, max_length=100)
    every: str | None = Field(default=None, max_length=100)


class Declaration(BaseModel):
    """Everything a plugin declared, as the core accepts it."""

    model_config = ConfigDict(extra="ignore")

    commands: list[DeclaredCommand] = Field(default_factory=list, max_length=MAX_HANDLERS)
    phrases: list[DeclaredPhrase] = Field(default_factory=list, max_length=MAX_HANDLERS)
    schedules: list[DeclaredSchedule] = Field(default_factory=list, max_length=MAX_HANDLERS)
    has_check: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Declaration:
        if len(self.commands) + len(self.phrases) + len(self.schedules) > MAX_HANDLERS:
            raise ValueError(f"more than {MAX_HANDLERS} handlers")
        if not (self.commands or self.phrases or self.schedules):
            raise ValueError("it declares no commands, phrases or schedules, so it can do nothing")
        names: set[str] = set()
        for command in self.commands:
            for name in (command.name, *command.aliases):
                if name in names:
                    raise ValueError(f"the command name {name!r} is declared twice")
                names.add(name)
        ids: set[str] = set()
        for ident in [p.id for p in self.phrases] + [s.id for s in self.schedules]:
            if not ident:
                raise ValueError("a handler has no id")
            if ident in ids:
                raise ValueError(f"the handler id {ident!r} is declared twice")
            ids.add(ident)
        return self

    @property
    def command_names(self) -> list[str]:
        return [name for command in self.commands for name in (command.name, *command.aliases)]


def parse_declaration(raw: object) -> Declaration:
    """Validate what the worker said it declared. Raises :class:`PluginFailure`."""
    if not isinstance(raw, dict):
        raise PluginFailure("it did not say what it declares")
    try:
        return Declaration.model_validate(raw)
    except ValidationError as exc:
        raise PluginFailure(f"invalid declaration: {_pydantic_problems(exc)}") from exc


# --------------------------------------------------------------------------- #
# Phrases
# --------------------------------------------------------------------------- #

_WORD = re.compile(r"\w")


class PhraseMatcher:
    """Finds a literal phrase in a message. Never a pattern a plugin wrote.

    Both sides are NFKC-normalised and casefolded (:func:`sable.plugin_api.fold`).
    With ``whole_words`` a phrase must not be touched by a word character on a side
    where its own edge is a word character: ``gm`` is in "gm!" and "say gm" and not in
    "gmail", while ``:)`` or ``c++`` have non-word edges and match anywhere (the ``+``
    side). Without it, any substring matches.

    One compiled expression per phrase that needs a boundary, built from the escaped
    literal and lookarounds only, so nothing in it can backtrack. A plain substring test
    goes first: it is linear in the message, and it is what keeps a long phrase from
    being tried at every position of a long message that does not even contain it. (One
    alternation of all the phrases measured at 60-70 ms on a 32,000 character message of
    near misses; this is under 5.)
    """

    def __init__(self, phrases: Sequence[str], whole_words: bool) -> None:
        self._items: list[tuple[str, str, re.Pattern[str] | None]] = []
        for declared in phrases:
            folded = fold(declared).strip()
            if not folded:
                continue
            pattern = None
            if whole_words:
                before = r"(?<!\w)" if _WORD.match(folded[0]) else ""
                after = r"(?!\w)" if _WORD.match(folded[-1]) else ""
                if before or after:
                    pattern = re.compile(before + re.escape(folded) + after)
            self._items.append((declared, folded, pattern))

    def match(self, folded_text: str) -> str | None:
        """The first declared phrase found in ``folded_text`` (already folded), or None."""
        for declared, folded, pattern in self._items:
            if folded not in folded_text:
                continue
            if pattern is None or pattern.search(folded_text):
                return declared
        return None


#: The attribute :func:`_folded_for_matching` caches its result under, on the event
#: itself. A name unlikely to collide with anything Talk or the rest of sable uses.
_FOLD_CACHE_ATTR = "_sable_phrase_fold"


def _folded_for_matching(text: str) -> str:
    """``text``, ready to search for phrases: casefolded, and NFKC-normalised unless
    it is already plain ASCII (where NFKC is always the identity, so normalising
    would only cost time for nothing). Capped to :data:`MAX_MATCH_TEXT` BEFORE
    normalising non-ASCII text, not after: the cost that needs capping is inside
    normalisation itself, and slicing the result first would not avoid it.
    """
    if text.isascii():
        return text.casefold()
    return fold(text[:MAX_MATCH_TEXT])


def _folded_message(event: TalkEvent) -> str:
    """The event's message, folded for phrase matching - computed once and cached on
    the event itself, because both ``would_handle`` and ``handle`` classify the same
    event (see ``Bot._route``) and folding is the expensive part worth not doing
    twice. ``TalkEvent`` is a frozen dataclass; ``object.__setattr__`` is how a cache
    is attached to one without changing its declared, compared and hashed fields.
    """
    cached = getattr(event, _FOLD_CACHE_ATTR, None)
    if isinstance(cached, str):
        return cached
    folded = _folded_for_matching(event.message)
    object.__setattr__(event, _FOLD_CACHE_ATTR, folded)
    return folded


@dataclass(frozen=True)
class PhraseHit:
    """A phrase handler a message would fire."""

    plugin: str
    handler: str
    #: The declared phrase that matched.
    phrase: str
    #: The handler's cooldown in seconds.
    cooldown: int
    room: str


@dataclass
class _PhraseEntry:
    plugin: str
    handler: str
    matcher: PhraseMatcher
    cooldown: int


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


@dataclass
class PluginRecord:
    """One plugin, from the moment it is found to the moment it is closed."""

    #: The validated slug, or empty when the file name is not a legal plugin name.
    name: str
    #: Where it lives, relative to the plugins directory and safe to print.
    label: str
    entry: Path | None = None
    settings_path: Path | None = None
    status: Status = Status.INACTIVE
    reason: str = ""
    config: PluginSettings | None = None
    declared: Declaration | None = None
    worker: Worker | None = None
    #: The last thing that went wrong while it ran. For ``!plugins`` and the log.
    last_error: str = ""
    #: Things worth saying that did not stop it loading.
    warnings: list[str] = field(default_factory=list)
    #: The strings in its settings, longest first: blanked out of every message
    #: that carries text the plugin wrote, since a plugin may quote its own key.
    secrets: list[str] = field(default_factory=list, repr=False)
    #: True for the later of two plugins with one name. It is listed and says why it
    #: failed, but the name belongs to the first: nothing looks the duplicate up.
    duplicate: bool = False

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    @property
    def effective_status(self) -> Status:
        """The status, with a plugin whose breaker is open counted as failed."""
        if self.status is Status.ACTIVE and self.worker is not None and self.worker.tripped:
            return Status.FAILED
        return self.status

    @property
    def display(self) -> str:
        if self.duplicate:
            return f"{self.name} ({self.label})"
        if self.name:
            return self.name
        if self.label.startswith("("):
            return self.label
        return f"(invalid name) {self.label}"

    @property
    def state(self) -> str:
        """The status as ``!plugins`` words it."""
        if self.status is Status.ACTIVE:
            worker = self.worker
            if worker is not None and worker.tripped:
                return worker.tripped_text()
            return "restarting" if worker is not None and worker.needs_restart else "active"
        if self.status is Status.INACTIVE:
            return f"inactive: {self.reason or 'no rooms set'}"
        if self.status is Status.FAILED:
            return f"failed: {self.reason}"
        return "disabled"

    def fail(self, reason: str) -> None:
        self.status = Status.FAILED
        self.reason = _one_line(self.redact(reason))
        self.last_error = self.reason

    @property
    def rooms(self) -> list[str]:
        return list(self.config.access.rooms) if self.config else []


def _entries(path: Path, limit: int) -> tuple[list[os.DirEntry[str]], bool]:
    """The first ``limit`` entries in name order, and whether there were more.

    Sorted before they are cut, so that which ones are looked at never depends on
    the order the file system happens to list them in.
    """
    with os.scandir(path) as scan:
        found = sorted(itertools.islice(scan, MAX_DIRECTORY_SCAN), key=lambda e: e.name)
    return found[:limit], len(found) > limit


#: A file that looks like a settings file without being one: ``x_settings.yml``,
#: ``x_setting.yaml``, ``X_Settings.yaml``. Said out loud, since the plugin it was
#: meant for silently does not exist otherwise.
NEAR_MISS_RE = re.compile(r"^.+_settings?\.ya?ml$", re.IGNORECASE)


def discover(root: Path) -> tuple[list[PluginRecord], list[str]]:
    """Find the plugins under ``root``, in sorted path order.

    Returns the records (a plugin that is malformed is a record with status
    FAILED, so that ``!plugins`` can say why) and notes about things skipped.
    Reads names and sizes only; no file is opened and nothing is executed.

    Rules: only immediate subdirectories are scanned; names starting with ``.`` or
    ``_`` and every symlink are skipped; a plugin is a ``<stem>_settings.yaml``
    together with ``<stem>.py``; the name is the lowercased stem; the first of two
    plugins with one name (in sorted order) keeps it.
    """
    records: list[PluginRecord] = []
    notes: list[str] = []
    owners: dict[str, str] = {}
    unread = 0
    root = root.resolve()
    try:
        directories, more = _entries(root, MAX_DIRECTORIES)
    except OSError as exc:
        return [], [f"cannot read the plugins directory: {exc.strerror or exc}"]
    if more:
        notes.append(
            f"more than {MAX_DIRECTORIES} entries in the plugins directory: only the first "
            f"{MAX_DIRECTORIES}, in name order, are looked at"
        )

    for directory in directories:
        if directory.name.startswith((".", "_")):
            continue
        if directory.is_symlink():
            notes.append(f"skipped {_safe(directory.name)}: it is a symlink, and none is followed")
            continue
        if not directory.is_dir(follow_symlinks=False):
            continue
        try:
            files, more = _entries(Path(directory.path), MAX_FILES_PER_DIRECTORY)
        except OSError as exc:
            notes.append(f"cannot read {_safe(directory.name)}: {exc.strerror or exc}")
            continue
        if more:
            notes.append(
                f"more than {MAX_FILES_PER_DIRECTORY} files in {_safe(directory.name)}: only the "
                f"first {MAX_FILES_PER_DIRECTORY}, in name order, are looked at"
            )
        by_name = {entry.name: entry for entry in files}
        for entry in files:
            if not entry.name.endswith("_settings.yaml"):
                if NEAR_MISS_RE.match(entry.name):
                    notes.append(
                        f"{_safe(directory.name)}/{_safe(entry.name)} looks like a settings file "
                        "but is not called <name>_settings.yaml (that exact spelling, lowercase), "
                        "so it is ignored"
                    )
                continue
            stem = entry.name.removesuffix("_settings.yaml")
            label = _safe(f"{directory.name}/{stem}.py")
            if len(records) >= MAX_PLUGIN_FILES:
                unread += 1
                continue
            record = PluginRecord(name="", label=label, settings_path=Path(entry.path))
            records.append(record)
            name = stem.lower()
            if not NAME_RE.match(name):
                record.fail(
                    f"{_safe(stem, 40)!r} is not a legal plugin name: use lowercase letters, "
                    "digits, - and _, starting with a letter, at most 32 characters"
                )
                continue
            record.name = name
            if name in owners:
                # The first, in path order, keeps the name; this one never shadows it.
                record.duplicate = True
                record.fail(f"the name {name!r} is already used by {owners[name]}")
                continue
            owners[name] = label
            if problem := _structure_problem(entry, by_name.get(f"{stem}.py"), stem):
                record.fail(problem)
                continue
            record.entry = Path(directory.path) / f"{stem}.py"
    if unread:
        overflow = PluginRecord(name="", label=f"(and {unread} more settings files)")
        overflow.fail(
            f"not looked at: sable reads at most {MAX_PLUGIN_FILES} plugin settings files, in "
            f"path order, and found {unread} more after them"
        )
        records.append(overflow)
    return records, notes


def _structure_problem(
    settings: os.DirEntry[str], entry: os.DirEntry[str] | None, stem: str
) -> str | None:
    """What is wrong with a settings file and the entry file it should pair with."""
    for what, found, cap in (
        ("settings file", settings, MAX_SETTINGS_BYTES),
        ("plugin file", entry, MAX_ENTRY_BYTES),
    ):
        if found is None:
            return f"{_safe(stem, 40)}_settings.yaml has no {_safe(stem, 40)}.py next to it"
        if found.is_symlink():
            return f"the {what} is a symlink, and symlinks are never followed"
        try:
            info = found.stat(follow_symlinks=False)
        except OSError as exc:
            return f"the {what} cannot be read: {exc.strerror or exc}"
        if not stat.S_ISREG(info.st_mode):
            return f"the {what} is not a regular file"
        if info.st_size > cap:
            return f"the {what} is {info.st_size} bytes, more than the {cap} allowed"
    return None


# --------------------------------------------------------------------------- #
# The worker process
# --------------------------------------------------------------------------- #


def worker_environment(
    timezone: str = "", parent: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment a worker is given: built from nothing, never inherited.

    A fixed PATH, a UTF-8 locale, the time zone, and the certificate locations if
    the parent has them (an internal CA must be trusted by plugins too). Nothing
    else: in particular no ``SABLE_*``, which holds the account's password.
    """
    parent = os.environ if parent is None else parent
    env = {
        "PATH": SAFE_PATH,
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "TZ": timezone or "UTC",
    }
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        if parent.get(key):
            env[key] = parent[key]
    return env


def bootstrap_source(
    package_root: str, cpu_seconds: int, host_module: str = "sable.plugin_host"
) -> str:
    """The ``-c`` program a worker starts with.

    Sets the resource limits before anything else is imported (not through
    ``preexec_fn``, which is unsafe in a threaded process), puts the package on
    the path, and hands over to the host. ``python -I`` has already ignored every
    ``PYTHON*`` variable, the user site and the working directory.
    """
    limits = [
        ("RLIMIT_AS", RLIMIT_AS_BYTES, RLIMIT_AS_BYTES),
        ("RLIMIT_NOFILE", RLIMIT_NOFILE_COUNT, RLIMIT_NOFILE_COUNT),
        ("RLIMIT_CORE", RLIMIT_CORE_BYTES, RLIMIT_CORE_BYTES),
        # No hard limit of its own (None): see RLIMIT_CPU_FACTOR.
        ("RLIMIT_CPU", cpu_seconds, None),
    ]
    return (
        "import resource, sys\n"
        f"for _name, _soft, _hard in {limits!r}:\n"
        "    try:\n"
        "        _which = getattr(resource, _name)\n"
        "        if _hard is None:\n"
        "            try:\n"
        "                resource.setrlimit(_which, (_soft, resource.RLIM_INFINITY))\n"
        "                continue\n"
        "            except (ValueError, OSError):\n"
        "                _hard = resource.getrlimit(_which)[1]\n"
        "        resource.setrlimit(_which, (_soft, _hard))\n"
        "    except (AttributeError, ValueError, OSError) as _error:\n"
        "        sys.stderr.write('could not set ' + _name + ': ' + str(_error) + chr(10))\n"
        f"sys.path.insert(0, {package_root!r})\n"
        f"from {host_module} import main\n"
        "main()\n"
    )


#: Builds the command line of a worker: the plugin and the CPU limit in seconds.
CommandFactory = Callable[[PluginRecord, int], Sequence[str]]


def default_command(record: PluginRecord, cpu_seconds: int) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-c",
        bootstrap_source(PACKAGE_ROOT, cpu_seconds),
        str(record.entry),
    ]


#: Posts one action a worker asked for. Returns the reason it refused, or None.
ActSink = Callable[[str, dict[str, Any]], Awaitable["str | None"]]


@dataclass(frozen=True)
class CallOutcome:
    """What a worker said about one call."""

    ok: bool
    reply: str | None = None
    error: str = ""
    user_visible: bool = False


@dataclass
class _CallState:
    sink: ActSink
    queue: asyncio.Queue[tuple[Any, str, dict[str, Any]] | None] = field(
        default_factory=asyncio.Queue
    )
    runner: asyncio.Task[None] | None = None
    seen: int = 0


@dataclass
class _Pending:
    future: asyncio.Future[dict[str, Any]]
    accepts: frozenset[str]
    call: _CallState | None = None


class _Session:
    """One worker process, from spawn to death."""

    def __init__(self, proc: asyncio.subprocess.Process, first_id: int) -> None:
        self.proc = proc
        self.pgid = proc.pid
        #: The first request id issued to this process: an id from here up to the last
        #: one issued is one that was asked, and may have finished since.
        self.first_id = first_id
        self.pending: dict[int, _Pending] = {}
        self.calls: dict[int, _CallState] = {}
        self.dead = False
        self.closing = False
        self.death = ""
        #: Messages dropped because they answered a call that was already over.
        self.late = 0
        self.exited = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.stdout_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.watch_task: asyncio.Task[None] | None = None
        self.stderr_window_start = 0.0
        self.stderr_lines = 0
        self.stderr_suppressed = False

    @property
    def tasks(self) -> list[asyncio.Task[None]]:
        return [t for t in (self.stdout_task, self.stderr_task, self.watch_task) if t is not None]


class Worker:
    """The core's end of one plugin's worker process.

    Starts the process, speaks the protocol, enforces the per-call timeout, and
    restarts it lazily after it died. Everything it reads is untrusted: a line
    that is not valid protocol kills the worker, and counts as a crash.

    The circuit breaker: more than three restarts in five minutes switch the plugin
    off. After a cooldown one trial call is let through; if it works the breaker
    closes and the history is forgotten, if it fails the plugin is off for another
    cooldown. So a plugin cannot be kept off for good by whoever can crash it.

    ``command`` and ``clock`` are injectable so tests can run a fake worker and
    move time without sleeping.
    """

    def __init__(
        self,
        record: PluginRecord,
        *,
        timeout: float,
        load_timeout: float = LOAD_TIMEOUT,
        command: CommandFactory = default_command,
        clock: Callable[[], float] = time.monotonic,
        timezone: str = "",
        quiet: bool = False,
    ) -> None:
        self.record = record
        self._timeout = timeout
        self._load_timeout = load_timeout
        self._command = command
        self._clock = clock
        self._timezone = timezone
        #: Log what the worker writes to stderr at DEBUG, not INFO (``--check``).
        self._quiet = quiet
        self._session: _Session | None = None
        self._lifecycle = asyncio.Lock()
        self._slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._last_id = 0
        self._restarts: collections.deque[float] = collections.deque()
        self._settings: dict[str, Any] = {}
        self._declaration: Declaration | None = None
        self._closed = False
        #: Set when the breaker opens: the time after which one trial call may go.
        self._tripped_until: float | None = None
        self._trial = False
        #: Why the plugin is off for good (it changed what it declares).
        self._broken = ""
        #: Restarts so far, for ``!plugins``.
        self.restart_count = 0

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def tripped(self) -> bool:
        """Is the plugin switched off, whether or not it is time to try it again?"""
        return self._tripped_until is not None or bool(self._broken)

    @property
    def blocked(self) -> bool:
        """Is a call to this plugin refused right now?"""
        if self._broken:
            return True
        return self._tripped_until is not None and self._clock() < self._tripped_until

    def tripped_text(self) -> str:
        if self._tripped_until is None:
            return "switched off"
        left = self._tripped_until - self._clock()
        if left <= 0:
            return "switched off, retrying on the next use"
        return f"switched off, retrying in {math.ceil(left / 60)} min"

    @property
    def needs_restart(self) -> bool:
        """Is the process gone, so that the next call starts a new one?"""
        session = self._session
        return not self.tripped and not self._closed and (session is None or session.dead)

    def _next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def _text(self, text: str, limit: int = MAX_ERROR_TEXT) -> str:
        """Text the worker wrote, on one line and with the plugin's own settings blanked."""
        return _one_line(self.record.redact(text), limit)

    # -- starting ----------------------------------------------------------- #

    async def start(self, settings: dict[str, Any], *, check: bool = True) -> Declaration:
        """Spawn the process, load the plugin and (once) run its ``check``.

        Raises :class:`PluginFailure` with the reason, in words for the operator.
        """
        self._settings = settings
        async with self._lifecycle:
            declaration = await self._open()
        self._declaration = declaration
        if check and declaration.has_check:
            session = _present(self._session)
            reply = await self._request(session, {"op": "check"}, frozenset({"result"}))
            if reply.get("ok") is not True:
                error = reply.get("error")
                text = self._text(error) if isinstance(error, str) else ""
                raise PluginFailure(f"its check rejected the settings: {text or 'no reason given'}")
        return declaration

    async def _open(self) -> Declaration:
        """Spawn a process and run the ``load`` handshake. Caller holds the lifecycle lock."""
        name = self.name
        entry = _present(self.record.entry)
        cpu = max(1, math.ceil(self._timeout * RLIMIT_CPU_FACTOR))
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command(self.record, cpu),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=worker_environment(self._timezone),
                cwd=str(entry.parent),
                # Its own session and process group: the whole group is killed on
                # a timeout or at shutdown, children and all.
                start_new_session=True,
                limit=MAX_LINE_BYTES,
            )
        except OSError as exc:
            raise PluginFailure(f"the worker process could not be started: {exc.strerror}") from exc
        session = _Session(proc, self._last_id + 1)
        self._session = session
        session.stdout_task = asyncio.create_task(
            self._read_stdout(session), name=f"plugin-{name}-stdout"
        )
        session.stderr_task = asyncio.create_task(
            self._read_stderr(session), name=f"plugin-{name}-stderr"
        )
        session.watch_task = asyncio.create_task(
            self._watch(session, _open_pidfd(proc.pid)), name=f"plugin-{name}-exit"
        )
        if self._closed:
            # Shutdown began while the process was being spawned: nobody else knows
            # about it yet, so it is ours to end.
            self._declare_dead(session, "sable is shutting down")
            await self._join(session)
            raise PluginFailure("sable is shutting down")
        message = {
            "op": "load",
            "plugin": name,
            "path": str(entry),
            "settings": self._settings,
        }
        try:
            reply = await self._request(session, message, frozenset({"loaded", "error"}))
            if reply.get("op") == "error":
                error = reply.get("error")
                text = self._text(error) if isinstance(error, str) else ""
                raise PluginFailure(text or "the import failed")
            return parse_declaration(reply.get("declared"))
        except PluginFailure:
            self._declare_dead(session, "it could not be loaded")
            raise

    async def _ready(self) -> tuple[_Session, bool]:
        """A running session, restarting the process if it died; and whether this call
        is the one trial let through a switched-off plugin."""
        async with self._lifecycle:
            if self._closed:
                raise PluginFailure("sable is shutting down")
            if self._broken:
                raise PluginFailure(f"the `{self.name}` plugin is switched off")
            trial = False
            now = self._clock()
            if self._tripped_until is not None:
                if now < self._tripped_until or self._trial:
                    raise PluginFailure(f"the `{self.name}` plugin is switched off after crashing")
                trial = True
                self._trial = True
            session = self._session
            if session is not None and not session.dead and not trial:
                return session, False
            if not trial:
                while self._restarts and now - self._restarts[0] > RESTART_WINDOW:
                    self._restarts.popleft()
                if len(self._restarts) >= MAX_RESTARTS:
                    self._trip(
                        f"crashed or timed out more than {MAX_RESTARTS} times in "
                        f"{int(RESTART_WINDOW // 60)} minutes"
                    )
                    raise PluginFailure(f"the `{self.name}` plugin is switched off after crashing")
                self._restarts.append(now)
            self.restart_count += 1
            log.info("restarting plugin %s (restart %d)", self.name, self.restart_count)
            try:
                declaration = await self._open()
                self._same_as_before(declaration)
            except PluginFailure as exc:
                if self._closed:
                    raise
                self.record.last_error = self._text(str(exc))
                log.warning("plugin %s could not be restarted: %s", self.name, exc)
                if trial:
                    self._trial = False
                    self._trip("the trial restart failed")
                raise PluginFailure(f"the `{self.name}` plugin could not be restarted") from exc
            return _present(self._session), trial

    def _same_as_before(self, declaration: Declaration) -> None:
        """A restart that declares something else is a different plugin: fail it, visibly."""
        before = self._declaration
        if before is None or declaration.model_dump() == before.model_dump():
            return
        reason = "it declares something different after a restart, so it is switched off"
        self._broken = reason
        self.record.fail(reason)
        if self._session is not None:
            self._declare_dead(self._session, reason)
        raise PluginFailure(reason)

    def _trip(self, reason: str) -> None:
        """Open the breaker: no calls until the cooldown has passed."""
        self._tripped_until = self._clock() + BREAKER_COOLDOWN
        self.record.last_error = f"{reason}; switched off for {int(BREAKER_COOLDOWN // 60)} minutes"
        log.error("plugin %s: %s", self.name, self.record.last_error)

    def _close_breaker(self) -> None:
        """The trial call worked: the plugin is whole again, and its past is forgiven."""
        self._tripped_until = None
        self._restarts.clear()
        self.record.last_error = ""
        log.info("plugin %s works again; switched back on", self.name)

    # -- the wire ------------------------------------------------------------ #

    def _failure(self, session: _Session, message: str | None = None) -> PluginFailure:
        if message is None:
            if session.closing or self._closed:
                message = "sable is shutting down"
            else:
                message = f"the `{self.name}` plugin crashed"
        return PluginFailure(message)

    async def _send(self, session: _Session, message: Mapping[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        stdin = session.proc.stdin
        if session.dead or stdin is None:
            raise self._failure(session)
        try:
            async with session.write_lock:
                stdin.write(line)
                await stdin.drain()
        except (ConnectionError, OSError, RuntimeError):
            self._declare_dead(session, "its input pipe closed")
            raise self._failure(session) from None

    async def _request(
        self,
        session: _Session,
        message: dict[str, Any],
        accepts: frozenset[str],
    ) -> dict[str, Any]:
        """Send a handshake message and wait for its answer, within the load timeout."""
        rid = self._next_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        session.pending[rid] = _Pending(future, accepts | {"error"})
        try:
            async with asyncio.timeout(self._load_timeout):
                await self._send(session, {**message, "id": rid})
                return await future
        except TimeoutError:
            self._declare_dead(session, "no answer in time")
            raise PluginFailure(
                f"it did not answer within {self._load_timeout:g} seconds (while loading)"
            ) from None
        except PluginFailure as exc:
            if session.closing or self._closed:
                raise PluginFailure("sable is shutting down") from exc
            reason = session.death or "it stopped"
            raise PluginFailure(f"the worker process stopped while loading: {reason}") from exc
        finally:
            session.pending.pop(rid, None)

    async def _watch(self, session: _Session, pidfd: int | None) -> None:
        """Notice the worker exiting at once, whoever else still holds its pipes open.

        A child it left behind (in a session of its own, out of reach of the group
        kill) keeps stdout open, and the reader would wait for it. ``Process.wait``
        would too: asyncio finishes a subprocess only when its pipes have closed, so
        this watches the process itself.
        """
        code = await _wait_for_exit(session.proc, pidfd)
        session.exited.set()
        description = _exit_description(code)
        if not session.dead and not session.closing:
            self.record.last_error = f"the worker process died: {description}"
            log.warning("plugin %s: the worker process died: %s", self.name, description)
        self._declare_dead(session, description)

    async def _read_stdout(self, session: _Session) -> None:
        stream = _present(session.proc.stdout)
        try:
            while True:
                try:
                    line = await stream.readline()
                except ValueError:
                    self._violation(session, f"a line longer than {MAX_LINE_BYTES} bytes")
                    return
                if not line:
                    break
                problem = self._handle_line(session, line)
                if problem:
                    self._violation(session, problem)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reading from plugin %s failed", self.name)
        if not session.dead:
            # It closed its output. It should be exiting; if it is not, end it.
            try:
                await asyncio.wait_for(session.exited.wait(), 5.0)
            except TimeoutError:
                self._declare_dead(session, "it closed its output but kept running")

    def _handle_line(self, session: _Session, line: bytes) -> str | None:
        """One line from the worker. Returns why it is a violation, or None."""
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            return "a line that is not JSON"
        if not isinstance(message, dict):
            return "a line that is not a JSON object"
        op = message.get("op")
        if op == "act":
            return self._on_act(session, message)
        if op not in ("loaded", "result", "error"):
            return "an unknown op"
        rid = message.get("id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            return f"a {op} without an id"
        pending = session.pending.get(rid)
        if pending is None:
            if session.first_id <= rid <= self._last_id:
                # Asked here, and over: its call timed out or was cancelled.
                return self._late(session, f"a {op}")
            return f"a {op} for a request nobody made"
        if op not in pending.accepts:
            return f"a {op} where it should have answered otherwise"
        if op == "result" and not isinstance(message.get("ok"), bool):
            return "a result without ok"
        session.pending.pop(rid, None)
        if not pending.future.done():
            pending.future.set_result(message)
        if pending.call is not None:
            session.calls.pop(rid, None)
            pending.call.queue.put_nowait(None)
        return None

    def _late(self, session: _Session, what: str) -> str | None:
        """Drop a message that answers a call already over. Not a violation: a handler that
        fires off ``ctx.reply`` without awaiting it and returns does exactly this. A
        flood of them is one, though."""
        session.late += 1
        if session.late > MAX_ACT_MESSAGES_PER_CALL:
            return "a flood of messages for calls that are over"
        if session.late <= 3:
            log.warning(
                "plugin %s: dropped %s that arrived after its call had finished", self.name, what
            )
        return None

    def _on_act(self, session: _Session, message: dict[str, Any]) -> str | None:
        act_id, call, action, args = (message.get(k) for k in ("id", "call", "action", "args"))
        if not isinstance(act_id, str | int) or isinstance(act_id, bool) or len(str(act_id)) > 64:
            return "an action without a usable id"
        if not isinstance(call, int) or isinstance(call, bool):
            return "an action for no call"
        if not isinstance(action, str) or not isinstance(args, dict):
            return "an action that is not well formed"
        state = session.calls.get(call)
        if state is None:
            if session.first_id <= call <= self._last_id:
                return self._late(session, "an action")
            return "an action for a call nobody made"
        state.seen += 1
        if state.seen > MAX_ACT_MESSAGES_PER_CALL:
            return "a flood of actions"
        state.queue.put_nowait((act_id, action, args))
        return None

    async def _run_acts(self, session: _Session, state: _CallState) -> None:
        """Perform a call's actions one at a time, in order, answering each."""
        while (item := await state.queue.get()) is not None:
            act_id, action, args = item
            try:
                error = await state.sink(action, args)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("plugin %s: performing %r failed", self.name, action[:20])
                error = "that failed inside sable"
            answer: dict[str, Any] = {"op": "act_result", "id": act_id, "ok": error is None}
            if error is not None:
                answer["error"] = _one_line(error, 200)
            try:
                await self._send(session, answer)
            except PluginFailure:
                return

    async def _read_stderr(self, session: _Session) -> None:
        """Log what the worker writes to stderr, line by line, capped and rate-limited."""
        stream = _present(session.proc.stderr)
        partial = b""
        try:
            while chunk := await stream.read(65536):
                if self._stderr_over_budget(session):
                    # Over the cap for this window: do not even split the chunk. A
                    # worker writing nothing but newlines would otherwise cost a log
                    # call per byte.
                    partial = b""
                    continue
                partial += chunk
                *lines, partial = partial.split(b"\n")
                partial = partial[: STDERR_LINE_CAP * 4]
                for line in lines:
                    self._log_stderr(session, line)
            if partial and not self._stderr_over_budget(session):
                self._log_stderr(session, partial)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reading stderr of plugin %s failed", self.name)

    def _stderr_over_budget(self, session: _Session) -> bool:
        now = self._clock()
        if now - session.stderr_window_start >= STDERR_WINDOW:
            session.stderr_window_start = now
            session.stderr_lines = 0
            session.stderr_suppressed = False
        if session.stderr_lines < STDERR_LINES_PER_WINDOW:
            return False
        if not session.stderr_suppressed:
            session.stderr_suppressed = True
            log.log(
                logging.DEBUG if self._quiet else logging.INFO,
                "plugin %s: too much output on stderr; the rest is dropped",
                self.name,
            )
        return True

    def _log_stderr(self, session: _Session, raw: bytes) -> None:
        if self._stderr_over_budget(session):
            return
        session.stderr_lines += 1
        text = self._text(raw.decode("utf-8", "replace"), STDERR_LINE_CAP)
        if text:
            log.log(
                logging.DEBUG if self._quiet else logging.INFO, "plugin %s: %s", self.name, text
            )

    # -- ending -------------------------------------------------------------- #

    def _violation(self, session: _Session, reason: str) -> None:
        if session.dead:
            return
        message = f"protocol violation: {reason}"
        self.record.last_error = message
        log.warning("plugin %s: %s; the worker is stopped", self.name, message)
        self._declare_dead(session, message)

    def _kill_group(self, session: _Session) -> None:
        # Everything in the worker's process group, not only the worker: a plugin
        # may have started children.
        if hasattr(os, "killpg"):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(session.pgid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            session.proc.kill()

    def _declare_dead(self, session: _Session, reason: str, message: str | None = None) -> None:
        """Kill the process group, and fail everything waiting on it. Idempotent.

        ``message`` is what whoever was waiting is told; by default that the plugin
        crashed, or that sable is shutting down if it is.
        """
        if session.dead:
            return
        session.dead = True
        session.death = reason
        self._kill_group(session)
        for pending in list(session.pending.values()):
            if not pending.future.done():
                pending.future.set_exception(self._failure(session, message))
                pending.future.exception()  # retrieved: nobody may be left waiting on it
        for state in list(session.calls.values()):
            state.queue.put_nowait(None)
        # Stop reading. A descendant that escaped the group kill may still hold the
        # pipes; stderr gets a moment to deliver a last traceback.
        current = asyncio.current_task()
        if session.stdout_task is not None and session.stdout_task is not current:
            session.stdout_task.cancel()
        if session.stderr_task is not None and session.stderr_task is not current:
            asyncio.get_running_loop().call_later(2.0, session.stderr_task.cancel)

    async def _join(self, session: _Session) -> None:
        """Wait for a dead session's tasks, so nothing outlives the worker."""
        tasks = session.tasks
        if not tasks:
            return
        _, still = await asyncio.wait(tasks, timeout=5.0)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    async def aclose(self) -> None:
        """Ask the worker to shut down, then make sure it has, group and all.

        Bounded: a worker that has stopped reading its input (a full pipe) cannot make
        this wait. Calls still in flight are told that sable is shutting down.
        """
        self._closed = True
        session = self._session
        if session is None:
            return
        if not session.dead:
            session.closing = True
            with contextlib.suppress(TimeoutError, PluginFailure):
                async with asyncio.timeout(SHUTDOWN_GRACE):
                    await self._send(session, {"op": "shutdown"})
                    await session.exited.wait()
            self._declare_dead(session, "shut down")
        await self._join(session)

    # -- calls --------------------------------------------------------------- #

    async def call(self, handler: str, ctx: dict[str, Any], sink: ActSink) -> CallOutcome:
        """Run one handler in the worker and return what it said.

        At most four calls are in flight per plugin; the others wait. A call that
        outlives the timeout kills the worker (the next call starts a new one) and
        raises :class:`PluginFailure`; the other calls that were in flight are told
        the worker was restarted, and the whole incident counts once.
        """
        async with self._slots:
            session, trial = await self._ready()
            worked = False
            try:
                outcome = await self._call_on(session, handler, ctx, sink)
                worked = True
                return outcome
            finally:
                if trial:
                    self._trial = False
                    if worked:
                        self._close_breaker()
                    else:
                        self._trip("the trial call failed")

    async def _call_on(
        self, session: _Session, handler: str, ctx: dict[str, Any], sink: ActSink
    ) -> CallOutcome:
        rid = self._next_id()
        state = _CallState(sink)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        session.pending[rid] = _Pending(future, frozenset({"result", "error"}), state)
        session.calls[rid] = state
        state.runner = asyncio.create_task(self._run_acts(session, state))
        try:
            async with asyncio.timeout(self._timeout):
                await self._send(session, {"op": "call", "id": rid, "handler": handler, "ctx": ctx})
                reply = await future
                await state.runner
        except TimeoutError:
            self.record.last_error = f"a call took longer than {self._timeout:g} seconds"
            log.warning("plugin %s: %s; the worker is killed", self.name, self.record.last_error)
            self._declare_dead(
                session,
                "a call timed out",
                f"the `{self.name}` plugin's worker was restarted (another call took too long)",
            )
            raise PluginFailure(f"the `{self.name}` plugin took too long") from None
        finally:
            session.pending.pop(rid, None)
            session.calls.pop(rid, None)
            if state.runner is not None and not state.runner.done():
                state.runner.cancel()
                await asyncio.gather(state.runner, return_exceptions=True)
        error = reply.get("error")
        if reply.get("op") == "error":
            return CallOutcome(False, error=self._text(error) if isinstance(error, str) else "")
        text = reply.get("reply")
        visible = reply.get("user_visible") is True
        if isinstance(error, str):
            # What a plugin chose to say to the chat is its own; anything else is
            # for the log and the report, and may quote a setting.
            error = _visible_text(error) if visible else self._text(error)
        return CallOutcome(
            ok=reply["ok"],
            reply=text if isinstance(text, str) else None,
            error=error if isinstance(error, str) else "",
            user_visible=visible,
        )


# --------------------------------------------------------------------------- #
# What a plugin may do to the chat
# --------------------------------------------------------------------------- #


class ChatPort(Protocol):
    """What :class:`PluginManager` needs from the bot to act on a plugin's behalf.

    ``Bot`` is one. Each method returns whether the message or reaction was
    actually posted, and never raises for a Talk failure.
    """

    async def plugin_reply(self, event: TalkEvent, text: str, *, silent: bool) -> bool: ...

    async def plugin_send(self, room: str, text: str, *, silent: bool) -> bool: ...

    async def plugin_react(self, event: TalkEvent, emoji: str) -> bool: ...


class _Budget:
    """What one call may still do: ten actions, twenty thousand characters."""

    def __init__(self) -> None:
        self.actions = 0
        self.chars = 0

    def take(self, text: str | None = None) -> str | None:
        """Spend an action (and the text's characters). None means the budget is gone.

        Text over what is left is cut to fit rather than refused: part of an answer
        beats none, and the cap exists for the flood, not for the sentence.
        """
        if self.actions >= MAX_ACTIONS_PER_CALL:
            return None
        if text is None:
            self.actions += 1
            return ""
        left = MAX_CHARS_PER_CALL - self.chars
        if left <= 0:
            return None
        self.actions += 1
        text = text[:left]
        self.chars += len(text)
        return text


class _ChatSink:
    """Carries out a worker's actions for one call, and refuses what is not allowed."""

    def __init__(self, record: PluginRecord, port: ChatPort, event: TalkEvent, config: Config):
        self.record = record
        self.port = port
        self.event = event
        self.config = config
        self.budget = _Budget()

    def may_post_to(self, room: str) -> bool:
        rooms = self.record.rooms
        return (
            TOKEN_RE.match(room) is not None
            and ("*" in rooms or room in rooms)
            and self.config.room_allowed(room)
        )

    async def __call__(self, action: str, args: dict[str, Any]) -> str | None:
        if action == "react":
            emoji = args.get("emoji")
            if not isinstance(emoji, str) or not emoji.strip() or len(emoji) > REACTION_LIMIT:
                return "react needs an emoji"
            if self.budget.take() is None:
                return "this call has used up its actions"
            reacted = await self.port.plugin_react(self.event, emoji.strip())
            return None if reacted else "the reaction could not be added"
        if action in ("reply", "send"):
            return await self._post(action, args)
        return f"unknown action {_safe(action, 20)!r}"

    async def _post(self, action: str, args: dict[str, Any]) -> str | None:
        text, silent = args.get("text"), args.get("silent", False)
        if not isinstance(text, str) or not text.strip():
            return None  # nothing to say is not an error
        if not isinstance(silent, bool):
            return "silent must be true or false"
        room = self.event.room_token
        if action == "send":
            target = args.get("room")
            if not isinstance(target, str) or not self.may_post_to(target):
                return "this plugin may not post to that conversation"
            room = target
        text = defang_mentions(text)
        allowed = self.budget.take(text)
        if allowed is None:
            return "this call has used up its actions or its characters"
        if action == "reply":
            posted = await self.port.plugin_reply(self.event, allowed, silent=silent)
        else:
            posted = await self.port.plugin_send(room, allowed, silent=silent)
        return None if posted else "the message could not be posted"

    def final(self, reply: str | None) -> str | None:
        """The text a handler returned: one last reply, within the same budget."""
        if reply is None or not reply.strip():
            return None
        return self.budget.take(reply)


# --------------------------------------------------------------------------- #
# The manager
# --------------------------------------------------------------------------- #


class PluginManager:
    """Every plugin of one bot: discovered, validated, run and reported on.

    Without a directory nothing is built; the bot simply has no manager.
    """

    def __init__(
        self,
        config: Config,
        *,
        timeout: float | None = None,
        load_timeout: float = LOAD_TIMEOUT,
        command: CommandFactory = default_command,
        clock: Callable[[], float] = time.monotonic,
        quiet: bool = False,
    ) -> None:
        self.config = config
        #: Absolute: a worker runs in its plugin's directory, so a path relative to
        #: the working directory would not mean the same thing there.
        self.root = Path(config.plugins_dir).resolve()
        self._quiet = quiet
        self._timeout = float(config.plugins_timeout) if timeout is None else timeout
        self._load_timeout = load_timeout
        self._command = command
        self._clock = clock
        self.records: list[PluginRecord] = []
        self.notes: list[str] = []
        self._by_name: dict[str, PluginRecord] = {}
        #: Why the process could not be made uninspectable; empty if it was.
        self.hardening_problem = ""
        self.is_admin: Callable[[TalkEvent], bool] = self._config_admin
        self._phrases: list[_PhraseEntry] = []
        #: When each (plugin, handler, room) may fire again, on the injected clock.
        #: Oldest first: a handler that fires is moved to the end.
        self._cooldowns: dict[tuple[str, str, str], float] = {}

    def _config_admin(self, event: TalkEvent) -> bool:
        return not event.actor.is_bot and self.config.is_admin_user(event.actor.user_id)

    # -- loading ------------------------------------------------------------- #

    async def load_all(self, builtins: Registry) -> list[PluginRecord]:
        """Discover and validate every plugin. Never raises for a bad plugin.

        Stages, each of which can fail a plugin without touching the others:
        structure (discovery), settings, syntax, then, for the plugins that are
        enabled and have rooms, the handshake with a worker (import, declaration,
        ``check``) and finally collisions with built-ins and with earlier plugins.
        A plugin that is inactive or disabled is validated and never run.
        """
        self.hardening_problem = await asyncio.to_thread(disable_process_inspection)
        try:
            self.records, self.notes = await asyncio.to_thread(discover, self.root)
        except Exception as exc:
            log.error("plugins: looking for plugins failed with %s", type(exc).__name__)
            self.records, self.notes = [], [f"looking for plugins failed ({type(exc).__name__})"]
        await asyncio.to_thread(self._prepare_all)

        gate = asyncio.Semaphore(8)

        async def shake(record: PluginRecord) -> None:
            async with gate:
                await self._handshake(record)

        await asyncio.gather(*(shake(r) for r in self.records if r.status is Status.ACTIVE))
        await self._settle_names(builtins)
        await self._enforce_plugin_cap()
        self._by_name = {r.name: r for r in self.records if r.name and not r.duplicate}
        self._phrases = self._index_phrases()
        return self.records

    async def _enforce_plugin_cap(self) -> None:
        """At most :data:`MAX_PLUGINS` plugins are ever started.

        Counted here, after the handshake and after name collisions are settled, so
        that only a plugin that actually loaded - passed its settings and syntax
        checks, imported cleanly, declared something sane, and (if it has one)
        passed its own ``check`` - ever counts towards the limit or takes a place
        from a later one. The first, in path order, keep their place; anything past
        them is failed and never gets to keep its worker running.
        """
        admitted = 0
        for record in self.records:
            if record.status is not Status.ACTIVE:
                continue
            if admitted < MAX_PLUGINS:
                admitted += 1
                continue
            worker = record.worker
            record.worker = None
            record.fail(
                f"not loaded: sable starts at most {MAX_PLUGINS} plugins, and {MAX_PLUGINS} "
                "that loaded successfully, in path order, already are"
            )
            if worker is not None:
                await worker.aclose()

    def _index_phrases(self) -> list[_PhraseEntry]:
        """Every active plugin's phrase handlers, in plugin-name then handler-id order."""
        found: list[_PhraseEntry] = []
        for record in sorted(self.records, key=lambda r: r.name):
            if record.status is not Status.ACTIVE or record.declared is None:
                continue
            found.extend(
                _PhraseEntry(
                    record.name,
                    decl.id,
                    PhraseMatcher(decl.any, decl.whole_words),
                    decl.cooldown,
                )
                for decl in sorted(record.declared.phrases, key=lambda d: d.id)
            )
        return found

    def _prepare_all(self) -> None:
        for record in self.records:
            if record.status is Status.FAILED:
                continue
            try:
                self._prepare(record)
            except Exception as exc:
                # Whatever surprise one plugin's files hold, it ends with that plugin.
                # Only the type is said: the message may quote the file.
                log.error(
                    "plugin %s: unexpected %s while checking it", record.display, type(exc).__name__
                )
                record.fail(f"unexpected {type(exc).__name__} while checking it")

    def _prepare(self, record: PluginRecord) -> None:
        try:
            record.config = load_settings(_present(record.settings_path))
        except SettingsError as exc:
            record.fail(str(exc))
            return
        record.secrets = _secret_values(record.config.settings)
        if problem := check_syntax(_present(record.entry)):
            record.fail(problem)
            return
        config = record.config
        if not config.enabled:
            record.status = Status.DISABLED
        elif not config.access.rooms:
            record.status = Status.INACTIVE
            record.reason = "no rooms set"
        else:
            record.status = Status.ACTIVE
            self._note_room_warnings(record)

    def _note_room_warnings(self, record: PluginRecord) -> None:
        rooms = record.rooms
        if "*" in rooms:
            return
        outside = [room for room in rooms if not self.config.room_allowed(room)]
        if outside:
            record.warnings.append(
                f"{', '.join(outside)} {'is' if len(outside) == 1 else 'are'} not in "
                "SABLE_ALLOWED_ROOMS, so sable never reads there and the plugin never runs there"
            )

    async def _handshake(self, record: PluginRecord) -> None:
        config = _present(record.config)
        worker = Worker(
            record,
            timeout=self._timeout,
            load_timeout=self._load_timeout,
            command=self._command,
            clock=self._clock,
            timezone=self.config.timezone,
            quiet=self._quiet,
        )
        record.worker = worker
        try:
            record.declared = await worker.start(config.settings)
        except PluginFailure as exc:
            record.fail(str(exc))
            await worker.aclose()
            record.worker = None
        except Exception as exc:
            log.error(
                "plugin %s: unexpected %s while loading it", record.display, type(exc).__name__
            )
            record.fail(f"unexpected {type(exc).__name__} while loading it")
            await worker.aclose()
            record.worker = None

    async def _settle_names(self, builtins: Registry) -> None:
        """Fail a plugin whose command names collide: built-ins win, then the earlier plugin."""
        taken: dict[str, str] = {}
        for record in self.records:
            if record.status is not Status.ACTIVE or record.declared is None:
                continue
            clash = None
            for name in record.declared.command_names:
                if builtins.get(name) is not None:
                    clash = f"the command {name!r} is a built-in command"
                elif name in taken:
                    clash = f"the command {name!r} is already taken by the {taken[name]} plugin"
                if clash:
                    break
            if clash:
                record.fail(clash)
                if record.worker is not None:
                    await record.worker.aclose()
                    record.worker = None
                continue
            for name in record.declared.command_names:
                taken[name] = record.name

    # -- the registry -------------------------------------------------------- #

    def commands(self) -> list[Command]:
        """The registry entries of every active plugin's commands."""
        found: list[Command] = []
        for record in self.records:
            if record.status is not Status.ACTIVE or record.declared is None:
                continue
            found.extend(
                Command(
                    declared.name,
                    self._handler(record.name, declared.name),
                    declared.help,
                    declared.usage,
                    tuple(declared.aliases),
                    plugin=record.name,
                )
                for declared in record.declared.commands
            )
        return found

    def _handler(self, plugin: str, command: str) -> Callable[[Context], Awaitable[str | None]]:
        async def handler(ctx: Context) -> str | None:
            return await self.run_command(plugin, command, ctx)

        return handler

    # -- who may ------------------------------------------------------------- #

    def admins_only(self, plugin: str) -> bool:
        record = self._by_name.get(plugin)
        return bool(record and record.config and record.config.access.admins_only)

    def log_safe(self, plugin: str, text: str) -> str:
        """Worker-authored text, made fit for a log line: one line, with the plugin's
        own settings values blanked if it is known. Never for text shown to the
        person who triggered the call - a PluginError's chat-visible text stays
        exactly as the plugin wrote it, for both commands and phrases alike; this is
        only so that the same text, put in a log, cannot inject a fake log line or
        quote a secret verbatim.
        """
        record = self._by_name.get(plugin)
        return _one_line(record.redact(text) if record is not None else text)

    def allows(self, plugin: str, event: TalkEvent) -> Access:
        """May whoever caused this event trigger this plugin, here?

        In order: the plugin is active, the room is one of its rooms and one sable
        follows, the sender is an administrator if the plugin says so, and a user
        on its list if it has one. An administrator is not implicitly on that
        list. A bot is refused here, in the decision, as everywhere else.
        """
        record = self._by_name.get(plugin)
        if (
            record is None
            or record.status is not Status.ACTIVE
            or record.config is None
            or record.worker is None
            or record.worker.blocked
            or event.actor.is_bot
        ):
            return Access.NOT_HERE
        access = record.config.access
        room = event.room_token
        if not ("*" in access.rooms or (room and room in access.rooms)):
            return Access.NOT_HERE
        if not self.config.room_allowed(room):
            return Access.NOT_HERE
        if access.admins_only and not self.is_admin(event):
            return Access.NOT_YOU
        if access.users:
            wanted = event.actor.user_id.casefold()
            listed = {user.casefold().removeprefix("users/") for user in access.users}
            if not wanted or wanted not in listed:
                return Access.NOT_YOU
        return Access.OK

    # -- phrases ------------------------------------------------------------- #

    @property
    def cooldown_entries(self) -> int:
        """How many cooldowns are remembered right now."""
        return len(self._cooldowns)

    def _cooling(self, key: tuple[str, str, str], now: float) -> bool:
        until = self._cooldowns.get(key)
        return until is not None and now < until

    def phrase_hits(self, event: TalkEvent) -> tuple[PhraseHit, ...]:
        """The phrase handlers this message would fire. Reads, never writes.

        Only the handlers whose plugin may be triggered by this person here (the same
        :meth:`allows` as a command) and that are not cooling down are considered, and
        the message is searched only for those. Cheap when no plugin has a phrase, and
        for ordinary chatter that matches nothing: no worker is involved, and nothing is
        consumed.

        At most :data:`MAX_PHRASE_FIRES_PER_MESSAGE` fire, round-robined one per
        distinct plugin before any plugin gets a second: otherwise a plugin with
        several handlers on a broad, cooldown-0 phrase could fill every slot itself
        and starve every other plugin's handler for that phrase, indefinitely.
        """
        if not self._phrases or not event.is_message:
            return ()
        now = self._clock()
        allowed: dict[str, bool] = {}
        folded: str | None = None
        # One queue per eligible plugin, each in handler-id order (self._phrases is
        # already sorted by plugin name then handler id, and dicts keep insertion
        # order, so both the plugin order and each queue's order come for free).
        queues: dict[str, list[_PhraseEntry]] = {}
        for entry in self._phrases:
            may = allowed.get(entry.plugin)
            if may is None:
                may = allowed[entry.plugin] = self.allows(entry.plugin, event) is Access.OK
            if not may or self._cooling((entry.plugin, entry.handler, event.room_token), now):
                continue
            queues.setdefault(entry.plugin, []).append(entry)
        if not queues:
            return ()

        hits: list[PhraseHit] = []
        pending = list(queues.values())
        while pending and len(hits) < MAX_PHRASE_FIRES_PER_MESSAGE:
            still_pending = []
            for queue in pending:
                if len(hits) >= MAX_PHRASE_FIRES_PER_MESSAGE:
                    break
                entry = queue.pop(0)
                if folded is None:
                    folded = _folded_message(event)
                matched = entry.matcher.match(folded)
                if matched is not None:
                    hits.append(
                        PhraseHit(
                            entry.plugin, entry.handler, matched, entry.cooldown, event.room_token
                        )
                    )
                if queue:
                    still_pending.append(queue)
            pending = still_pending
        return tuple(hits)

    def claim_phrase(self, hit: PhraseHit) -> bool:
        """Start a handler's cooldown, at the moment it is dispatched.

        False if it is cooling down already, which can only be because something else
        claimed it since :meth:`phrase_hits` looked.
        """
        now = self._clock()
        key = (hit.plugin, hit.handler, hit.room)
        if self._cooling(key, now):
            return False
        if hit.cooldown > 0:
            self._cooldowns.pop(key, None)
            self._cooldowns[key] = now + hit.cooldown
            if len(self._cooldowns) > MAX_COOLDOWN_ENTRIES:
                self._evict(now)
        return True

    def _evict(self, now: float) -> None:
        """Keep the table bounded: what has expired goes first, then whichever of what
        is left is soonest to expire - never by insertion order, which could otherwise
        evict a long cooldown claimed early in favour of a short one claimed a moment
        later, letting the long one's handler fire again before its time.
        """
        for key in [k for k, until in self._cooldowns.items() if until <= now]:
            del self._cooldowns[key]
        over = len(self._cooldowns) - MAX_COOLDOWN_ENTRIES
        if over <= 0:
            return
        soonest = sorted(self._cooldowns.items(), key=lambda item: item[1])
        for key, _ in soonest[:over]:
            del self._cooldowns[key]

    async def run_phrase(self, hit: PhraseHit, event: TalkEvent, port: ChatPort) -> str | None:
        """Run one phrase handler for a message; the reply text, or None for silence.

        Raises CommandError for a handler's own ``PluginError`` and :class:`PluginFailure`
        when the plugin crashed, took too long or is switched off. Nobody asked, so the
        caller logs both and says nothing in the room.
        """
        record = self._by_name.get(hit.plugin)
        if record is None:
            raise PluginFailure(f"the `{hit.plugin}` plugin is not running")
        sink = _ChatSink(record, port, event, self.config)
        payload = self._payload(
            hit.plugin, "phrase", hit.handler, "", [], event, self.is_admin(event), hit.phrase
        )
        outcome = await self.call(hit.plugin, f"phrase:{hit.handler}", payload, sink)
        return self._settle(record, f"the {hit.handler} handler", outcome, sink)

    # -- calling ------------------------------------------------------------- #

    @staticmethod
    def _payload(
        plugin: str,
        trigger: str,
        name: str,
        args: str,
        argv: list[str],
        event: TalkEvent,
        is_admin: bool,
        match: str = "",
    ) -> dict[str, Any]:
        """The context a handler is called with. Only what the event itself says."""
        return {
            "plugin": plugin,
            "trigger": trigger,
            "name": name,
            "args": args,
            "argv": argv,
            "room": event.room_token,
            "actor_id": event.actor.id,
            "user_id": event.actor.user_id,
            "actor_name": event.actor.name,
            "is_admin": is_admin,
            "message_id": event.message_id,
            "text": event.message,
            "match": match,
        }

    def _settle(
        self, record: PluginRecord, what: str, outcome: CallOutcome, sink: _ChatSink
    ) -> str | None:
        """Turn what a worker said into a reply, a CommandError or a PluginFailure."""
        if not outcome.ok:
            if outcome.user_visible and outcome.error:
                raise CommandError(outcome.error)
            record.last_error = outcome.error or "the handler failed"
            log.warning("plugin %s: %s failed: %s", record.name, what, record.last_error)
            raise PluginFailure(f"the `{record.name}` plugin crashed")
        return sink.final(outcome.reply)

    async def call(
        self, plugin: str, handler: str, payload: dict[str, Any], sink: ActSink
    ) -> CallOutcome:
        """Run one handler of one plugin (``command:<name>``, later ``phrase:`` and
        ``schedule:``) and return what its worker said.

        Does not check who may trigger it: :meth:`allows` is the caller's job, before
        the worker is ever asked. Raises :class:`PluginFailure` if the plugin is not
        running, crashed or took too long.
        """
        record = self._by_name.get(plugin)
        if record is None or record.worker is None:
            raise PluginFailure(f"the `{plugin}` plugin is not running")
        try:
            return await record.worker.call(handler, payload, sink)
        except PluginFailure as exc:
            record.last_error = record.last_error or str(exc)
            raise

    async def run_command(self, plugin: str, command: str, ctx: Context) -> str | None:
        """Run one plugin command for a chat event; the reply text, or None for silence.

        Raises CommandError for a plugin's own ``PluginError`` (its text is for the
        chat) and :class:`PluginFailure` when the plugin crashed or took too long.
        """
        record = self._by_name.get(plugin)
        if record is None:
            raise PluginFailure(f"the `{plugin}` plugin is not running")
        event = ctx.event
        sink = _ChatSink(record, ctx.bot, event, self.config)
        payload = self._payload(plugin, "command", command, ctx.args, ctx.argv, event, ctx.is_admin)
        outcome = await self.call(plugin, f"command:{command}", payload, sink)
        return self._settle(record, f"the {command} handler", outcome, sink)

    # -- reporting ----------------------------------------------------------- #

    def counts(self) -> dict[str, int]:
        found = {status.value: 0 for status in Status}
        for record in self.records:
            found[record.effective_status.value] += 1
        return found

    def failures(self) -> list[PluginRecord]:
        return [r for r in self.records if r.effective_status is Status.FAILED]

    def summary(self) -> str:
        """The banner line: ``3 active, 1 inactive, 1 failed (/plugins)``."""
        counts = self.counts()
        parts = [f"{counts[s.value]} {s.value}" for s in Status if counts[s.value]]
        return f"{', '.join(parts) or 'none found'} ({self.config.plugins_dir})"

    def log_status(self) -> None:
        """Say what happened at load: a warning for each failure, a line for the rest."""
        if self.hardening_problem:
            log.warning(
                "could not make sable's own memory unreadable to plugins (%s): a plugin runs "
                "as the same user and could read this process's environment, including the "
                "account's password. Run sable in a container, or on Linux.",
                self.hardening_problem,
            )
        for note in self.notes:
            log.warning("plugins: %s", note)
        for record in self.records:
            if record.status is Status.FAILED:
                log.warning("plugin %s failed: %s", record.display, record.reason)
            elif record.status is Status.INACTIVE:
                log.info("plugin %s is inactive: %s", record.display, record.reason)
            elif record.status is Status.DISABLED:
                log.info("plugin %s is disabled", record.display)
            else:
                log.info(
                    "plugin %s is active in %s: %s",
                    record.display,
                    ", ".join(record.rooms),
                    self._what(record),
                )
            for warning in record.warnings:
                log.warning("plugin %s: %s", record.display, warning)

    @staticmethod
    def _what(record: PluginRecord) -> str:
        declared = record.declared
        if declared is None:
            return "nothing loaded"
        parts = [f"commands: {', '.join(declared.command_names) or 'none'}"]
        if declared.phrases:
            parts.append(f"phrases: {', '.join(p.id for p in declared.phrases)}")
        if declared.schedules:
            parts.append(f"{len(declared.schedules)} schedule(s)")
        return "; ".join(parts)

    def check_lines(self) -> list[str]:
        """The per-plugin result, for ``--check``."""
        lines = [f"  plugins:   {self.summary()}"]
        lines += [f"    {note}" for note in self.notes]
        for record in self.records:
            detail = self._what(record) if record.status is Status.ACTIVE else ""
            lines.append(
                f"    {record.display}: {record.state}" + (f" ({detail})" if detail else "")
            )
            lines += [f"      warning: {warning}" for warning in record.warnings]
        if self.hardening_problem:
            lines.append(
                f"    warning: sable's memory stays readable to plugins ({self.hardening_problem})"
            )
        return lines

    def report(self) -> str:
        """The ``!plugins`` list. Names, status, triggers and rooms: never settings."""
        if not self.records:
            return f"**Plugins**: none found in `{self.config.plugins_dir}`."
        lines = [f"**Plugins** - {self.summary()}"]
        prefix = self.config.command_prefix
        for record in self.records:
            line = f"- `{record.display}` - {record.state}"
            declared = record.declared
            if declared is not None and record.effective_status is Status.ACTIVE:
                if declared.commands:
                    line += " - " + ", ".join(
                        f"`{prefix}{command.name}`" for command in declared.commands
                    )
                if declared.phrases:
                    line += " - phrases: " + ", ".join(f"`{p.id}`" for p in declared.phrases)
                if declared.schedules:
                    line += f" - {len(declared.schedules)} schedule(s)"
            if record.rooms:
                line += f" - rooms: {', '.join(record.rooms)}"
            lines.append(line)
        lines += [f"_{note}_" for note in self.notes]
        lines.append(f"`{prefix}plugins <name>` for one in detail.")
        return "\n".join(lines)

    def describe(self, name: str) -> str:
        """The ``!plugins <name>`` detail. Never includes the plugin's settings."""
        record = self._by_name.get(name.strip().lower())
        if record is None:
            raise CommandError(f"No plugin called `{_safe(name, 40)}`.")
        prefix = self.config.command_prefix
        lines = [f"**{record.display}** - {record.state}", f"File: `{record.label}`"]
        if record.config is not None:
            access = record.config.access
            lines.append(
                "Rooms: " + (", ".join(access.rooms) or "none (inactive until some are set)")
            )
            lines.append(
                "Users: " + (", ".join(access.users) if access.users else "anyone in those rooms")
            )
            if access.admins_only:
                lines.append("Administrators only.")
        declared = record.declared
        # A plugin that failed, or is switched off, offers nothing: do not list its
        # commands as if they worked.
        if declared is not None and record.effective_status is Status.ACTIVE:
            for command in declared.commands:
                text = f"Command `{prefix}{command.usage or command.name}`"
                if command.help:
                    text += f" - {command.help}"
                if command.aliases:
                    text += (
                        " (aliases: " + ", ".join(f"`{prefix}{a}`" for a in command.aliases) + ")"
                    )
                lines.append(text)
            lines.extend(self._describe_phrase(phrase) for phrase in declared.phrases)
            if declared.schedules:
                lines.append(f"Schedules: {len(declared.schedules)}")
        if record.worker is not None and record.worker.restart_count:
            lines.append(f"Restarts since sable started: {record.worker.restart_count}")
        if record.last_error:
            lines.append(f"Last error: {record.redact(record.last_error)}")
        lines += [f"Warning: {warning}" for warning in record.warnings]
        return "\n".join(lines)

    @staticmethod
    def _describe_phrase(phrase: DeclaredPhrase) -> str:
        """A phrase handler for ``!plugins <name>``: the phrases and the cooldown. A phrase
        is plain text an author chose, shown between quotes and cut to a sane length.
        ``_encode_safe`` is a backstop, not the defence (see its docstring)."""
        shown = ", ".join(
            f'"{_one_line(_encode_safe(p).replace(chr(96), chr(39)), 60)}"' for p in phrase.any
        )
        how = "whole words" if phrase.whole_words else "anywhere in a word"
        return f"Phrase handler `{phrase.id}`: {shown[:600]} ({how}, cooldown {phrase.cooldown}s)"

    # -- ending -------------------------------------------------------------- #

    async def aclose(self) -> None:
        """Shut every worker down, process groups included. Safe to call twice."""
        await asyncio.gather(
            *(record.worker.aclose() for record in self.records if record.worker is not None)
        )


async def check_plugins(config: Config, builtins: Registry) -> tuple[list[str], bool]:
    """Discover and validate every plugin, for ``--check`` and the strict preflight.

    Returns the report lines and whether strict mode would refuse to start.
    """
    # Quiet: what the workers write to stderr belongs in the report, not above it.
    manager = PluginManager(config, quiet=True)
    try:
        await manager.load_all(builtins)
    finally:
        await manager.aclose()
    return manager.check_lines(), bool(config.plugins_strict and manager.failures())
