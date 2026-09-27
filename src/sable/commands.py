"""The command registry and the built-in commands.

Adding a command is one decorator::

    from sable.commands import registry

    @registry.command("weather", help="Show the forecast for a city.")
    async def weather(ctx: Context) -> str:
        return f"It is always sunny in {ctx.args or 'Philadelphia'}."

Return a Markdown string to reply, or ``None`` to stay silent. Raising
:class:`CommandError` replies with the message instead of logging a traceback.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from .events import TalkEvent

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .bot import Bot


class CommandError(Exception):
    """User-facing failure: the text is posted back to the conversation."""


@dataclass
class Context:
    """Everything a command handler is given."""

    bot: Bot
    event: TalkEvent
    name: str
    args: str
    argv: list[str] = field(default_factory=list)

    @property
    def room_token(self) -> str:
        return self.event.room_token

    @property
    def sender(self) -> str:
        return self.event.actor.name or self.event.actor.id


Handler = Callable[[Context], Awaitable[str | None]]


@dataclass
class Command:
    name: str
    handler: Handler
    help: str = ""
    usage: str = ""
    aliases: tuple[str, ...] = ()
    hidden: bool = False


class Registry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._aliases: dict[str, str] = {}

    def command(
        self,
        name: str,
        *,
        help: str = "",
        usage: str = "",
        aliases: tuple[str, ...] = (),
        hidden: bool = False,
    ) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            key = name.lower()
            if key in self._commands or key in self._aliases:
                raise ValueError(f"command {name!r} is already registered")
            self._commands[key] = Command(key, handler, help, usage, aliases, hidden)
            for alias in aliases:
                self._aliases[alias.lower()] = key
            return handler

        return decorator

    def get(self, name: str) -> Command | None:
        key = name.lower()
        return self._commands.get(self._aliases.get(key, key))

    def visible(self) -> list[Command]:
        return sorted(
            (c for c in self._commands.values() if not c.hidden), key=lambda c: c.name
        )

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None


registry = Registry()


def split_command(text: str, prefix: str) -> tuple[str, str] | None:
    """Split ``!name rest`` into ``(name, rest)``, or ``None`` if not a command."""
    if not prefix or not text.startswith(prefix):
        return None
    body = text[len(prefix) :].lstrip()
    if not body:
        return None
    name, _, rest = body.partition(" ")
    return name.strip(), rest.strip()


def parse_argv(args: str) -> list[str]:
    """Shell-style argument split, tolerant of unbalanced quotes."""
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


# --------------------------------------------------------------------------- #
# Built-ins
# --------------------------------------------------------------------------- #


@registry.command("help", help="List the commands I know.", aliases=("commands", "?"))
async def help_command(ctx: Context) -> str:
    prefix = ctx.bot.config.command_prefix
    if ctx.args:
        command = ctx.bot.registry.get(ctx.args.split()[0])
        if command is None:
            raise CommandError(f"I have no `{ctx.args.split()[0]}` command.")
        lines = [f"**{prefix}{command.name}** - {command.help or 'No description.'}"]
        if command.usage:
            lines.append(f"Usage: `{prefix}{command.usage}`")
        if command.aliases:
            lines.append("Aliases: " + ", ".join(f"`{prefix}{a}`" for a in command.aliases))
        return "\n".join(lines)

    lines = [f"- `{prefix}{c.usage or c.name}` - {c.help}" for c in ctx.bot.registry.visible()]
    body = "\n".join(lines)
    if ctx.bot.llm_enabled:
        body += (
            f"\n\nMention me (`@{ctx.bot.config.bot_name}`) or use "
            f"`{prefix}ai <question>` to talk to the model."
        )
    return f"**Commands**\n{body}"


@registry.command("ping", help="Check that I am awake.")
async def ping(ctx: Context) -> str:
    return "pong 🏓"


@registry.command("whoami", help="Show how I see you.")
async def whoami(ctx: Context) -> str:
    actor = ctx.event.actor
    kind = "bot" if actor.is_bot else "guest" if actor.is_guest else "user"
    return (
        f"You are **{actor.name or 'unknown'}** (`{actor.id}`), a {kind}"
        f"{f' with participant type {actor.participant_type}' if actor.participant_type else ''}, "
        f"in conversation `{ctx.event.room_token}`."
    )


@registry.command("echo", help="Repeat what you said.", usage="echo <text>")
async def echo(ctx: Context) -> str:
    if not ctx.args:
        raise CommandError("Give me something to echo.")
    return ctx.args


@registry.command(
    "ai", help="Ask the model a question.", usage="ai <question>", aliases=("ask",)
)
async def ai(ctx: Context) -> str | None:
    if not ctx.args:
        raise CommandError("Ask me something.")
    return await ctx.bot.answer_with_llm(ctx.event, ctx.args)


@registry.command("reset", help="Forget this conversation's history.", aliases=("forget",))
async def reset(ctx: Context) -> str:
    dropped = ctx.bot.history.clear(ctx.room_token)
    return f"Forgotten ({dropped} message{'s' if dropped != 1 else ''} dropped)."


@registry.command("version", help="Show my version and model.")
async def version(ctx: Context) -> str:
    from . import __version__

    model = ctx.bot.config.llm.model or "not configured"
    return f"sable {__version__} · model `{model}`"
