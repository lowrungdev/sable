"""What a sable plugin imports: the decorators, :class:`Context` and the errors.

A plugin is a Python file that declares handlers with decorators and answers
each call with a Markdown string (or ``None`` for silence)::

    from sable.plugin_api import Context, PluginError, command, on_phrase

    @command("weather", aliases=("wx",), help="Forecast for a city", usage="weather <city>")
    async def weather(ctx: Context) -> str | None:
        if not ctx.args:
            raise PluginError("Which city?")
        return f"Sunny in {ctx.args}."

    @on_phrase(any=["good morning", "gm"], whole_words=True, cooldown="1h")
    async def greet(ctx: Context) -> str | None:
        return f"Good morning, {ctx.actor_name}!"

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
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
    "check_breadth",
    "check_phrases",
    "command",
    "declarations",
    "fold",
    "freeze",
    "on_phrase",
    "parse_cooldown",
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
#: What ``@on_phrase`` accepts: how many phrases, how long each is (after strip),
#: and the cooldown in seconds (a week), which is 30 seconds unless said otherwise.
MAX_PHRASES = 20
MIN_PHRASE = 2
MAX_PHRASE = 100
MAX_COOLDOWN = 7 * 24 * 3600
DEFAULT_COOLDOWN = 30
#: Below this, a phrase without word boundaries (``whole_words=False``) would match
#: inside almost any longer word: too broad to be a meaningful trigger, and close to
#: reading every message. Short phrases are still fine with ``whole_words=True``.
MIN_SUBSTRING_PHRASE = 3
#: Categories a phrase (after folding) may not consist of alone: it would then have
#: no character a person could type or read, so nothing can ever visibly match it.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Zl", "Zp", "Mn"})
#: Categories refused in a phrase outright: control characters, line/paragraph
#: separators (would not survive a single line of chat), lone surrogates and
#: unassigned code points (not valid, independent text - and encoding one for a log
#: or a chat message can raise).
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs", "Cn", "Zl", "Zp"})
#: A handler's id on the wire: its function name.
ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_COOLDOWN_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
#: re.ASCII: '\d' matches only '0'-'9', not every Unicode decimal digit.
_COOLDOWN_RE = re.compile(r"^(\d{1,9})([smhd])$", re.ASCII)

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


@dataclass(frozen=True, slots=True)
class PhraseDecl:
    """One ``@on_phrase``, as the host reads it. ``cooldown`` is in seconds."""

    id: str
    phrases: tuple[str, ...]
    whole_words: bool
    cooldown: int
    handler: Handler


@dataclass(slots=True)
class Declarations:
    """Everything a plugin declared, in declaration order.

    One list per kind of trigger. Step 3 adds ``schedules`` beside them without
    touching this shape.
    """

    commands: list[CommandDecl] = field(default_factory=list)
    phrases: list[PhraseDecl] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Handlers of every kind, for the per-plugin cap."""
        return len(self.commands) + len(self.phrases)


#: The host imports exactly one plugin per process, so a module-level collection
#: is the whole registry. ``reset_declarations`` exists for the host to start
#: clean and for tests to run in one process repeatedly.
_declarations = Declarations()
#: Command names and aliases already taken in this plugin (one namespace).
_taken: set[str] = set()
#: Handler ids (function names) of the triggers that have one: unique across all
#: of those kinds in this plugin, since an id names one handler.
_taken_ids: set[str] = set()


def declarations() -> Declarations:
    """A snapshot of what has been declared since the last reset."""
    return Declarations(commands=list(_declarations.commands), phrases=list(_declarations.phrases))


def reset_declarations() -> None:
    """Forget every declaration."""
    _declarations.commands.clear()
    _declarations.phrases.clear()
    _taken.clear()
    _taken_ids.clear()


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


def fold(text: str) -> str:
    """How a phrase and a message are compared: NFKC-normalised, then casefolded.

    The same function runs in the core, on the message, so that what an author
    sees here is what is matched there.
    """
    return unicodedata.normalize("NFKC", text).casefold()


def parse_cooldown(value: object) -> int:
    """A cooldown as whole seconds: an int, or text like ``"30s"``, ``"5m"``,
    ``"1h"``, ``"1d"``. 0 (no cooldown) to a week.

    Raises :class:`PluginDeclarationError`.
    """
    if isinstance(value, bool):
        raise PluginDeclarationError("cooldown must be a number of seconds or text like '5m'")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        found = _COOLDOWN_RE.match(value.strip())
        if found is None:
            raise PluginDeclarationError(
                f"cooldown {value[:20]!r} is not understood: use seconds (30) or text like "
                "'30s', '5m', '1h' or '1d'"
            )
        seconds = int(found.group(1)) * _COOLDOWN_UNITS[found.group(2)]
    else:
        raise PluginDeclarationError(
            f"cooldown must be a number of seconds or text like '5m', not {type(value).__name__}"
        )
    if not 0 <= seconds <= MAX_COOLDOWN:
        raise PluginDeclarationError(
            f"cooldown is {seconds} seconds; it must be between 0 and {MAX_COOLDOWN} (a week)"
        )
    return seconds


def _has_visible_character(text: str) -> bool:
    """Is there a character in ``text`` a person could actually see or type?

    False for an empty string, for one made only of combining marks with no base
    character, or of format characters (zero-width joiners and the like) - all of
    which fold-in without changing the character count, and are not something a
    phrase can meaningfully consist of only.
    """
    return any(unicodedata.category(ch) not in _INVISIBLE_CATEGORIES for ch in text)


def check_phrases(value: object) -> tuple[str, ...]:
    """Validate the ``any=`` list of ``@on_phrase``: 1 to 20 strings of 2 to 100
    characters after stripping, no control characters, duplicates (by folded
    text) dropped. The core applies the same rules to what the worker declares.
    """
    if isinstance(value, str):
        raise PluginDeclarationError(f'any must be a list like ["{value[:20]}"], not a string')
    if not isinstance(value, Sequence):
        raise PluginDeclarationError("any must be a list of phrases")
    if not 1 <= len(value) <= MAX_PHRASES:
        raise PluginDeclarationError(
            f"any must hold between 1 and {MAX_PHRASES} phrases, not {len(value)}"
        )
    kept: dict[str, str] = {}
    for item in value:
        if not isinstance(item, str):
            raise PluginDeclarationError(
                f"every phrase in any must be a string, not {type(item).__name__}"
            )
        phrase = item.strip()
        if not MIN_PHRASE <= len(phrase) <= MAX_PHRASE:
            raise PluginDeclarationError(
                f"the phrase {phrase[:30]!r} is {len(phrase)} characters after stripping; "
                f"it must be {MIN_PHRASE} to {MAX_PHRASE}"
            )
        if any(unicodedata.category(ch) in _FORBIDDEN_CATEGORIES for ch in phrase):
            raise PluginDeclarationError(
                f"the phrase {phrase[:30]!r} holds a control character, a line break, or a "
                "surrogate or unassigned code point"
            )
        # Checked again after folding: canonical composition can shrink a sequence
        # (an unaccented letter plus a combining accent becomes one precomposed
        # character), so a phrase that is long enough before folding can still fold
        # down to nothing a person could ever type or read.
        folded = fold(phrase)
        if len(folded) < MIN_PHRASE or not _has_visible_character(folded):
            raise PluginDeclarationError(
                f"the phrase {phrase[:30]!r} is too short, or has no visible character, "
                f"once folded (casefolded and Unicode-normalised): at least {MIN_PHRASE} "
                "visible characters must remain"
            )
        kept.setdefault(folded, phrase)
    return tuple(kept.values())


def check_breadth(phrases: Sequence[str], whole_words: bool) -> None:
    """Refuse a handler whose phrases, combined with ``whole_words=False``, would
    match almost anything: a short phrase with no word boundary is not a trigger, it
    is close to reading every message in the handler's rooms.
    """
    if whole_words:
        return
    for phrase in phrases:
        if len(phrase) < MIN_SUBSTRING_PHRASE:
            raise PluginDeclarationError(
                f"the phrase {phrase[:30]!r} is {len(phrase)} characters: too broad for "
                f"whole_words=False, which would match it inside any longer word. Phrases "
                f"under {MIN_SUBSTRING_PHRASE} characters must use whole_words=True"
            )


def on_phrase(
    *args: Any,
    any: Sequence[str] | None = None,
    whole_words: bool = True,
    cooldown: int | str = DEFAULT_COOLDOWN,
) -> Callable[[Handler], Handler]:
    """Declare a handler for messages that contain one of some phrases.

    ::

        @on_phrase(any=["good morning", "gm"], whole_words=True, cooldown="1h")
        async def greet(ctx): ...

    Matching is literal, case-insensitive (Unicode casefold after NFKC) and done by
    sable, never by the plugin. With ``whole_words`` (the default) a phrase must not
    be touched by a word character on either side where its own edge is a word
    character: ``gm`` matches "gm!" and "say gm" but not "gmail", while ``:)`` and
    ``c++`` have non-word edges and match anywhere. Without it, any substring does.

    It fires only for ordinary messages: not for a ``!command``, and not for a message
    addressed to the bot. ``cooldown`` is how long this handler stays quiet in a room
    after it fired there (30 seconds by default; ``0`` for none). The handler's id is
    its function name, and must be unique in the plugin. ``ctx.match`` is the declared
    phrase that matched. A phrase under 3 characters must use ``whole_words=True``: any
    shorter with ``whole_words=False`` would match inside almost any word, which is a
    declaration error.

    Validated here, at decoration time, like :func:`command`.
    """
    if args:
        raise PluginDeclarationError(
            '@on_phrase needs arguments: write @on_phrase(any=["gm"]), with brackets'
        )
    if any is None:
        raise PluginDeclarationError('@on_phrase needs any=["a phrase", ...]')
    checked = check_phrases(any)
    if not isinstance(whole_words, bool):
        raise PluginDeclarationError("whole_words must be True or False")
    check_breadth(checked, whole_words)
    seconds = parse_cooldown(cooldown)

    def decorate(handler: Handler) -> Handler:
        ok = _check_handler(handler)
        ident = getattr(handler, "__name__", "")
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
            raise PluginDeclarationError(
                f"{_label(handler)} has no usable name: a phrase handler's id is its function "
                "name, letters, digits and '_', starting with a letter or '_'"
            )
        if ident in _taken_ids:
            raise PluginDeclarationError(
                f"the handler id {ident!r} is declared more than once (a handler's id is its "
                "function name, unique across all kinds of trigger in the plugin)"
            )
        if _declarations.count >= MAX_HANDLERS:
            raise PluginDeclarationError(f"a plugin may declare at most {MAX_HANDLERS} handlers")
        _taken_ids.add(ident)
        _declarations.phrases.append(
            PhraseDecl(
                id=ident,
                phrases=checked,
                whole_words=whole_words,
                cooldown=seconds,
                handler=ok,
            )
        )
        return handler

    return decorate
