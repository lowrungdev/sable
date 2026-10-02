"""Plugin phrase triggers: the matcher, the cooldowns, who may fire what, and what the
rate limit and the poller see. Driven by the fake worker, with a few tests at the end
against the real one."""

from __future__ import annotations

import json
import logging
import textwrap
import time
from typing import Any

import httpx
import pytest
import respx

from conftest import (
    ROOM,
    TALK,
    FakeLLM,
    as_bot,
    event,
    make_config,
    message_payload,
    reaction_event,
)
from plugin_helpers import message_route, posix_only, sent, texts, write_plugin
from sable.bot import Bot
from sable.commands import Registry
from sable.plugins import (
    MAX_COOLDOWN_ENTRIES,
    MAX_PHRASE_FIRES_PER_MESSAGE,
    PhraseHit,
    PhraseMatcher,
    PluginManager,
    Status,
    Worker,
    default_command,
    fold,
)

OTHER = "efgh5678"
THIRD = "zzzz9999"
ALICE = "users/alice"
MASER = "users/maser"


# --------------------------------------------------------------------------- #
# The matcher
# --------------------------------------------------------------------------- #


def found(phrases: list[str], text: str, *, whole: bool = True) -> str | None:
    return PhraseMatcher(phrases, whole).match(fold(text))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gm", True),
        ("GM", True),
        ("Gm everyone", True),
        ("say gm", True),
        ("gm!", True),
        ("gm, all", True),
        ("(gm)", True),
        ("gm.", True),
        ("gm\ngm", True),
        ("gmail", False),
        ("agm", False),
        ("gm_", False),
        ("_gm", False),
        ("gm2", False),
        ("2gm", False),
        ("gmgm", False),
        ("ggm", False),
        ("gmé", False),
        ("égm", False),
        ("g m", False),
        ("", False),
        ("   ", False),
    ],
)
def test_whole_words_means_not_touched_by_a_word_character(text, expected) -> None:
    assert (found(["gm"], text) is not None) is expected


@pytest.mark.parametrize("text", ["gmail", "agm", "gm", "xgmx", "g m"])
def test_without_whole_words_any_substring_matches(text) -> None:
    assert (found(["gm"], text, whole=False) is not None) is ("gm" in text)


@pytest.mark.parametrize(
    ("phrase", "text", "expected"),
    [
        (":)", "hi:)there", True),
        (":)", "x:)", True),
        ("c++", "love c++ a lot", True),
        ("c++", "abc++", False),  # the c is touched on the left
        ("c++", "c++x", True),  # the right edge is a +, so nothing can touch it
        ("c++", "c+", False),
        ("\U0001f389\U0001f389", "wow\U0001f389\U0001f389wow", True),
        ("\U0001f389\U0001f389", "\U0001f389", False),
        ("!!", "hey!!you", True),
        ("-x-", "a-x-b", True),
        ("hi there", "oh hi there you", True),
        ("hi there", "ohi there", False),
        ("hi there", "hi theres", False),
        ("hi there", "hi  there", False),  # literal: two spaces are not one
        ("a.b", "axb", False),
        ("a.b", "a.b", True),
    ],
)
def test_the_edges_of_the_phrase_decide_where_it_may_sit(phrase, text, expected) -> None:
    assert (found([phrase], text) is not None) is expected


@pytest.mark.parametrize(
    ("phrase", "text"),
    [
        ("STRASSE", "Straße is a street"),
        ("Straße", "STRASSE"),
        ("über", "ÜBER cool"),
        ("Über", "über cool"),
        ("find", "a ﬁnd"),  # a ligature
        ("ＧＭ", "gm all"),  # full-width letters  # noqa: RUF001
        ("gm", "ＧＭ all"),  # noqa: RUF001
        ("caf\u00e9", "cafe\u0301 au lait"),  # composed against decomposed
        ("cafe\u0301", "CAF\u00c9"),  # and the other way round
        ("σοφός", "ΣΟΦΌΣ"),  # Greek final sigma
    ],
)
def test_matching_is_nfkc_and_casefolded_on_both_sides(phrase, text) -> None:
    assert found([phrase], text) is not None


def test_the_phrase_that_matched_is_returned_as_declared() -> None:
    assert found(["Good Morning", "GM"], "oh, good morning!") == "Good Morning"
    assert found(["Good Morning", "GM"], "hi gm") == "GM"


def test_the_first_declared_phrase_wins_when_several_match() -> None:
    assert found(["hello", "world"], "hello world") == "hello"
    assert found(["world", "hello"], "hello world") == "world"


@pytest.mark.parametrize(
    ("phrase", "text", "expected"),
    [
        ("a.*b", "a-anything-b", False),
        ("a.*b", "a.*b", True),
        ("(a+)+$", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!", False),
        ("(a+)+$", "x (a+)+$ x", True),
        ("[abc]", "a", False),
        ("[abc]", "see [abc] here", True),
        ("^start", "start of line", False),
        ("^start", "^start", True),
        ("a|b", "a", False),
        ("a|b", "a|b", True),
        ("\\d+", "123", False),
        ("\\d+", "x \\d+ y", True),
        ("(?i)abc", "ABC", False),
    ],
)
def test_a_phrase_is_never_a_pattern(phrase, text, expected) -> None:
    assert (found([phrase], text, whole=False) is not None) is expected
    assert (found([phrase], text, whole=True) is not None) is expected


def test_overlong_and_empty_inputs_are_harmless() -> None:
    matcher = PhraseMatcher(["gm"], True)
    assert matcher.match("") is None
    assert matcher.match("x" * 1_000_000) is None
    assert PhraseMatcher([], True).match("gm") is None
    assert PhraseMatcher(["  "], True).match("gm") is None
    assert PhraseMatcher(["x" * 100], True).match("x") is None


ADVERSARIAL = {
    "long prefixes, absent": (["a" * 99 + c for c in "bcdefghijklmnopqrstu"], "a" * 32000),
    "long prefixes, present at the end": (
        ["a" * 99 + c for c in "bcdefghijklmnopqrstu"],
        "a" * 32000 + " " + " ".join("a" * 99 + c for c in "bcdefghijklmnopqrstu"),
    ),
    "non-word edges, present at the end": (
        [":" * 98 + c + "x" for c in "abcdefghijklmnopqrst"],
        ":" * 32000 + "".join(":" * 98 + c + "x" for c in "abcdefghijklmnopqrst"),
    ),
    "a word of near misses": (
        ["a" * 99 + c for c in "bcdefghijklmnopqrstu"],
        ("a" * 98 + " ") * 326,
    ),
    "short phrases everywhere": (
        ["aa", "aaa", "aaaa"] + ["a" * n + "!" for n in range(5, 22)],
        "a" * 32000,
    ),
    "overlapping periodic text": (["ab" * 49 + "c" + str(n) for n in range(20)], "ab" * 16000),
    "spaces": (["x " * 49 + "y" + str(n) for n in range(20)], "x " * 16000),
}


@pytest.mark.parametrize("whole", [True, False])
@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_an_adversarial_message_is_matched_in_well_under_fifty_milliseconds(name, whole) -> None:
    phrases, text = ADVERSARIAL[name]
    assert len(text) >= 32000 or "present" in name
    matcher = PhraseMatcher(phrases, whole)
    folded = fold(text)
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        matcher.match(folded)
        best = min(best, time.perf_counter() - started)
    assert best < 0.05, f"{name}: {best * 1000:.1f} ms"


def test_the_whole_path_including_folding_is_fast_too() -> None:
    matcher = PhraseMatcher(["ab" * 49 + "c" + str(n) for n in range(20)], True)
    text = "AB" * 16000
    started = time.perf_counter()
    matcher.match(fold(text))
    assert time.perf_counter() - started < 0.05


# --------------------------------------------------------------------------- #
# H1: a crafted run of combining marks must not stall normalisation
# --------------------------------------------------------------------------- #

#: One base character plus ~32,000 combining marks of alternating canonical
#: combining class. CPython's Unicode normalisation reorders combining marks by
#: class with an algorithm that is quadratic on a long run whose classes are not
#: already in order; a single repeated mark (all one class) does not trigger it,
#: which is why this alternates two marks with very different classes. Measured
#: before the fix: this took upwards of a second to fold, twice per event
#: (would_handle, then handle) - well over a second total.
_COMBINING_FLOOD = "a" + ("̴̡" * 16000)
assert len(_COMBINING_FLOOD) == 32001


def test_folding_a_crafted_combining_mark_flood_is_fast() -> None:
    from sable.plugins import _folded_for_matching

    started = time.perf_counter()
    _folded_for_matching(_COMBINING_FLOOD)
    assert time.perf_counter() - started < 0.05


def test_folding_the_same_message_twice_only_normalises_it_once(monkeypatch) -> None:
    """would_handle and handle both classify the same event; folding must happen at
    most once for it, cached on the event itself (H1-b)."""
    import unicodedata

    from sable.plugins import _folded_message

    calls = []
    real_normalize = unicodedata.normalize

    def counting(form, text):
        calls.append(len(text))
        return real_normalize(form, text)

    monkeypatch.setattr(unicodedata, "normalize", counting)
    incoming = event(_COMBINING_FLOOD)
    first = _folded_message(incoming)
    second = _folded_message(incoming)
    assert first == second
    assert len(calls) == 1  # only the first call actually normalised anything


# --------------------------------------------------------------------------- #
# Rigs
# --------------------------------------------------------------------------- #


def spec(
    handler: str = "greet",
    any: list[str] | None = None,
    *,
    whole_words: bool = True,
    cooldown: int = 30,
) -> dict[str, Any]:
    return {
        "id": handler,
        "any": any if any is not None else ["gm", "good morning"],
        "whole_words": whole_words,
        "cooldown": cooldown,
    }


def phrases_only(*phrases: dict[str, Any], behaviours: dict[str, str] | None = None, **extra: Any):
    """Settings for the fake worker: a plugin that declares only these phrase handlers."""
    settings: dict[str, Any] = {
        "declare": {"commands": [], "phrases": list(phrases), "schedules": []},
        **extra,
    }
    if behaviours:
        settings["phrase"] = behaviours
    return settings


@pytest.fixture
def worker_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every call made to any worker: (plugin, handler)."""
    calls: list[tuple[str, str]] = []
    original = Worker.call

    async def spy(self, handler, ctx, sink):
        calls.append((self.name, handler))
        return await original(self, handler, ctx, sink)

    monkeypatch.setattr(Worker, "call", spy)
    return calls


def spy_limiter(bot: Bot) -> list[str]:
    """Who the rate limiter was asked about, one entry per token taken."""
    tokens: list[str] = []
    original = bot._limiter.hit

    def hit(key: str) -> str:
        tokens.append(key)
        return original(key)

    bot._limiter.hit = hit  # type: ignore[method-assign]
    return tokens


def said(route) -> list[str]:
    return texts(route)


@respx.mock
async def test_a_combining_mark_flood_does_not_stall_would_handle_or_handle(rigs, tmp_path) -> None:
    """The exact repro: a ~32,000 character crafted message, in a room an eligible
    phrase plugin serves, must not stall would_handle or handle - no knowledge of
    the plugin's actual phrases is needed to trigger the old quadratic cost, since
    folding happened before any phrase was even looked at."""
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    message_route()
    incoming = event(_COMBINING_FLOOD, message_id=1)

    started = time.perf_counter()
    predicted = rig.bot.would_handle(incoming)
    would_handle_cost = time.perf_counter() - started

    started = time.perf_counter()
    await rig.bot.handle(incoming)
    handle_cost = time.perf_counter() - started

    assert would_handle_cost < 0.05, would_handle_cost
    assert handle_cost < 0.05, handle_cost
    assert predicted is False  # the flood does not contain "gm" or "good morning"


@respx.mock
async def test_a_phrase_fires_for_a_plain_message(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("well, good morning everybody"))
    assert said(route) == ["greeter/greet matched good morning"]


@respx.mock
async def test_a_plugin_that_declares_only_phrases_is_valid_and_has_no_commands(
    rigs, tmp_path
) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    assert rig.record("greeter").status is Status.ACTIVE
    assert rig.manager.commands() == []


@respx.mock
async def test_the_handler_is_called_with_the_phrase_context(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "greeter",
        settings=phrases_only(spec(), behaviours={"greet": "ctx"}, api_key="SECRET-VALUE-123"),
    )
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("Say GM to everyone", message_id=77, actor_id=MASER))
    ctx = json.loads(said(route)[0])
    assert ctx["trigger"] == "phrase"
    assert ctx["name"] == "greet"
    assert ctx["match"] == "gm"  # the phrase as declared, not as typed
    assert ctx["text"] == "Say GM to everyone"
    assert ctx["args"] == ""
    assert ctx["argv"] == []
    assert ctx["room"] == ROOM
    assert ctx["actor_id"] == MASER
    assert ctx["user_id"] == "maser"
    assert ctx["actor_name"] == "Alice"
    assert ctx["is_admin"] is True
    assert ctx["message_id"] == 77
    assert "SECRET-VALUE-123" not in str(ctx)


@respx.mock
async def test_the_worker_is_called_with_phrase_and_the_handler_id(
    rigs, tmp_path, worker_calls
) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec("morning")))
    rig = await rigs(tmp_path)
    message_route()
    await rig.bot.handle(event("gm"))
    assert worker_calls == [("greeter", "phrase:morning")]


@respx.mock
async def test_a_reply_threads_exactly_as_a_commands_does(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "greeter",
        settings={
            **phrases_only(spec()),
            "declare": {
                "commands": [{"name": "hi"}],
                "phrases": [spec()],
                "schedules": [],
            },
        },
    )
    rig = await rigs(tmp_path, reply_as_reply=True)
    route = message_route()
    await rig.bot.handle(event("gm", message_id=41))
    await rig.bot.handle(event("!hi echo x", message_id=42))
    bodies = sent(route)
    assert bodies[0]["replyTo"] == 41
    assert bodies[1]["replyTo"] == 42


@respx.mock
async def test_mass_mentions_in_a_phrase_reply_are_defanged(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path, "greeter", settings=phrases_only(spec(), behaviours={"greet": "mention"})
    )
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm"))
    assert "@all" not in said(route)[0]
    assert "@​all" in said(route)[0]


@respx.mock
async def test_a_handler_may_say_nothing(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec(), behaviours={"greet": "none"}))
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm"))
    assert route.call_count == 0


# --------------------------------------------------------------------------- #
# What a phrase does not listen to
# --------------------------------------------------------------------------- #


@respx.mock
async def test_ordinary_chatter_costs_nothing(rigs, tmp_path, worker_calls) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    route = message_route()
    tokens = spy_limiter(rig.bot)
    worker = rig.record("greeter").worker
    before = worker._last_id
    for text in ["hello there", "gmail is down", "good  morning", "nothing to see", "gm" * 5]:
        assert rig.bot.would_handle(event(text)) is False
        await rig.bot.handle(event(text))
    assert worker_calls == []
    assert worker._last_id == before  # not a single request went to the worker
    assert tokens == []
    assert route.call_count == 0
    assert rig.manager.cooldown_entries == 0


@respx.mock
async def test_a_command_never_fires_a_phrase(rigs, tmp_path, worker_calls) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!ping gm"))
    await rig.bot.handle(event("!help good morning", message_id=101))
    await rig.bot.handle(event("!nothing gm", message_id=102))
    assert worker_calls == []
    assert said(route)[0] == "pong \U0001f3d3"
    assert not any("matched" in t for t in said(route))


@respx.mock
async def test_a_message_to_the_bot_never_fires_a_phrase(rigs, tmp_path, worker_calls) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    route = message_route()
    mention = {"mention-user1": {"type": "user", "id": "sable", "name": "sable"}}
    for number, (text, parameters) in enumerate(
        [
            ("@sable good morning", None),
            ("sable: gm", None),
            ("{mention-user1} good morning", mention),
            ("good morning {mention-user1}", mention),
            ("hey @sable gm", None),
        ]
    ):
        await rig.bot.handle(
            event(text, message_id=200 + number, parameters=parameters)
            if parameters
            else event(text, message_id=200 + number)
        )
    assert worker_calls == []
    # They went to the model path as they always did; no model answers them here.
    assert not any("matched" in t for t in said(route))


@respx.mock
async def test_a_mention_still_goes_to_the_model_and_only_there(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("@sable good morning"))
    assert said(route) == ["mock answer"]


@respx.mock
async def test_a_reaction_never_fires_a_phrase(rigs, tmp_path, worker_calls) -> None:
    write_plugin(
        tmp_path, "greeter", settings=phrases_only(spec(any=["\U0001f44d\U0001f44d", "up"]))
    )
    rig = await rigs(tmp_path)
    route = message_route()
    assert rig.bot.would_handle(reaction_event("\U0001f44d")) is False
    await rig.bot.handle(reaction_event("\U0001f44d"))
    assert worker_calls == []
    assert route.call_count == 0


@respx.mock
async def test_a_system_message_never_fires_a_phrase(rigs, tmp_path, worker_calls) -> None:
    from sable.events import parse_message

    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    payload = message_payload("gm", message_id=300)
    payload["messageType"] = "system"
    payload["systemMessage"] = "user_added"
    parsed = parse_message(payload, room_name="x")
    assert parsed is None or rig.bot.would_handle(parsed) is False
    assert worker_calls == []


@respx.mock
async def test_in_an_ai_room_the_model_and_a_phrase_both_answer(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path, ai_rooms=[ROOM])
    route = message_route()
    tokens = spy_limiter(rig.bot)
    await rig.bot.handle(event("good morning team"))
    assert sorted(said(route)) == ["greeter/greet matched good morning", "mock answer"]
    assert len(tokens) == 1  # one event, one token, whatever it triggered


@respx.mock
async def test_in_an_ai_room_chatter_still_goes_to_the_model_alone(
    rigs, tmp_path, worker_calls
) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path, ai_rooms=[ROOM])
    route = message_route()
    await rig.bot.handle(event("just chatting"))
    assert said(route) == ["mock answer"]
    assert worker_calls == []


@respx.mock
async def test_an_ai_room_message_that_is_a_command_does_not_fire_phrases(
    rigs, tmp_path, worker_calls
) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path, ai_rooms=[ROOM])
    route = message_route()
    await rig.bot.handle(event("!ping gm"))
    assert said(route) == ["pong \U0001f3d3"]
    assert worker_calls == []


# --------------------------------------------------------------------------- #
# Who may fire what
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_access_rules_decide_silently_who_fires_what(
    rigs, tmp_path, worker_calls
) -> None:
    def plugin(number: int, **access: Any) -> None:
        write_plugin(
            tmp_path,
            f"p{number}",
            settings=phrases_only(spec("h", [f"kw{number}x"], cooldown=0)),
            **access,
        )

    plugin(0)  # open, in ROOM
    plugin(1, rooms=[OTHER])  # not in ROOM
    plugin(2, rooms=None)  # inactive
    plugin(3, enabled=False)  # disabled
    plugin(4, users=["bob"])
    plugin(5, admins_only=True)
    plugin(6, rooms=["*"])
    plugin(7, users=["alice"])
    plugin(8, rooms=[ROOM, OTHER])
    rig = await rigs(
        tmp_path,
        admin_users=["maser"],
        ignore_users=["spam"],
        allowed_rooms=[ROOM, OTHER],
        rate_limit=0,
    )
    routes = {room: message_route(room) for room in (ROOM, OTHER, THIRD)}
    cases = [
        # plugin, who, room, fires
        (0, ALICE, ROOM, True),
        (0, "guests/7f3c9a2b", ROOM, True),
        (0, "federated_users/karl@cloud.example.net", ROOM, True),
        (0, MASER, ROOM, True),
        (0, ALICE, OTHER, False),
        (1, ALICE, ROOM, False),
        (1, ALICE, OTHER, True),
        (2, ALICE, ROOM, False),
        (3, ALICE, ROOM, False),
        (4, ALICE, ROOM, False),
        (4, "users/bob", ROOM, True),
        (4, "guests/7f3c9a2b", ROOM, False),
        (5, ALICE, ROOM, False),
        (5, MASER, ROOM, True),
        (6, ALICE, ROOM, True),
        (6, ALICE, OTHER, True),
        (6, ALICE, THIRD, False),  # not a room sable follows (SABLE_ALLOWED_ROOMS)
        (7, ALICE, ROOM, True),
        (7, MASER, ROOM, False),  # an administrator is not implicitly on a list
        (8, ALICE, OTHER, True),
        (0, "users/spam", ROOM, False),  # ignored
        (0, "users/sable", ROOM, False),  # ourselves
        (0, "bots/relay", ROOM, False),
    ]
    wrong = []
    for number, (n, who, room, fires) in enumerate(cases):
        before = {r: route.call_count for r, route in routes.items()}
        incoming = event(f"hello kw{n}x", actor_id=who, room=room, message_id=1000 + number)
        predicted = rig.bot.would_handle(incoming)
        await rig.bot.handle(incoming)
        replies = [
            body["message"] for r, route in routes.items() for body in sent(route)[before[r] :]
        ]
        did = replies == [f"p{n}/h matched kw{n}x"]
        if did is not fires or predicted is not fires or (replies and not fires):
            wrong.append((n, who, room, fires, predicted, replies))
    assert not wrong, wrong
    # Nobody was told anything about the ones that did not fire, and the workers that
    # were never allowed to hear the message did not.
    assert not any(plugin in {"p2", "p3"} for plugin, _ in worker_calls)


@respx.mock
async def test_a_plugin_that_is_switched_off_never_fires(rigs, tmp_path, worker_calls) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec(cooldown=0)))
    rig = await rigs(tmp_path)
    route = message_route()
    worker = rig.record("greeter").worker
    worker._tripped_until = rig.clock() + 300  # the breaker is open
    assert rig.bot.would_handle(event("gm")) is False
    await rig.bot.handle(event("gm"))
    assert worker_calls == []
    assert route.call_count == 0
    rig.clock.advance(301)  # after the cooldown the next message is the trial
    assert rig.bot.would_handle(event("gm")) is True


@respx.mock
async def test_a_plugin_that_is_not_loaded_never_fires(rigs, tmp_path, worker_calls) -> None:
    write_plugin(tmp_path, "greeter", settings={**phrases_only(spec()), "load": "error"})
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm"))
    assert worker_calls == []
    assert route.call_count == 0


@respx.mock
async def test_several_plugins_may_listen_for_the_same_phrase(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "alpha", directory="a", settings=phrases_only(spec("one", ["gm"])))
    write_plugin(tmp_path, "beta", directory="b", settings=phrases_only(spec("one", ["gm"])))
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm"))
    assert said(route) == ["alpha/one matched gm", "beta/one matched gm"] or sorted(
        said(route)
    ) == ["alpha/one matched gm", "beta/one matched gm"]
    assert rig.record("alpha").status is Status.ACTIVE
    assert rig.record("beta").status is Status.ACTIVE


# --------------------------------------------------------------------------- #
# Cooldowns
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_handler_is_quiet_for_its_cooldown_then_speaks_again(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec(cooldown=60)))
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm", message_id=1))
    await rig.bot.handle(event("gm again", message_id=2))
    rig.clock.advance(59)
    await rig.bot.handle(event("gm still", message_id=3))
    assert len(said(route)) == 1
    rig.clock.advance(2)
    await rig.bot.handle(event("gm now", message_id=4))
    assert len(said(route)) == 2
    await rig.bot.handle(event("gm too soon", message_id=5))
    assert len(said(route)) == 2


@respx.mock
async def test_a_cooldown_of_zero_means_every_time(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec(cooldown=0)))
    rig = await rigs(tmp_path)
    route = message_route()
    for number in range(4):
        await rig.bot.handle(event("gm", message_id=number + 1))
    assert len(said(route)) == 4
    assert rig.manager.cooldown_entries == 0


@respx.mock
async def test_the_cooldown_is_per_room(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", rooms=[ROOM, OTHER], settings=phrases_only(spec(cooldown=60)))
    rig = await rigs(tmp_path)
    here = message_route()
    there = message_route(OTHER)
    await rig.bot.handle(event("gm", message_id=1))
    await rig.bot.handle(event("gm", room=OTHER, message_id=2))
    await rig.bot.handle(event("gm", message_id=3))
    await rig.bot.handle(event("gm", room=OTHER, message_id=4))
    assert len(said(here)) == 1
    assert len(said(there)) == 1


@respx.mock
async def test_the_cooldown_is_per_handler_and_ignores_who_spoke(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "greeter",
        settings=phrases_only(
            spec("slow", ["gm"], cooldown=600), spec("fast", ["gm"], cooldown=10)
        ),
    )
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm", message_id=1))
    assert sorted(said(route)) == ["greeter/fast matched gm", "greeter/slow matched gm"]
    rig.clock.advance(11)
    await rig.bot.handle(event("gm", actor_id="users/bob", message_id=2))
    assert said(route)[-1] == "greeter/fast matched gm"
    assert len(said(route)) == 3


@respx.mock
async def test_the_cooldown_is_per_plugin(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path, "alpha", directory="a", settings=phrases_only(spec("h", ["gm"], cooldown=60))
    )
    write_plugin(
        tmp_path, "beta", directory="b", settings=phrases_only(spec("h", ["gm"], cooldown=5))
    )
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm", message_id=1))
    rig.clock.advance(6)
    await rig.bot.handle(event("gm", message_id=2))
    assert sorted(said(route)) == [
        "alpha/h matched gm",
        "beta/h matched gm",
        "beta/h matched gm",
    ]


@respx.mock
async def test_would_handle_sees_a_cooldown_but_never_starts_one(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec(cooldown=60)))
    rig = await rigs(tmp_path)
    route = message_route()
    for _ in range(5):
        assert rig.bot.would_handle(event("gm")) is True
    assert rig.manager.cooldown_entries == 0
    await rig.bot.handle(event("gm", message_id=1))
    assert rig.manager.cooldown_entries == 1
    for _ in range(5):
        assert rig.bot.would_handle(event("gm")) is False
    rig.clock.advance(61)
    assert rig.bot.would_handle(event("gm")) is True
    assert len(said(route)) == 1


def hit(plugin: str = "p", handler: str = "h", room: str = ROOM, cooldown: int = 60) -> PhraseHit:
    return PhraseHit(plugin, handler, "gm", cooldown, room)


async def test_a_claimed_handler_cannot_be_claimed_again_until_it_expires(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    manager = rig.manager
    assert manager.claim_phrase(hit()) is True
    assert manager.claim_phrase(hit()) is False
    assert manager.claim_phrase(hit(room=OTHER)) is True
    assert manager.claim_phrase(hit(handler="other")) is True
    assert manager.claim_phrase(hit(plugin="q")) is True
    rig.clock.advance(60)
    assert manager.claim_phrase(hit()) is True


async def test_the_table_of_cooldowns_is_bounded_and_drops_the_expired_first(
    rigs, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("sable.plugins.MAX_COOLDOWN_ENTRIES", 4)
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    manager = rig.manager
    for room in ("aaaa", "bbbb"):
        manager.claim_phrase(hit(room=room, cooldown=10))
    for room in ("cccc", "dddd"):
        manager.claim_phrase(hit(room=room, cooldown=1000))
    assert manager.cooldown_entries == 4
    rig.clock.advance(11)
    manager.claim_phrase(hit(room="eeee", cooldown=1000))
    # The two that had expired went; the older but live ones stayed.
    assert manager.cooldown_entries == 3
    assert manager.claim_phrase(hit(room="cccc", cooldown=1000)) is False
    assert manager.claim_phrase(hit(room="aaaa", cooldown=1000)) is True


async def test_with_nothing_expired_the_oldest_cooldown_goes(rigs, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sable.plugins.MAX_COOLDOWN_ENTRIES", 3)
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    manager = rig.manager
    for room in ("aaaa", "bbbb", "cccc", "dddd", "eeee"):
        manager.claim_phrase(hit(room=room, cooldown=1000))
    assert manager.cooldown_entries == 3
    assert manager.claim_phrase(hit(room="aaaa", cooldown=1000)) is True  # evicted
    assert manager.claim_phrase(hit(room="eeee", cooldown=1000)) is False  # still cooling


async def test_eviction_is_by_nearest_expiry_not_by_insertion_order(
    rigs, tmp_path, monkeypatch
) -> None:
    """L8: a long cooldown claimed early must not be evicted ahead of a short one
    claimed later just because it is older. Eviction picks whichever remaining entry
    is soonest to expire, regardless of when it was inserted."""
    monkeypatch.setattr("sable.plugins.MAX_COOLDOWN_ENTRIES", 2)
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    manager = rig.manager
    # Inserted first (oldest by insertion), but expires LAST (a long cooldown).
    manager.claim_phrase(hit(room="long-lived", cooldown=1000))
    # Inserted after it, but expires FIRST (a short cooldown).
    manager.claim_phrase(hit(room="short-lived", cooldown=5))
    assert manager.cooldown_entries == 2
    # Over the cap: the entry nearest to expiry goes, not the one inserted first.
    manager.claim_phrase(hit(room="third", cooldown=1000))
    assert manager.cooldown_entries == 2
    assert manager.claim_phrase(hit(room="short-lived", cooldown=1)) is True  # evicted: fires
    assert manager.claim_phrase(hit(room="long-lived", cooldown=1)) is False  # still cooling


async def test_a_busy_table_never_passes_its_cap(rigs, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sable.plugins.MAX_COOLDOWN_ENTRIES", 50)
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    for number in range(500):
        rig.manager.claim_phrase(hit(room=f"r{number:04d}", cooldown=1000))
        assert rig.manager.cooldown_entries <= 50


def test_the_real_cap_is_ten_thousand() -> None:
    assert MAX_COOLDOWN_ENTRIES == 10_000
    assert MAX_PHRASE_FIRES_PER_MESSAGE == 3


# --------------------------------------------------------------------------- #
# The rate limit, and would_handle against handle
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_phrase_never_touches_the_rate_limiter(rigs, tmp_path) -> None:
    """An ambient phrase match - firing, cooling, or matching nothing - must never
    cost the sender a token: a broad, cooldown-0 phrase in some plugin must not be
    able to exhaust somebody's budget for a real command (see M2)."""
    write_plugin(
        tmp_path,
        "greeter",
        settings=phrases_only(spec("one", ["gm"]), spec("two", ["gm"]), spec("three", ["gm"])),
    )
    rig = await rigs(tmp_path)
    message_route()
    tokens = spy_limiter(rig.bot)
    await rig.bot.handle(event("gm", message_id=1))  # three handlers fire
    assert tokens == []
    await rig.bot.handle(event("gm", message_id=2))  # all three cooling
    assert tokens == []
    await rig.bot.handle(event("chatter", message_id=3))  # matches nothing
    assert tokens == []


@respx.mock
async def test_a_phrase_fires_even_when_the_senders_command_budget_is_exhausted(
    rigs, tmp_path
) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec(cooldown=0)))
    rig = await rigs(tmp_path, rate_limit=1)
    route = message_route()
    await rig.bot.handle(event("!ping", message_id=1))  # alice's only token this minute
    await rig.bot.handle(event("gm", message_id=2))  # a phrase: the limiter never sees it
    assert said(route) == ["pong \U0001f3d3", "greeter/greet matched gm"]


@respx.mock
async def test_a_phrase_cooldown_is_shared_by_everyone_in_the_room(rigs, tmp_path) -> None:
    """Cooldowns are per room, not per person (see the module docstring): once fired
    for alice, the same handler is quiet for bob too, in that room, for its cooldown -
    whatever either of their rate-limit budgets looks like."""
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec(cooldown=600)))
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm", message_id=1))
    await rig.bot.handle(event("gm", actor_id="users/bob", message_id=2))
    assert said(route) == ["greeter/greet matched gm"]


@respx.mock
async def test_chatter_does_not_use_up_somebodys_tokens(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path, rate_limit=1)
    route = message_route()
    for number in range(10):
        await rig.bot.handle(event("blah blah", message_id=number + 1))
    await rig.bot.handle(event("gm", message_id=50))
    assert said(route) == ["greeter/greet matched gm"]


@respx.mock
async def test_at_most_three_handlers_fire_for_one_message(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "zeta",
        directory="z",
        settings=phrases_only(*(spec(f"h{n}", ["gm"]) for n in (3, 1, 2))),
    )
    write_plugin(
        tmp_path,
        "alpha",
        directory="a",
        settings=phrases_only(*(spec(f"g{n}", ["gm"]) for n in (2, 1))),
    )
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm", message_id=1))
    # Plugin name, then handler id: alpha's two, then zeta's first.
    assert sorted(said(route)) == [
        "alpha/g1 matched gm",
        "alpha/g2 matched gm",
        "zeta/h1 matched gm",
    ]
    # The two that did not fire did not start a cooldown, so they are next.
    await rig.bot.handle(event("gm", message_id=2))
    assert sorted(said(route)[3:]) == ["zeta/h2 matched gm", "zeta/h3 matched gm"]
    await rig.bot.handle(event("gm", message_id=3))
    assert len(said(route)) == 5


@respx.mock
async def test_the_cap_round_robins_across_plugins_so_none_is_starved(rigs, tmp_path) -> None:
    """M1: before the fix, sorting hits flat by (plugin, handler) meant a plugin named
    early with several handlers on a broad, cooldown-0 phrase could fill the whole
    cap itself, starving every other plugin's handler for that phrase forever. Here
    "aaa" (3 handlers) sorts before "zzz" (1 handler); zzz must still fire, every
    single time, not just occasionally."""
    write_plugin(
        tmp_path,
        "aaa",
        directory="a",
        settings=phrases_only(
            spec("h1", ["gm"], cooldown=0),
            spec("h2", ["gm"], cooldown=0),
            spec("h3", ["gm"], cooldown=0),
        ),
    )
    write_plugin(
        tmp_path, "zzz", directory="z", settings=phrases_only(spec("h1", ["gm"], cooldown=0))
    )
    rig = await rigs(tmp_path)
    route = message_route()
    for number in range(10):
        await rig.bot.handle(event("gm", message_id=number + 1))
    fired = said(route)
    assert len(fired) == 30  # the cap (3), every one of the 10 messages
    for batch in range(0, 30, 3):
        assert "zzz/h1 matched gm" in fired[batch : batch + 3], (batch, fired[batch : batch + 3])


@respx.mock
async def test_would_handle_agrees_with_handle_for_every_kind_of_event(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "greeter",
        users=["alice", "bob"],
        settings=phrases_only(spec(cooldown=20)),
    )
    rig = await rigs(tmp_path, admin_users=["maser"], ai_rooms=[OTHER])
    message_route()
    message_route(OTHER)
    spy = {"asked": 0}
    original = rig.bot._rate_limited

    def counting(incoming):
        spy["asked"] += 1
        return original(incoming)

    rig.bot._rate_limited = counting  # type: ignore[method-assign]
    mention = {"mention-user1": {"type": "user", "id": "sable", "name": "sable"}}
    events = [
        event("gm", message_id=1),
        event("gm", message_id=2),  # cooling now
        event("hello", message_id=3),
        event("!ping", message_id=4),
        event("!ping gm", message_id=5),
        event("@sable hello", message_id=6),
        event("{mention-user1} gm", message_id=7, parameters=mention),
        event("gm", actor_id=MASER, message_id=8),  # not on the list
        event("gm", actor_id="users/bob", message_id=9),  # cooling
        event("gm", room=OTHER, message_id=10),  # not its room, but an AI room
        event("gm", actor_id="bots/relay", message_id=11),
        event("gm", actor_id="guests/7f3c9a2b", message_id=12),
        reaction_event("\U0001f44d"),
        event("", message_id=13),
        event("   ", message_id=14),
    ]
    for step, incoming in enumerate(events):
        if step == 3:
            rig.clock.advance(21)  # the first cooldown is over
        predicted = rig.bot.would_handle(incoming)
        again = rig.bot.would_handle(incoming)
        route = rig.bot._route(incoming) if rig.bot._screen(incoming) else None
        spy["asked"] = 0
        await rig.bot.handle(incoming)
        assert predicted is again, step
        if not predicted:
            assert spy["asked"] == 0, (step, spy)
        elif route is not None and route.kind == "phrase":
            # An ambient phrase match never touches the rate limiter (see M2).
            assert spy["asked"] == 0, (step, spy)
        else:
            assert spy["asked"] == 1, (step, spy)


# --------------------------------------------------------------------------- #
# The poller
# --------------------------------------------------------------------------- #


async def test_the_poller_spawns_nothing_for_chatter_and_one_task_for_a_match(
    rigs, tmp_path
) -> None:
    from sable.poller import Poller

    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path)
    spawned: list[Any] = []

    def spawn(coroutine) -> None:
        spawned.append(coroutine)
        coroutine.close()

    poller = Poller(rig.bot, spawn)
    poller._dispatch(ROOM, message_payload("just chatting"))
    poller._dispatch(ROOM, message_payload("gmail", message_id=101))
    assert spawned == []
    poller._dispatch(ROOM, message_payload("gm all", message_id=102))
    assert len(spawned) == 1
    assert rig.manager.cooldown_entries == 0  # asking started nothing


# --------------------------------------------------------------------------- #
# Failures are for the log
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.parametrize("behaviour", ["crash", "exception", "error"])
async def test_a_failing_phrase_handler_is_never_posted(rigs, tmp_path, caplog, behaviour) -> None:
    write_plugin(
        tmp_path, "greeter", settings=phrases_only(spec(), behaviours={"greet": behaviour})
    )
    rig = await rigs(tmp_path)
    route = message_route()
    with caplog.at_level(logging.INFO):
        await rig.bot.handle(event("gm"))
    assert route.call_count == 0
    if behaviour == "error":
        # The handler's own PluginError, at INFO, with its text.
        record = next(r for r in caplog.records if "no thanks" in r.getMessage())
        assert record.levelno == logging.INFO
        assert "plugin greeter: phrase handler greet said: no thanks" in caplog.text
    else:
        record = next(r for r in caplog.records if "phrase handler greet failed" in r.getMessage())
        assert record.levelno == logging.WARNING
        assert "greeter" in record.getMessage()


@respx.mock
async def test_a_phrase_errors_log_line_is_redacted_and_one_lined(rigs, tmp_path, caplog) -> None:
    """L1: a phrase handler's own PluginError is never posted (nobody asked it
    anything), so it only ever reaches the log - which must get the same treatment
    as any other worker-authored text: the plugin's own settings values blanked, and
    no embedded newline able to forge a second log line."""
    secret = "hunter2-SECRET-VALUE"
    write_plugin(
        tmp_path,
        "greeter",
        settings=phrases_only(spec(), behaviours={"greet": "leak"}, api_key=secret),
    )
    rig = await rigs(tmp_path)
    route = message_route()
    with caplog.at_level(logging.INFO):
        await rig.bot.handle(event("gm"))
    assert route.call_count == 0
    assert secret not in caplog.text
    line = next(m for m in caplog.messages if "phrase handler greet said" in m)
    assert line == "plugin greeter: phrase handler greet said: bad key *** FAKE LOG LINE: pwned"
    # One line: the embedded "\n" became a space, so it cannot forge a second line.
    assert "\n" not in line


@respx.mock
async def test_a_phrase_handler_that_times_out_is_not_posted(rigs, tmp_path, caplog) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec(), behaviours={"greet": "hang"}))
    rig = await rigs(tmp_path, call_timeout=0.3)
    route = message_route()
    with caplog.at_level(logging.WARNING):
        await rig.bot.handle(event("gm"))
    assert route.call_count == 0
    assert "took too long" in caplog.text


@respx.mock
async def test_a_failing_handler_does_not_stop_the_others(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "alpha",
        directory="a",
        settings=phrases_only(spec("h", ["gm"]), behaviours={"h": "crash"}),
    )
    write_plugin(tmp_path, "beta", directory="b", settings=phrases_only(spec("h", ["gm"])))
    write_plugin(
        tmp_path,
        "gamma",
        directory="c",
        settings=phrases_only(spec("h", ["gm"]), behaviours={"h": "exception"}),
    )
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("gm"))
    assert said(route) == ["beta/h matched gm"]


@respx.mock
async def test_a_failure_does_not_depend_on_report_errors(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec(), behaviours={"greet": "crash"}))
    rig = await rigs(tmp_path, report_errors=True)
    route = message_route()
    await rig.bot.handle(event("gm"))
    assert route.call_count == 0


@respx.mock
async def test_repeated_crashes_switch_a_phrase_plugin_off_like_any_other(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path, "greeter", settings=phrases_only(spec(cooldown=0), behaviours={"greet": "crash"})
    )
    rig = await rigs(tmp_path)
    route = message_route()
    for number in range(8):
        await rig.bot.handle(event("gm", message_id=number + 1))
    worker = rig.record("greeter").worker
    assert worker.tripped
    assert rig.bot.would_handle(event("gm")) is False
    assert route.call_count == 0
    assert rig.record("greeter").state.startswith("switched off")


# --------------------------------------------------------------------------- #
# Declarations as the core accepts them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("handler", "why"),
    [
        (spec(any=[]), "at least 1"),
        (spec(any=["x" * 3] * 0 + [f"p{n}" for n in range(21)]), "at most 20"),
        (spec(any=["a"]), "2 to 100"),
        (spec(any=["x" * 101]), "2 to 100"),
        (spec(any=["bad\nline"]), "control character"),
        (spec(any=["ok", 5]), "string"),
        (spec(cooldown=-1), "greater than or equal to 0"),
        (spec(cooldown=604801), "less than or equal to 604800"),
        ({**spec(), "cooldown": "5m"}, "valid integer"),
        ({**spec(), "cooldown": 1.5}, "valid integer"),
        ({**spec(), "whole_words": "yes"}, "valid boolean"),
        ({**spec(), "id": "not an id"}, "legal handler id"),
        ({**spec(), "id": ""}, "legal handler id"),
        ({k: v for k, v in spec().items() if k != "id"}, "id"),
        ({k: v for k, v in spec().items() if k != "any"}, "any"),
        # M3: too broad for substring matching. A hostile worker is re-checked by the
        # core the same as an honest one is by @on_phrase.
        (spec(any=["ab"], whole_words=False), "too broad"),
        (spec(any=[":)"], whole_words=False), "too broad"),
        # L5: a lone surrogate or an unassigned code point, not just control chars.
        (spec(any=["\ud800y"]), "surrogate or unassigned"),
        (spec(any=["x\U0010ffff"]), "surrogate or unassigned"),
        # L6: the minimum length (and "has a visible character") checked after
        # folding, not before: "e" + a combining acute composes to one character.
        (spec(any=["é"]), "once folded"),
        (spec(any=["‍‍"]), "no visible character"),
    ],
)
async def test_an_invalid_phrase_declaration_fails_the_plugin(rigs, tmp_path, handler, why) -> None:
    write_plugin(tmp_path, "liar", settings=phrases_only(handler))
    rig = await rigs(tmp_path)
    record = rig.record("liar")
    assert record.status is Status.FAILED
    assert why in record.reason


async def test_a_handler_id_may_not_repeat(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path, "liar", settings=phrases_only(spec("same", ["gm"]), spec("same", ["hi"]))
    )
    rig = await rigs(tmp_path)
    assert rig.record("liar").status is Status.FAILED
    assert "declared twice" in rig.record("liar").reason


async def test_the_core_deduplicates_phrases_too(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "liar", settings=phrases_only(spec("h", ["GM", "gm", "ＧＭ", "hello"])))  # noqa: RUF001
    rig = await rigs(tmp_path)
    assert rig.record("liar").declared.phrases[0].any == ["GM", "hello"]


async def test_the_cooldown_on_the_wire_defaults_to_thirty_seconds(rigs, tmp_path) -> None:
    handler = {"id": "h", "any": ["gm"]}
    write_plugin(tmp_path, "greeter", settings=phrases_only(handler))
    rig = await rigs(tmp_path)
    declared = rig.record("greeter").declared.phrases[0]
    assert declared.cooldown == 30
    assert declared.whole_words is True


async def test_more_than_thirty_two_handlers_fail_the_plugin(rigs, tmp_path) -> None:
    handlers = [spec(f"h{n:02d}", [f"w{n:02d}"]) for n in range(33)]
    write_plugin(tmp_path, "many", settings=phrases_only(*handlers))
    rig = await rigs(tmp_path)
    assert rig.record("many").status is Status.FAILED


# --------------------------------------------------------------------------- #
# !plugins
# --------------------------------------------------------------------------- #


@respx.mock
async def test_plugins_lists_the_phrase_handlers_and_describes_them(rigs, tmp_path) -> None:
    secret = "hunter2-SECRET-VALUE"
    write_plugin(
        tmp_path,
        "greeter",
        settings=phrases_only(
            spec("greet", ["gm", "Good `Morning`"], cooldown=3600),
            spec("loose", ["hey"], whole_words=False, cooldown=0),
            api_key=secret,
        ),
    )
    write_plugin(tmp_path, "quiet", rooms=None, settings=phrases_only(spec(any=["secret phrase"])))
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins", actor_id=MASER, message_id=1))
    await rig.bot.handle(event("!plugins greeter", actor_id=MASER, message_id=2))
    await rig.bot.handle(event("!plugins quiet", actor_id=MASER, message_id=3))
    listing, detail, inactive = said(route)
    assert "`greeter` - active - phrases: `greet`, `loose`" in listing
    assert (
        'Phrase handler `greet`: "gm", "Good \'Morning\'" (whole words, cooldown 3600s)' in detail
    )
    assert 'Phrase handler `loose`: "hey" (anywhere in a word, cooldown 0s)' in detail
    # An inactive plugin was never started, so it shows no phrases at all.
    assert "secret phrase" not in listing + inactive
    assert "inactive: no rooms set" in inactive
    for body in said(route):
        assert secret not in body
    assert secret not in "\n".join(rig.manager.check_lines())
    assert "phrases: greet, loose" in "\n".join(rig.manager.check_lines())


@respx.mock
async def test_a_switched_off_plugin_describes_no_phrases(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=phrases_only(spec()))
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    rig.record("greeter").worker._tripped_until = rig.clock() + 300
    await rig.bot.handle(event("!plugins greeter", actor_id=MASER))
    assert "Phrase handler" not in said(route)[0]
    assert "switched off" in said(route)[0]


# --------------------------------------------------------------------------- #
# The real worker
# --------------------------------------------------------------------------- #

GREETER = textwrap.dedent(
    """
    from sable.plugin_api import PluginError, command, on_phrase


    @on_phrase(any=["good morning", "gm"], whole_words=True, cooldown="1h")
    async def greet(ctx):
        await ctx.react("\\U0001f44b")
        return f"Good morning, {ctx.actor_name}! (you said {ctx.match!r}, trigger {ctx.trigger})"


    @on_phrase(any=["c++"], cooldown=0)
    async def plusplus(ctx):
        await ctx.reply(f"{ctx.name}: {ctx.text}")


    @on_phrase(any=["oops"], cooldown=0)
    async def broken(ctx):
        raise PluginError("do not say that")


    @on_phrase(any=["kaboom"], cooldown=0)
    async def crashes(ctx):
        raise RuntimeError("kaboom")


    @command("hi")
    async def hi(ctx):
        return "hi from the command"
    """
)


@posix_only
@respx.mock
async def test_a_real_plugin_answers_phrases_through_the_real_worker(
    rigs, tmp_path, caplog
) -> None:
    write_plugin(tmp_path, "greeter", source=GREETER)
    rig = await rigs(tmp_path, command=default_command)
    assert rig.record("greeter").status is Status.ACTIVE, rig.record("greeter").reason
    route = message_route()
    reaction = respx.post(url__regex=rf"{TALK}/reaction/{ROOM}/\d+").mock(
        return_value=httpx.Response(201, json={"ocs": {"data": {}}})
    )
    with caplog.at_level(logging.INFO):
        await rig.bot.handle(event("Good Morning all", message_id=100))
        await rig.bot.handle(event("gm again", message_id=101))  # cooling for an hour
        await rig.bot.handle(event("I like C++ a lot", message_id=102))
        await rig.bot.handle(event("oops", message_id=103))
        await rig.bot.handle(event("kaboom", message_id=104))
        await rig.bot.handle(event("!hi gm", message_id=105))
        await rig.bot.handle(event("gmail", message_id=106))
    assert said(route) == [
        "Good morning, Alice! (you said 'good morning', trigger phrase)",
        "plusplus: I like C++ a lot",
        "hi from the command",
    ]
    assert reaction.call_count == 1  # the first message only: the others were cooling
    assert "phrase handler broken said: do not say that" in caplog.text
    assert "kaboom" in caplog.text  # in the log, with the plugin named, never in the room
    rig.clock.advance(3601)
    await rig.bot.handle(event("gm", message_id=107))
    assert said(route)[-1] == "Good morning, Alice! (you said 'gm', trigger phrase)"


@posix_only
async def test_a_real_plugin_with_a_bad_phrase_declaration_fails_with_a_readable_reason(
    rigs, tmp_path
) -> None:
    source = textwrap.dedent(
        """
        from sable.plugin_api import on_phrase

        @on_phrase(any=["x"])
        async def greet(ctx):
            return "hi"
        """
    )
    write_plugin(tmp_path, "greeter", source=source)
    write_plugin(
        tmp_path,
        "twice",
        source=textwrap.dedent(
            """
            from sable.plugin_api import on_phrase

            @on_phrase(any=["gm"])
            async def greet(ctx):
                return "hi"

            greet2 = on_phrase(any=["hey"])(greet)
            """
        ),
    )
    rig = await rigs(tmp_path, command=default_command)
    assert rig.record("greeter").status is Status.FAILED
    assert "2 to 100" in rig.record("greeter").reason
    assert rig.record("twice").status is Status.FAILED
    assert "more than once" in rig.record("twice").reason


@posix_only
async def test_a_real_phrase_only_plugin_is_reported_by_check(tmp_path) -> None:
    from sable.plugins import check_plugins

    write_plugin(
        tmp_path,
        "greeter",
        source="from sable.plugin_api import on_phrase\n"
        "@on_phrase(any=['gm'])\nasync def greet(ctx):\n    return 'hi'\n",
    )
    lines, refused = await check_plugins(make_config(plugins_dir=str(tmp_path)), Registry())
    assert not refused
    assert any("greeter: active (commands: none; phrases: greet)" in line for line in lines)


@respx.mock
async def test_the_manager_is_not_needed_for_phrases_to_be_ignored(http_client) -> None:
    bot = Bot(make_config(), http_client=http_client, llm=FakeLLM())  # type: ignore[arg-type]
    assert bot.plugins is None
    assert bot.would_handle(event("gm")) is False
    route = message_route()
    await bot.handle(event("gm"))
    assert route.call_count == 0
    assert as_bot  # imported for the access matrix of bots in other tests
    assert PluginManager is not None
