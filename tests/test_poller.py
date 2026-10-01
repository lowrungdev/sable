"""Reading chat: the long-poll loop, end to end against a scripted Talk."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Callable

import httpx
import pytest
import respx
from sable.talk import ROOMS_API_BASE
from conftest import (
    BACKEND,
    ROOM,
    TALK,
    USER,
    FakeLLM,
    make_config,
    mention,
    message_payload,
    reaction_payload,
)

from sable.bot import Bot
from sable.poller import MAX_POLLED_ROOMS, Poller

OTHER = "wxyz9876"
ROOM_URL = f"{BACKEND}{ROOMS_API_BASE}/room"


def chat_url(room: str = ROOM) -> str:
    return f"{TALK}/chat/{room}"


def ocs(data: object, status: int = 200, **kwargs) -> httpx.Response:
    return httpx.Response(status, json={"ocs": {"meta": {}, "data": data}}, **kwargs)


def room(token: str = ROOM, last: int | None = 50, **extra) -> dict:
    body: dict = {"token": token, "displayName": "Team chat", "type": 2, "lastActivity": 1}
    body["lastMessage"] = {"id": last} if last is not None else []
    body.update(extra)
    return body


class Room:
    """A scripted conversation: what each poll of it answers, in order.

    Once the script runs out it answers 304 - nothing new - like Talk does when a
    poll times out empty, so the loop idles the way the real one would.
    """

    def __init__(self, *batches: list[dict] | int | Exception) -> None:
        self.batches = list(batches)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.params.get("lookIntoFuture") == "0":
            return ocs([{"id": 9}])
        if not self.batches:
            return httpx.Response(304)
        step = self.batches.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return httpx.Response(step, text="scripted")
        last = max(m["id"] for m in step)
        return ocs(step, headers={"X-Chat-Last-Given": str(last)})

    @property
    def polls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.params.get("lookIntoFuture") == "1"]

    @property
    def cursors(self) -> list[int]:
        return [int(r.url.params["lastKnownMessageId"]) for r in self.polls]


async def until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("gave up waiting for the poller")
        await asyncio.sleep(0.005)


@pytest.fixture
async def rig(llm: FakeLLM) -> AsyncIterator[tuple[Bot, Poller, list[asyncio.Task]]]:
    """A bot and a poller whose handlers run as plain tasks, and quickly."""
    bot = Bot(make_config(poll_timeout=5), llm=llm)  # type: ignore[arg-type]
    tasks: list[asyncio.Task] = []
    poller = Poller(
        bot,
        lambda coro: tasks.append(asyncio.create_task(coro)),
        backoff_base=0.01,
        backoff_max=0.05,
        idle_gap=0.01,
    )
    try:
        yield bot, poller, tasks
    finally:
        await poller.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.aclose()


def sent_bodies(route) -> list[dict]:
    return [json.loads(call.request.content) for call in route.calls]


def post_route():
    return respx.post(chat_url()).mock(return_value=ocs({"id": 7}, 201))


# -- where a conversation is followed from ------------------------------------ #


@respx.mock
async def test_history_is_not_replayed_at_start(rig) -> None:
    """The poll begins at the room's newest message, so an old '!ping' in the
    backlog is never answered."""
    _, poller, _ = rig
    feed = Room()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: feed.polls)

    assert feed.cursors[0] == 50
    # Nothing was fetched backwards: the room list already named the newest message.
    assert not [r for r in feed.requests if r.url.params.get("lookIntoFuture") == "0"]


@respx.mock
async def test_a_room_with_no_last_message_asks_for_the_newest_first(rig) -> None:
    _, poller, _ = rig
    feed = Room()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=None)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: feed.polls)

    assert feed.requests[0].url.params["lookIntoFuture"] == "0"
    assert feed.requests[0].url.params["limit"] == "1"
    assert feed.cursors[0] == 9


@respx.mock
async def test_the_cursor_advances_and_a_304_keeps_it(rig) -> None:
    bot, poller, tasks = rig
    feed = Room([message_payload("!ping", message_id=51)], 304)
    route = post_route()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: len(feed.polls) >= 4)

    # 50 -> the poll that found 51 -> 51 for the 304 and every idle poll after it.
    assert feed.cursors[:4] == [50, 51, 51, 51]
    await until(lambda: route.called)
    assert sent_bodies(route)[0]["message"] == "pong 🏓"
    assert len(route.calls) == 1, "a 304 must not deliver anything a second time"


@respx.mock
async def test_messages_arrive_in_order_and_each_is_handled(rig) -> None:
    bot, poller, tasks = rig
    # Talk answers oldest first, but the loop must not rely on it.
    feed = Room([message_payload("!echo b", message_id=53), message_payload("!echo a", message_id=52)])
    route = post_route()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: len(route.calls) == 2)

    assert sorted(b["message"] for b in sent_bodies(route)) == ["a", "b"]
    assert feed.cursors[1] == 53


# -- which conversations ------------------------------------------------------- #


@respx.mock
async def test_a_room_that_appears_is_followed_and_one_that_goes_is_dropped(
    rig, caplog
) -> None:
    _, poller, _ = rig
    feeds = {ROOM: Room(), OTHER: Room()}
    listing = respx.get(ROOM_URL).mock(return_value=ocs([room(ROOM, 50)]))
    for token, feed in feeds.items():
        respx.get(chat_url(token)).mock(side_effect=feed)

    with caplog.at_level(logging.INFO):
        await poller.scan()
        assert poller.following == [ROOM]

        listing.mock(return_value=ocs([room(ROOM, 50), room(OTHER, 70)]))
        await poller.scan()
        assert poller.following == sorted([ROOM, OTHER])
        await until(lambda: feeds[OTHER].polls)
        assert feeds[OTHER].cursors[0] == 70, "a new room starts from its newest message"

        listing.mock(return_value=ocs([room(OTHER, 70)]))
        await poller.scan()
        await asyncio.sleep(0)
        assert poller.following == [OTHER]

    assert f"following conversation {OTHER}" in caplog.text
    assert f"no longer in conversation {ROOM}" in caplog.text


@respx.mock
async def test_a_room_already_followed_is_not_followed_twice(rig) -> None:
    _, poller, _ = rig
    feed = Room()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    first = poller._tasks[ROOM]
    await poller.scan()
    assert poller._tasks[ROOM] is first


@respx.mock
async def test_unusable_tokens_and_rooms_nobody_talks_to_are_skipped(rig, caplog) -> None:
    _, poller, _ = rig
    respx.get(ROOM_URL).mock(
        return_value=ocs(
            [
                room("../evil", 1),
                room("UPPER", 1),
                room(OTHER, 1, type=4),
                room("former01", 1, type=5),
                room("notetoself", 1, type=6),
                room("sample01", 1, objectType="sample"),
                room(ROOM, 50),
            ]
        )
    )
    respx.get(chat_url()).mock(side_effect=Room())
    with caplog.at_level(logging.WARNING):
        await poller.scan()
    assert poller.following == [ROOM]
    assert "'../evil'" in caplog.text


@respx.mock
async def test_only_the_busiest_rooms_are_followed_past_the_cap(rig, caplog) -> None:
    _, poller, _ = rig
    rooms = [
        room(f"room{index:04d}", 10, lastActivity=index) for index in range(MAX_POLLED_ROOMS + 5)
    ]
    respx.get(ROOM_URL).mock(return_value=ocs(rooms))
    respx.get(url__regex=rf"{TALK}/chat/room\d+").mock(side_effect=Room())
    with caplog.at_level(logging.WARNING):
        await poller.scan()
        await poller.scan()
    assert len(poller.following) == MAX_POLLED_ROOMS
    assert "room0000" not in poller.following, "the quietest room is the one dropped"
    assert f"room{MAX_POLLED_ROOMS + 4:04d}" in poller.following
    assert caplog.text.count("following only the") == 1, "said once, not every scan"


@respx.mock
async def test_the_scan_loop_follows_rooms_by_itself_and_stops_cleanly(rig) -> None:
    bot, _, tasks = rig
    feed = Room()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)
    poller = Poller(bot, lambda coro: tasks.append(asyncio.create_task(coro)), idle_gap=0.01)

    poller.start()
    await until(lambda: poller.following == [ROOM])
    await poller.stop()
    assert poller.following == []


# -- what is acted on ------------------------------------------------------------ #


@respx.mock
async def test_our_own_replies_coming_back_down_the_poll_are_ignored(rig) -> None:
    _, poller, tasks = rig
    own = message_payload("!ping", message_id=51, actor_id=f"users/{USER}")
    feed = Room([own])
    route = post_route()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: len(feed.polls) >= 3)
    await asyncio.gather(*tasks)
    assert not route.called


@respx.mock
async def test_another_bot_is_ignored(rig) -> None:
    _, poller, tasks = rig
    feed = Room([message_payload("!ping", message_id=51, actor_id="bots/other")])
    route = post_route()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: len(feed.polls) >= 3)
    await asyncio.gather(*tasks)
    assert not route.called


@respx.mock
async def test_a_mention_by_parameter_reaches_the_model(rig, llm: FakeLLM) -> None:
    _, poller, tasks = rig
    feed = Room(
        [
            message_payload(
                "{mention-user1} what is 6*7?",
                message_id=51,
                parameters=mention("mention-user1", user=USER, name="Sable Bot"),
            )
        ]
    )
    route = post_route()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: route.called)
    assert llm.last_prompt == "Alice: what is 6*7?"
    assert sent_bodies(route)[0]["message"] == "mock answer"


@respx.mock
async def test_the_ask_reaction_arrives_as_a_system_message_and_is_answered(
    rig, llm: FakeLLM
) -> None:
    _, poller, tasks = rig
    feed = Room(
        [message_payload("the deploy failed", message_id=51, actor_name="Bob")],
        [reaction_payload("⁉️", message_id=51, system_id=52)],
    )
    route = post_route()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: route.called)
    assert "the deploy failed" in llm.last_prompt
    assert sent_bodies(route)[0]["replyTo"] == 51


@respx.mock
async def test_a_revoked_reaction_does_nothing(rig, llm: FakeLLM) -> None:
    _, poller, tasks = rig
    feed = Room(
        [message_payload("the deploy failed", message_id=51)],
        [reaction_payload("⁉️", message_id=51, system_id=52, undo=True)],
    )
    route = post_route()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: len(feed.polls) >= 4)
    await asyncio.gather(*tasks)
    assert not llm.calls
    assert not route.called


@respx.mock
async def test_other_system_messages_are_passed_over(rig) -> None:
    _, poller, tasks = rig
    joined = message_payload("{actor} joined", message_id=51)
    joined["messageType"] = "system"
    joined["systemMessage"] = "user_added"
    feed = Room([joined])
    route = post_route()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: len(feed.polls) >= 3)
    await asyncio.gather(*tasks)
    assert not route.called
    assert feed.cursors[2] == 51, "the cursor still moves past what is ignored"


# -- when Nextcloud misbehaves ------------------------------------------------------ #


@respx.mock
async def test_errors_back_off_and_the_poll_carries_on_from_the_same_cursor(rig) -> None:
    _, poller, _ = rig
    feed = Room(500, httpx.ConnectError("refused"), [message_payload("!ping", message_id=51)])
    route = post_route()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: route.called)
    assert feed.cursors[:3] == [50, 50, 50]
    assert poller.following == [ROOM]


@respx.mock
async def test_a_poll_held_too_long_is_slow_not_lost(rig, caplog) -> None:
    bot, poller, _ = rig
    feed = Room(
        httpx.ReadTimeout("held"),
        httpx.ReadTimeout("held"),
        [message_payload("hi", message_id=51)],
    )
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)
    with caplog.at_level(logging.DEBUG):
        await poller.scan()
        await until(lambda: len(feed.polls) >= 4)
    assert "lost connection to Nextcloud" not in caplog.text
    assert bot.nextcloud.up is not False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert ROOM in warnings[0].getMessage()
    assert "PHP-FPM" in warnings[0].getMessage()
    assert feed.cursors[:3] == [50, 50, 50]


@respx.mock
async def test_losing_nextcloud_is_logged_once_and_its_return_once(rig, caplog) -> None:
    bot, poller, _ = rig
    feed = Room(
        httpx.ConnectError("gone"),
        httpx.ConnectError("still gone"),
        httpx.ConnectError("still gone"),
        [message_payload("hi", message_id=51)],
    )
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)
    with caplog.at_level(logging.INFO):
        await poller.scan()
        await until(lambda: len(feed.polls) >= 5)
    assert caplog.text.count("lost connection to Nextcloud") == 1
    assert caplog.text.count("Nextcloud is reachable again") == 1
    assert bot.nextcloud.up is True


@respx.mock
async def test_a_conversation_we_were_removed_from_is_dropped(rig, caplog) -> None:
    _, poller, _ = rig
    feed = Room(404)
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    with caplog.at_level(logging.INFO):
        await poller.scan()
        await until(lambda: poller.following == [])
    assert "no longer readable (HTTP 404)" in caplog.text
    assert ROOM not in poller._cursors


@respx.mock
async def test_a_failed_room_list_is_survived_by_the_scan_loop(rig) -> None:
    bot, _, tasks = rig
    feed = Room()
    calls = {"n": 0}

    def listing(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else ocs([room(last=50)])

    respx.get(ROOM_URL).mock(side_effect=listing)
    respx.get(chat_url()).mock(side_effect=feed)
    poller = Poller(
        bot,
        lambda coro: tasks.append(asyncio.create_task(coro)),
        backoff_base=0.01,
        idle_gap=0.01,
    )
    poller.start()
    try:
        await until(lambda: poller.following == [ROOM])
    finally:
        await poller.stop()
    assert calls["n"] >= 2


@respx.mock
async def test_polls_use_the_configured_timeout_and_the_account(rig) -> None:
    _, poller, _ = rig
    feed = Room()
    respx.get(ROOM_URL).mock(return_value=ocs([room(last=50)]))
    respx.get(chat_url()).mock(side_effect=feed)

    await poller.scan()
    await until(lambda: feed.polls)
    request = feed.polls[0]
    assert request.url.params["timeout"] == "5"
    assert request.headers["authorization"].startswith("Basic ")
    assert request.headers["ocs-apirequest"] == "true"
