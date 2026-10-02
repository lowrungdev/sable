from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

import pytest

from sable import plugin_api
from sable.plugin_api import (
    Context,
    PluginActionError,
    PluginDeclarationError,
    PluginError,
    command,
    declarations,
    freeze,
    reset_declarations,
)


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    reset_declarations()
    yield
    reset_declarations()


class FakeTransport:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def act(self, action: str, args: Mapping[str, Any]) -> None:
        self.calls.append((action, dict(args)))
        if self.fail:
            raise PluginActionError(self.fail)


def make_context(transport: FakeTransport | None = None, **overrides: Any) -> Context:
    fields: dict[str, Any] = {
        "plugin": "demo",
        "trigger": "command",
        "name": "demo",
        "args": "a b",
        "argv": ["a", "b"],
        "room": "abcd1234",
        "actor_id": "alice",
        "actor_name": "Alice",
        "is_admin": False,
        "message_id": 7,
        "text": "!demo a b",
        "match": "",
        "settings": freeze({"k": [1, {"x": 2}]}),
        "log": logging.getLogger("sable.plugin.demo"),
        "_transport": transport,
    }
    fields.update(overrides)
    return Context(**fields)


# --- declarations ---------------------------------------------------------------------


async def test_command_registers_and_returns_the_function_unchanged() -> None:
    @command("weather", aliases=("wx", "forecast"), help="Forecast", usage="weather <city>")
    async def weather(ctx: Context) -> str | None:
        return "sun"

    found = declarations().commands
    assert len(found) == 1
    decl = found[0]
    assert (decl.name, decl.aliases, decl.help, decl.usage) == (
        "weather",
        ("wx", "forecast"),
        "Forecast",
        "weather <city>",
    )
    assert decl.handler is weather
    assert await weather(make_context()) == "sun"


def test_help_and_usage_are_optional_and_aliases_may_be_a_list() -> None:
    @command("ping", aliases=["p"])
    async def ping(ctx: Context) -> None:
        return None

    decl = declarations().commands[0]
    assert (decl.help, decl.usage, decl.aliases) == ("", "", ("p",))


def test_reset_clears_everything_so_the_same_names_can_be_declared_again() -> None:
    async def handler(ctx: Context) -> None:
        return None

    command("one", aliases=("uno",))(handler)
    reset_declarations()
    assert declarations().commands == []
    command("one", aliases=("uno",))(handler)
    assert [d.name for d in declarations().commands] == ["one"]


def test_declarations_is_a_snapshot() -> None:
    async def handler(ctx: Context) -> None:
        return None

    command("one")(handler)
    snapshot = declarations()
    snapshot.commands.clear()
    assert len(declarations().commands) == 1


@pytest.mark.parametrize(
    "name",
    ["", "Weather", "1abc", "-a", "_a", "a b", "a!", "x" * 33, "wéther", "!weather", "a\n"],
)
def test_bad_command_names_are_rejected(name: str) -> None:
    with pytest.raises(PluginDeclarationError, match="command name"):
        command(name)


def test_name_limits_are_inclusive() -> None:
    command("a" * 32)
    command("a")


def test_bad_alias_is_rejected_and_named() -> None:
    with pytest.raises(PluginDeclarationError, match="alias 'Bad'"):
        command("ok", aliases=("Bad",))


def test_aliases_must_not_be_a_bare_string() -> None:
    with pytest.raises(PluginDeclarationError, match="tuple"):
        command("ok", aliases="wx")  # type: ignore[arg-type]


def test_name_must_be_a_string_and_the_decorator_needs_brackets() -> None:
    with pytest.raises(PluginDeclarationError, match="must be a string"):
        command(5)  # type: ignore[arg-type]

    async def handler(ctx: Context) -> None:
        return None

    with pytest.raises(PluginDeclarationError, match="with brackets"):
        command(handler)  # type: ignore[arg-type]


def test_help_and_usage_limits() -> None:
    command("a", help="h" * 200, usage="u" * 100)
    with pytest.raises(PluginDeclarationError, match="help is 201"):
        command("b", help="h" * 201)
    with pytest.raises(PluginDeclarationError, match="usage is 101"):
        command("c", usage="u" * 101)


@pytest.mark.parametrize("bad", ["two\nlines", "tab\there", "bell\x07"])
def test_help_and_usage_must_be_one_line_of_plain_text(bad: str) -> None:
    with pytest.raises(PluginDeclarationError, match="single line"):
        command("a", help=bad)
    with pytest.raises(PluginDeclarationError, match="single line"):
        command("a", usage=bad)


def test_help_must_be_a_string() -> None:
    with pytest.raises(PluginDeclarationError, match="help must be a string"):
        command("a", help=3)  # type: ignore[arg-type]


def test_handler_must_be_a_coroutine_function() -> None:
    def sync(ctx: Context) -> str:
        return "x"

    with pytest.raises(PluginDeclarationError, match="async def"):
        command("a")(sync)  # type: ignore[arg-type]
    with pytest.raises(PluginDeclarationError, match="async def"):
        command("a")("nope")  # type: ignore[arg-type]
    assert declarations().commands == []


def test_handler_must_take_one_argument() -> None:
    async def none() -> None:
        return None

    async def two(ctx: Context, extra: int) -> None:
        return None

    for bad in (none, two):
        with pytest.raises(PluginDeclarationError, match="exactly one argument"):
            command("a")(bad)  # type: ignore[arg-type]
    assert declarations().commands == []


def test_duplicate_names_and_aliases_are_rejected_and_do_not_register() -> None:
    async def one(ctx: Context) -> None:
        return None

    async def two(ctx: Context) -> None:
        return None

    command("weather", aliases=("wx",))(one)
    with pytest.raises(PluginDeclarationError, match="'weather' is declared more than once"):
        command("weather")(two)
    with pytest.raises(PluginDeclarationError, match="'wx'"):
        command("other", aliases=("wx",))(two)
    # An alias that is another command's name collides too: one namespace.
    with pytest.raises(PluginDeclarationError, match="'weather'"):
        command("third", aliases=("weather",))(two)
    with pytest.raises(PluginDeclarationError, match="'same'"):
        command("same", aliases=("same",))(two)
    with pytest.raises(PluginDeclarationError, match="'dup'"):
        command("fresh", aliases=("dup", "dup"))(two)
    assert [d.name for d in declarations().commands] == ["weather"]
    # A rejected declaration must not have leaked its names: they are still free.
    command("other", aliases=("o",))(two)
    command("third")(two)


def test_at_most_32_handlers() -> None:
    async def handler(ctx: Context) -> None:
        return None

    for index in range(32):
        command(f"c{index}")(handler)
    with pytest.raises(PluginDeclarationError, match="at most 32"):
        command("one-too-many")(handler)
    assert plugin_api.declarations().count == 32


def test_declaration_error_is_a_value_error() -> None:
    assert issubclass(PluginDeclarationError, ValueError)
    assert issubclass(PluginError, Exception)
    assert not issubclass(PluginActionError, PluginError)


# --- freezing -------------------------------------------------------------------------


def test_freeze_is_deep_and_a_copy() -> None:
    original = {"a": [1, 2, {"b": [3]}], "c": {"d": "e"}, "n": None, "t": True}
    frozen = freeze(original)
    assert isinstance(frozen, MappingProxyType)
    assert frozen["a"] == (1, 2, frozen["a"][2])
    assert isinstance(frozen["a"][2], MappingProxyType)
    assert frozen["a"][2]["b"] == (3,)
    assert frozen["c"]["d"] == "e"
    assert frozen["n"] is None
    original["a"].append(99)
    original["c"]["d"] = "changed"
    assert frozen["a"][-1] != 99
    assert frozen["c"]["d"] == "e"
    for mutate in (
        lambda: frozen.__setitem__("x", 1),
        lambda: frozen["c"].__setitem__("d", "z"),
        lambda: frozen["a"].append(1),
    ):
        with pytest.raises((TypeError, AttributeError)):
            mutate()


# --- Context --------------------------------------------------------------------------


def test_context_is_read_only() -> None:
    ctx = make_context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.room = "elsewhere"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        del ctx.text
    with pytest.raises((AttributeError, TypeError)):
        ctx.something_new = 1  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        ctx.settings["k"] = 1  # type: ignore[index]
    assert ctx.settings["k"][1]["x"] == 2


def test_context_repr_hides_the_transport_and_logger() -> None:
    text = repr(make_context(FakeTransport()))
    assert "FakeTransport" not in text
    assert "Logger" not in text


async def test_reply_send_react_delegate_to_the_transport() -> None:
    transport = FakeTransport()
    ctx = make_context(transport)
    await ctx.reply("hi")
    await ctx.reply("quiet", silent=True)
    await ctx.send("other123", "over there", silent=True)
    await ctx.react("👍")
    assert transport.calls == [
        ("reply", {"text": "hi", "silent": False}),
        ("reply", {"text": "quiet", "silent": True}),
        ("send", {"room": "other123", "text": "over there", "silent": True}),
        ("react", {"emoji": "👍"}),
    ]


async def test_actions_reject_non_strings_before_reaching_the_transport() -> None:
    transport = FakeTransport()
    ctx = make_context(transport)
    with pytest.raises(TypeError, match="text must be a string"):
        await ctx.reply(5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="room must be a string"):
        await ctx.send(None, "x")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="emoji"):
        await ctx.react(None)  # type: ignore[arg-type]
    assert transport.calls == []


async def test_a_transport_failure_reaches_the_handler() -> None:
    ctx = make_context(FakeTransport(fail="not your room"))
    with pytest.raises(PluginActionError, match="not your room"):
        await ctx.send("nope", "x")


async def test_a_context_without_a_host_cannot_act() -> None:
    with pytest.raises(RuntimeError, match="not attached"):
        await make_context(None).reply("x")
