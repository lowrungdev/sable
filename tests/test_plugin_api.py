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
    check_phrases,
    command,
    declarations,
    fold,
    freeze,
    on_phrase,
    parse_cooldown,
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


# --------------------------------------------------------------------------- #
# @on_phrase
# --------------------------------------------------------------------------- #


async def _handler(ctx):
    return None


def test_a_phrase_handler_is_declared_with_its_defaults() -> None:
    @on_phrase(any=["gm", "  good morning  "])
    async def greet(ctx):
        return "hi"

    [decl] = declarations().phrases
    assert decl.id == "greet"
    assert decl.phrases == ("gm", "good morning")  # stripped, in the order given
    assert decl.whole_words is True
    assert decl.cooldown == 30
    assert decl.handler is greet  # returned unchanged


def test_every_option_can_be_set() -> None:
    @on_phrase(any=("a1b", "b2c"), whole_words=False, cooldown="2h")
    async def other(ctx):
        return None

    decl = declarations().phrases[0]
    assert decl.whole_words is False
    assert decl.cooldown == 7200
    assert decl.phrases == ("a1b", "b2c")


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (0, 0),
        (30, 30),
        (604800, 604800),
        ("0s", 0),
        ("30s", 30),
        ("5m", 300),
        ("1h", 3600),
        ("1d", 86400),
        ("7d", 604800),
        (" 5m ", 300),
        ("168h", 604800),
    ],
)
def test_cooldowns_parse(value, seconds) -> None:
    assert parse_cooldown(value) == seconds


@pytest.mark.parametrize(
    "value",
    [
        -1,
        604801,
        "8d",
        "169h",
        "10081m",
        "5",
        "m",
        "",
        "5x",
        "1h30m",
        "1.5h",
        "-5m",
        True,
        False,
        1.5,
        None,
        [30],
        b"5m",
        "5 m",
    ],
)
def test_bad_cooldowns_are_refused(value) -> None:
    with pytest.raises(PluginDeclarationError, match="cooldown"):
        parse_cooldown(value)


def test_the_cooldown_is_checked_at_decoration_time() -> None:
    with pytest.raises(PluginDeclarationError, match="cooldown"):
        on_phrase(any=["gm"], cooldown="soon")
    assert declarations().phrases == []


@pytest.mark.parametrize(
    "phrases",
    [
        None,
        "gm",
        [],
        ["a"],
        [" a "],
        ["x" * 101],
        ["ok", 5],
        ["ok", None],
        ["ok", ["no"]],
        ["line one\nline two"],
        ["tab\there"],
        ["nul\x00nul"],
        ["bell\x07"],
        ["sep\u2028sep"],
        [f"p{n}" for n in range(21)],
        5,
    ],
)
def test_bad_phrase_lists_are_refused_at_decoration_time(phrases) -> None:
    kwargs = {} if phrases is None else {"any": phrases}
    with pytest.raises(PluginDeclarationError):
        on_phrase(**kwargs)
    assert declarations().phrases == []


def test_twenty_phrases_of_a_hundred_characters_are_the_limit() -> None:
    @on_phrase(any=[f"{n:02d}" + "x" * 98 for n in range(20)])
    async def full(ctx):
        return None

    assert len(declarations().phrases[0].phrases) == 20
    assert all(len(p) == 100 for p in declarations().phrases[0].phrases)


def test_a_two_character_phrase_is_the_shortest() -> None:
    on_phrase(any=["gm"])
    with pytest.raises(PluginDeclarationError, match="2 to 100"):
        on_phrase(any=["g"])


def test_phrases_are_deduplicated_after_folding() -> None:
    @on_phrase(any=["GM", "gm", "\uff27\uff2d", "Gm", "good morning", "GOOD MORNING"])
    async def greet(ctx):
        return None

    assert declarations().phrases[0].phrases == ("GM", "good morning")


def test_unicode_and_emoji_phrases_are_fine() -> None:
    @on_phrase(
        any=[
            "\u00fcber",
            "\U0001f389\U0001f389",
            "\U0001f468\u200d\U0001f469\u200d\U0001f467",
            "a\u00a0b",
        ]
    )
    async def many(ctx):
        return None

    assert len(declarations().phrases[0].phrases) == 4


def test_whole_words_must_be_a_bool() -> None:
    for bad in ("yes", 1, None, 0):
        with pytest.raises(PluginDeclarationError, match="whole_words"):
            on_phrase(any=["gm"], whole_words=bad)  # type: ignore[arg-type]


def test_a_bare_decorator_is_a_clear_error() -> None:
    with pytest.raises(PluginDeclarationError, match="with brackets"):

        @on_phrase  # type: ignore[call-overload,misc]
        async def greet(ctx):
            return None


def test_positional_arguments_are_refused() -> None:
    with pytest.raises(PluginDeclarationError, match="needs arguments"):
        on_phrase(["gm"])  # type: ignore[call-overload]


def test_the_handler_must_be_async_and_take_the_context() -> None:
    with pytest.raises(PluginDeclarationError, match="async def"):

        @on_phrase(any=["gm"])
        def sync(ctx):
            return None

    with pytest.raises(PluginDeclarationError, match="exactly one argument"):

        @on_phrase(any=["gm"])
        async def two(ctx, extra):
            return None

    assert declarations().phrases == []


def test_a_handler_id_is_declared_once() -> None:
    @on_phrase(any=["gm"])
    async def greet(ctx):
        return None

    with pytest.raises(PluginDeclarationError, match="more than once"):
        on_phrase(any=["hello"])(greet)

    async def make():
        async def greet(ctx):
            return None

        return greet

    import asyncio

    second = asyncio.run(make())
    with pytest.raises(PluginDeclarationError, match="'greet'"):
        on_phrase(any=["hey"])(second)
    assert len(declarations().phrases) == 1


def test_a_handler_without_a_usable_name_is_refused() -> None:
    async def odd(ctx):
        return None

    odd.__name__ = "not an identifier"
    with pytest.raises(PluginDeclarationError, match="no usable name"):
        on_phrase(any=["gm"])(odd)
    odd.__name__ = "x" * 65
    with pytest.raises(PluginDeclarationError, match="no usable name"):
        on_phrase(any=["gm"])(odd)


def test_commands_and_phrases_share_the_cap_on_handlers() -> None:
    for number in range(31):

        @command(f"c{number}")
        async def one(ctx):
            return None

    @on_phrase(any=["gm"])
    async def last(ctx):
        return None

    assert plugin_api.declarations().count == 32
    with pytest.raises(PluginDeclarationError, match="at most 32"):

        @on_phrase(any=["hey"])
        async def too_many(ctx):
            return None


def test_a_rejected_phrase_leaves_the_registry_as_it_was() -> None:
    @on_phrase(any=["gm"])
    async def greet(ctx):
        return None

    snapshot = declarations()
    with pytest.raises(PluginDeclarationError):
        on_phrase(any=["x"])
    assert declarations().phrases == snapshot.phrases


def test_reset_forgets_phrase_ids_too() -> None:
    @on_phrase(any=["gm"])
    async def greet(ctx):
        return None

    reset_declarations()
    assert declarations().phrases == []
    on_phrase(any=["gm"])(greet)
    assert len(declarations().phrases) == 1


@pytest.mark.parametrize(
    ("text", "folded"),
    [
        ("Hello", "hello"),
        ("STRASSE", "strasse"),
        ("Stra\u00dfe", "strasse"),
        ("\ufb01nd", "find"),
        ("\uff27\uff2d", "gm"),
        ("e\u0301", "\u00e9"),
        ("\u03a3", "\u03c3"),
        ("\u2460", "1"),
    ],
)
def test_fold_is_nfkc_then_casefold(text, folded) -> None:
    assert fold(text) == folded


def test_a_single_emoji_is_too_short_for_a_phrase() -> None:
    # Two to a hundred characters, and one emoji is one character.
    with pytest.raises(PluginDeclarationError, match="2 to 100"):
        on_phrase(any=["\U0001f389"])


# --------------------------------------------------------------------------- #
# M3: a phrase too broad for whole_words=False is a declaration error
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("phrase", ["ab", ":)", "!!"])
def test_a_short_phrase_needs_whole_words_with_substring_matching(phrase) -> None:
    with pytest.raises(PluginDeclarationError, match="too broad"):
        on_phrase(any=[phrase], whole_words=False)
    assert declarations().phrases == []


@pytest.mark.parametrize("phrase", ["abc", "a1b", ":):)"])
def test_a_three_character_phrase_is_fine_with_whole_words_false(phrase) -> None:
    @on_phrase(any=[phrase], whole_words=False)
    async def handler(ctx):
        return None

    assert len(declarations().phrases) == 1


def test_a_short_phrase_is_fine_with_the_default_whole_words() -> None:
    @on_phrase(any=["gm"])
    async def handler(ctx):
        return None

    assert len(declarations().phrases) == 1


def test_only_the_short_phrase_in_a_mixed_list_is_rejected() -> None:
    with pytest.raises(PluginDeclarationError, match=r"'hi'.*too broad"):
        on_phrase(any=["hello", "hi"], whole_words=False)


# --------------------------------------------------------------------------- #
# L5: surrogates and unassigned code points are refused, not just control chars
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "phrase",
    [
        "\ud800y",  # a lone (high) surrogate
        "y\udfff",  # a lone (low) surrogate
        "\U0010fffex",  # an unassigned, non-character code point
        "x\U0010ffff",  # likewise
    ],
)
def test_surrogates_and_unassigned_code_points_are_refused(phrase) -> None:
    with pytest.raises(PluginDeclarationError):
        on_phrase(any=[phrase])


def test_a_phrase_with_a_surrogate_never_reaches_plugins_output(tmp_path) -> None:
    # check_phrases is what plugins.py's DeclaredPhrase re-validates against, so a
    # plugin cannot smuggle one past the core either: declaring one fails outright,
    # well before anything would try to render it in !plugins <name>.
    with pytest.raises(PluginDeclarationError):
        check_phrases(["\ud800y"])


# --------------------------------------------------------------------------- #
# L6: the minimum length (and "has a visible character") is checked AFTER folding
# --------------------------------------------------------------------------- #


def test_a_phrase_that_composes_down_to_one_character_is_refused() -> None:
    # "e" + combining acute accent: two characters before folding (passes the
    # pre-fold length check), but NFKC composes them into one precomposed "é".
    phrase = "é"
    assert len(phrase) == 2
    assert len(fold(phrase)) == 1
    with pytest.raises(PluginDeclarationError, match="once folded"):
        on_phrase(any=[phrase])
    assert declarations().phrases == []


def test_a_phrase_of_only_zero_width_joiners_is_refused() -> None:
    # Two ZERO WIDTH JOINER characters: two characters both before and after
    # folding (so the plain length checks alone would accept it), but neither one
    # is a character anybody could see or type.
    phrase = "‍‍"
    assert len(phrase) == len(fold(phrase)) == 2
    with pytest.raises(PluginDeclarationError, match="no visible character"):
        on_phrase(any=[phrase])


def test_a_phrase_of_only_combining_marks_with_no_base_is_refused() -> None:
    phrase = "́̂"  # two combining accents, nothing to combine with
    with pytest.raises(PluginDeclarationError, match="no visible character"):
        on_phrase(any=[phrase])


def test_a_phrase_with_one_visible_character_among_invisible_ones_is_fine() -> None:
    @on_phrase(any=["a‍‍"])
    async def handler(ctx):
        return None

    assert len(declarations().phrases) == 1


# --------------------------------------------------------------------------- #
# L9: cooldown digits are ASCII only
# --------------------------------------------------------------------------- #


def test_cooldown_digits_must_be_ascii() -> None:
    # U+0663 U+0660: ARABIC-INDIC DIGIT THREE, ZERO - Unicode decimal digits \d
    # matches without re.ASCII, but not what "30s" means to anyone reading a
    # settings file.
    with pytest.raises(PluginDeclarationError, match="cooldown"):
        parse_cooldown("٣٠s")  # noqa: RUF001 - the point is that these are digits
