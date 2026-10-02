"""What a sable plugin imports: the decorators, :class:`Context` and the errors.

A plugin is a Python file that declares handlers with decorators and answers
each call with a Markdown string (or ``None`` for silence)::

    from sable.plugin_api import Context, PluginError, command

    @command("weather", aliases=("wx",), help="Forecast for a city", usage="weather <city>")
    async def weather(ctx: Context) -> str | None:
        if not ctx.args:
            raise PluginError("Which city?")
        return f"Sunny in {ctx.args}."

This module is deliberately pure: standard library only, nothing imported from
the rest of ``sable``. It runs inside the worker process, where the host
(``sable.plugin_host``) imports the plugin, reads the declarations from here
and builds a :class:`Context` for every call. Everything a handler does to the
outside world (:meth:`Context.reply` and friends) goes through a transport the
host injects, and the core process enforces every limit on the other end of it:
nothing here is a security boundary, it only keeps authors honest early.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

__all__ = [
    "Context",
    "Declarations",
    "Handler",
    "PluginActionError",
    "PluginDeclarationError",
    "PluginError",
    "Transport",
    "command",
    "declarations",
    "freeze",
    "reset_declarations",
]

#: A command (or alias) name: lowercase, starts with a letter. The same rule
#: the core applies to plugin names, so ``!name`` always reads the same way.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: Limits that keep ``!help`` and ``!plugins`` readable.
MAX_HELP = 200
MAX_USAGE = 100
#: Handlers of every kind in one plugin.
MAX_HANDLERS = 32

Handler = Callable[["Context"], Awaitable["str | None"]]


class PluginError(Exception):
    """Raise from a handler to answer with this text, as the plugin's own error.

    The message is shown in chat, like a built-in command's usage error, so it
    must be written for the person who typed the command. Any other exception
    is a crash: logged with its traceback, and the chat only learns that the
    plugin crashed.
    """


class PluginDeclarationError(ValueError):
    """A decorator was used wrongly. Raised at import time, so the plugin fails
    to load with this message rather than misbehaving later."""


class PluginActionError(Exception):
    """The core refused or failed an action (``send`` to a room the plugin may
    not post in, for instance). Not a :class:`PluginError`: unless the handler
    catches it, it is a crash, because the person who typed the command did not
    cause it and cannot fix it."""


class Transport(Protocol):
    """What the host provides so a :class:`Context` can act. One per call."""

    async def act(self, action: str, args: Mapping[str, Any]) -> None:
        """Carry out ``reply``, ``send`` or ``react``; raise
        :class:`PluginActionError` when the core says no."""


def freeze(value: Any) -> Any:
    """A deeply read-only copy of JSON-like data.

    Mappings become :class:`types.MappingProxyType` over a fresh dict, lists
    and tuples become tuples, everything else is returned as it is (scalars are
    immutable already). The copy shares nothing with the original, so the
    handler cannot reach back into what the host holds.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class Context:
    """One call's view of the world. Read-only; the host fills it in.

    ``trigger`` is ``"command"``, ``"phrase"`` or ``"schedule"``; ``name`` is the
    command or handler name. ``args`` is everything after the command and
    ``argv`` the same split into words (both empty for other triggers). ``room``
    is the Talk token the call is about, ``actor_*`` is who triggered it
    (``actor_id`` is the full Talk id, such as ``users/alice``; ``user_id`` is the
    bare Nextcloud user id, ``alice``, and empty for a guest, a bot or a schedule),
    and ``text``/``message_id`` are the full triggering message. ``settings`` is the
    ``settings:`` block of the plugin's YAML, deeply frozen.

    ``reply``, ``send`` and ``react`` only work while the handler is running: once
    it has returned, they raise :class:`PluginActionError`, and tasks the handler
    started but did not await are cancelled. Await every action before returning.
    """

    plugin: str
    trigger: Literal["command", "phrase", "schedule"]
    name: str
    args: str
    argv: list[str]
    room: str
    actor_id: str
    actor_name: str
    is_admin: bool
    message_id: int
    text: str
    match: str
    settings: Mapping[str, Any]
    log: logging.Logger = field(repr=False)
    user_id: str = ""
    _transport: Transport | None = field(default=None, repr=False, compare=False)

    async def reply(self, text: str, *, silent: bool = False) -> None:
        """Post ``text`` into the room that triggered this call.

        May be called more than once, up to the core's per-call cap. Returning
        a string from the handler is the same as one last ``reply``. Raises
        :class:`PluginActionError` if the handler has already returned.
        """
        await self._act("reply", {"text": _need_str("text", text), "silent": bool(silent)})

    async def send(self, room: str, text: str, *, silent: bool = False) -> None:
        """Post ``text`` into another room, which must be one of this plugin's
        ``access.rooms``; otherwise :class:`PluginActionError`. Also raises it if
        the handler has already returned."""
        await self._act(
            "send",
            {
                "room": _need_str("room", room),
                "text": _need_str("text", text),
                "silent": bool(silent),
            },
        )

    async def react(self, emoji: str) -> None:
        """React to the message that triggered this call. Raises
        :class:`PluginActionError` if the handler has already returned."""
        await self._act("react", {"emoji": _need_str("emoji", emoji)})

    async def _act(self, action: str, args: Mapping[str, Any]) -> None:
        if self._transport is None:
            raise RuntimeError("this Context is not attached to a running plugin host")
        await self._transport.act(action, args)


def _need_str(what: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string, not {type(value).__name__}")
    return value


# --- declaration registry -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandDecl:
    """One ``@command``, as the host reads it."""

    name: str
    aliases: tuple[str, ...]
    help: str
    usage: str
    handler: Handler


@dataclass(slots=True)
class Declarations:
    """Everything a plugin declared, in declaration order.

    One list per kind of trigger. Step 2 adds ``phrases`` and step 3
    ``schedules`` beside ``commands`` without touching this shape.
    """

    commands: list[CommandDecl] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Handlers of every kind, for the per-plugin cap."""
        return len(self.commands)


#: The host imports exactly one plugin per process, so a module-level collection
#: is the whole registry. ``reset_declarations`` exists for the host to start
#: clean and for tests to run in one process repeatedly.
_declarations = Declarations()
#: Command names and aliases already taken in this plugin (one namespace).
_taken: set[str] = set()


def declarations() -> Declarations:
    """A snapshot of what has been declared since the last reset."""
    return Declarations(commands=list(_declarations.commands))


def reset_declarations() -> None:
    """Forget every declaration."""
    _declarations.commands.clear()
    _taken.clear()


def _label(handler: object) -> str:
    return getattr(handler, "__qualname__", None) or repr(handler)


def _check_name(what: str, value: object) -> str:
    if not isinstance(value, str):
        raise PluginDeclarationError(f"{what} must be a string, not {type(value).__name__}")
    if not NAME_RE.fullmatch(value):
        raise PluginDeclarationError(
            f"{what} {value[:40]!r} is not valid: use lowercase letters, digits, '-' and '_', "
            "starting with a letter, at most 32 characters"
        )
    return value


def _check_text(what: str, value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise PluginDeclarationError(f"{what} must be a string, not {type(value).__name__}")
    if len(value) > limit:
        raise PluginDeclarationError(f"{what} is {len(value)} characters; the limit is {limit}")
    if any(not ch.isprintable() for ch in value):
        raise PluginDeclarationError(f"{what} must be a single line of plain text")
    return value


def _check_handler(handler: object) -> Handler:
    if not callable(handler) or not inspect.iscoroutinefunction(handler):
        raise PluginDeclarationError(
            f"{_label(handler)} must be an 'async def' function, because sable awaits it"
        )
    try:
        inspect.signature(handler).bind(None)
    except TypeError:
        raise PluginDeclarationError(
            f"{_label(handler)} must take exactly one argument, the Context"
        ) from None
    return handler


def command(
    name: str,
    *,
    aliases: tuple[str, ...] | list[str] = (),
    help: str = "",
    usage: str = "",
) -> Callable[[Handler], Handler]:
    """Declare a ``!name`` command.

    Validated here, at decoration time: a bad declaration raises
    :class:`PluginDeclarationError` and the plugin does not load. The decorated
    function is returned unchanged.
    """
    if callable(name):
        raise PluginDeclarationError('@command needs a name: write @command("name"), with brackets')
    cmd = _check_name("command name", name)
    if isinstance(aliases, str):
        raise PluginDeclarationError(f'aliases must be a tuple like ("{aliases}",), not a string')
    try:
        alias_list = tuple(aliases)
    except TypeError:
        raise PluginDeclarationError("aliases must be a tuple of names") from None
    names = [cmd, *(_check_name("alias", alias) for alias in alias_list)]
    help_text = _check_text("help", help, MAX_HELP)
    usage_text = _check_text("usage", usage, MAX_USAGE)

    def decorate(handler: Handler) -> Handler:
        checked = _check_handler(handler)
        seen: set[str] = set()
        for each in names:
            if each in _taken or each in seen:
                raise PluginDeclarationError(f"command {each!r} is declared more than once")
            seen.add(each)
        if _declarations.count >= MAX_HANDLERS:
            raise PluginDeclarationError(f"a plugin may declare at most {MAX_HANDLERS} handlers")
        # Nothing is recorded before every check has passed, so a rejected
        # declaration leaves the registry as it was.
        _taken.update(names)
        _declarations.commands.append(
            CommandDecl(
                name=cmd,
                aliases=tuple(names[1:]),
                help=help_text,
                usage=usage_text,
                handler=checked,
            )
        )
        return handler

    return decorate
