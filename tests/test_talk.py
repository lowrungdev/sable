from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx
from conftest import BACKEND, PASSWORD, ROOM, TALK, USER

from sable.state import ConnectionState
from sable.talk import API_BASE, ROOMS_API_BASE, TalkClient, TalkError

CHAT_URL = f"{TALK}/chat/{ROOM}"
REACTION_URL = f"{TALK}/reaction/{ROOM}/100"
ROOM_URL = f"{BACKEND}{ROOMS_API_BASE}/room"
USER_URL = f"{BACKEND}/ocs/v2.php/cloud/user"


def ocs(data: object, status: int = 200, **kwargs) -> httpx.Response:
    return httpx.Response(status, json={"ocs": {"meta": {"status": "ok"}, "data": data}}, **kwargs)


def make() -> TalkClient:
    return TalkClient(BACKEND, USER, PASSWORD)


def test_the_base_path_is_the_spreed_v1_api() -> None:
    assert API_BASE == "/ocs/v2.php/apps/spreed/api/v1"


# -- sending ---------------------------------------------------------------- #


@respx.mock
async def test_send_message_is_a_chat_post_with_basic_auth() -> None:
    route = respx.post(CHAT_URL).mock(return_value=ocs({"id": 42}, 201))
    client = make()
    try:
        assert await client.send_message(ROOM, "hello **world**") == 42
    finally:
        await client.aclose()

    request = route.calls.last.request
    expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert request.headers["ocs-apirequest"] == "true"
    assert request.headers["accept"] == "application/json"
    assert json.loads(request.content) == {"message": "hello **world**", "silent": False}


@respx.mock
async def test_send_message_passes_reply_and_silent() -> None:
    route = respx.post(CHAT_URL).mock(return_value=ocs({"id": 1}, 201))
    client = make()
    try:
        await client.send_message(ROOM, "hi", reply_to=7, silent=True)
    finally:
        await client.aclose()
    assert json.loads(route.calls.last.request.content) == {
        "message": "hi",
        "silent": True,
        "replyTo": 7,
    }


@respx.mock
async def test_send_message_tolerates_a_response_without_an_id() -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(201, text="not json"))
    client = make()
    try:
        assert await client.send_message(ROOM, "hi") == 0
    finally:
        await client.aclose()


@respx.mock
async def test_send_message_truncates() -> None:
    route = respx.post(CHAT_URL).mock(return_value=ocs({"id": 1}, 201))
    client = TalkClient(BACKEND, USER, PASSWORD, max_message_chars=50)
    try:
        await client.send_message(ROOM, "x" * 500)
    finally:
        await client.aclose()
    sent = json.loads(route.calls.last.request.content)["message"]
    assert len(sent) == 50
    assert sent.endswith("_[truncated]_")


async def test_send_message_refuses_empty_text() -> None:
    client = make()
    try:
        with pytest.raises(ValueError):
            await client.send_message(ROOM, "   ")
    finally:
        await client.aclose()


@respx.mock
async def test_talk_errors_carry_the_status() -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(404, text="no such room"))
    client = make()
    try:
        with pytest.raises(TalkError) as excinfo:
            await client.send_message(ROOM, "hi")
    finally:
        await client.aclose()
    assert excinfo.value.status == 404
    assert "no such room" in excinfo.value.body


# -- reactions --------------------------------------------------------------- #


@respx.mock
async def test_react_and_unreact_shapes() -> None:
    add = respx.post(REACTION_URL).mock(return_value=ocs({}, 201))
    remove = respx.delete(REACTION_URL).mock(return_value=ocs({}, 200))
    client = make()
    try:
        await client.react(ROOM, 100, "👀")
        await client.unreact(ROOM, 100, "👀")
    finally:
        await client.aclose()

    assert json.loads(add.calls.last.request.content) == {"reaction": "👀"}
    assert json.loads(remove.calls.last.request.content) == {"reaction": "👀"}
    assert remove.calls.last.request.url.params["reaction"] == "👀"
    for route in (add, remove):
        assert route.calls.last.request.headers["authorization"].startswith("Basic ")
        assert route.calls.last.request.headers["ocs-apirequest"] == "true"


@respx.mock
async def test_reacting_twice_and_unreacting_what_is_gone_are_not_errors() -> None:
    respx.post(REACTION_URL).mock(return_value=ocs({}, 200))
    respx.delete(REACTION_URL).mock(return_value=httpx.Response(404))
    client = make()
    try:
        await client.react(ROOM, 100, "👀")
        await client.unreact(ROOM, 100, "👀")
    finally:
        await client.aclose()


@respx.mock
async def test_try_react_swallows_failures() -> None:
    respx.post(REACTION_URL).mock(return_value=httpx.Response(400, text="nope"))
    respx.delete(REACTION_URL).mock(side_effect=httpx.ConnectError("refused"))
    client = make()
    try:
        assert await client.try_react(ROOM, 100, "👀") is False
        assert await client.try_react(ROOM, 100, "") is False
        await client.try_unreact(ROOM, 100, "👀")  # must not raise
    finally:
        await client.aclose()


# -- who we are -------------------------------------------------------------- #


@respx.mock
async def test_whoami_returns_the_user_id_and_display_name() -> None:
    route = respx.get(USER_URL).mock(
        return_value=ocs({"id": "sable", "displayname": "Sable Bot"})
    )
    client = make()
    try:
        assert await client.whoami() == ("sable", "Sable Bot")
    finally:
        await client.aclose()
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


@respx.mock
async def test_whoami_with_the_wrong_password_is_a_401_talk_error() -> None:
    respx.get(USER_URL).mock(return_value=httpx.Response(401, text="Current user is not logged in"))
    client = make()
    try:
        with pytest.raises(TalkError) as excinfo:
            await client.whoami()
    finally:
        await client.aclose()
    assert excinfo.value.status == 401


@respx.mock
async def test_whoami_refuses_an_answer_that_names_nobody() -> None:
    respx.get(USER_URL).mock(return_value=httpx.Response(200, text="<html>not nextcloud</html>"))
    client = make()
    try:
        with pytest.raises(TalkError):
            await client.whoami()
    finally:
        await client.aclose()


# -- receiving --------------------------------------------------------------- #


@respx.mock
async def test_rooms_lists_the_conversations() -> None:
    route = respx.get(ROOM_URL).mock(
        return_value=ocs([{"token": "abcd1234"}, "junk", {"token": "wxyz9876"}])
    )
    client = make()
    try:
        assert [r["token"] for r in await client.rooms()] == ["abcd1234", "wxyz9876"]
        assert route.calls.last.request.url.params["noStatusUpdate"] == "1"
    finally:
        await client.aclose()


@respx.mock
async def test_poll_asks_for_what_is_new_and_advances_the_cursor() -> None:
    route = respx.get(CHAT_URL).mock(
        return_value=ocs(
            [{"id": 12, "message": "b"}, {"id": 11, "message": "a"}],
            headers={"X-Chat-Last-Given": "12"},
        )
    )
    client = make()
    try:
        messages, cursor = await client.poll(ROOM, 10, timeout=25)
    finally:
        await client.aclose()

    params = route.calls.last.request.url.params
    assert params["lookIntoFuture"] == "1"
    assert params["noStatusUpdate"] == "1"
    assert params["lastKnownMessageId"] == "10"
    assert params["timeout"] == "25"
    assert params["limit"] == "100"
    assert params["setReadMarker"] == "0"
    assert params["includeLastKnown"] == "0"
    # Oldest first, whatever order Talk answered in.
    assert [m["id"] for m in messages] == [11, 12]
    assert cursor == 12


@respx.mock
async def test_poll_takes_the_cursor_from_the_messages_when_the_header_is_missing() -> None:
    respx.get(CHAT_URL).mock(return_value=ocs([{"id": 31}]))
    client = make()
    try:
        assert (await client.poll(ROOM, 10))[1] == 31
    finally:
        await client.aclose()


@respx.mock
async def test_poll_treats_304_as_nothing_new() -> None:
    respx.get(CHAT_URL).mock(return_value=httpx.Response(304))
    client = make()
    try:
        assert await client.poll(ROOM, 10) == ([], 10)
    finally:
        await client.aclose()


@respx.mock
async def test_poll_never_asks_talk_to_wait_longer_than_it_will() -> None:
    route = respx.get(CHAT_URL).mock(return_value=httpx.Response(304))
    client = make()
    try:
        await client.poll(ROOM, 1, timeout=500)
    finally:
        await client.aclose()
    assert route.calls.last.request.url.params["timeout"] == "60"


@respx.mock
async def test_poll_errors_are_talk_errors() -> None:
    respx.get(CHAT_URL).mock(return_value=httpx.Response(404))
    client = make()
    try:
        with pytest.raises(TalkError) as excinfo:
            await client.poll(ROOM, 1)
    finally:
        await client.aclose()
    assert excinfo.value.status == 404


@respx.mock
async def test_latest_message_id_fetches_one_message_backwards() -> None:
    route = respx.get(CHAT_URL).mock(return_value=ocs([{"id": 77}]))
    client = make()
    try:
        assert await client.latest_message_id(ROOM) == 77
    finally:
        await client.aclose()
    params = route.calls.last.request.url.params
    assert params["lookIntoFuture"] == "0"
    assert params["limit"] == "1"
    assert params["setReadMarker"] == "0"


@respx.mock
async def test_latest_message_id_of_an_empty_conversation_is_zero() -> None:
    respx.get(CHAT_URL).mock(return_value=httpx.Response(304))
    client = make()
    try:
        assert await client.latest_message_id(ROOM) == 0
    finally:
        await client.aclose()


# -- reachability ------------------------------------------------------------ #


@respx.mock
async def test_the_connection_state_follows_the_calls(caplog) -> None:
    state = ConnectionState("Nextcloud")
    client = TalkClient(BACKEND, USER, PASSWORD, state=state)
    route = respx.get(ROOM_URL)
    try:
        route.mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(httpx.ConnectError):
            await client.rooms()
        assert state.up is False

        # An error answer is still an answer: reachable, just unhappy.
        route.mock(return_value=httpx.Response(500))
        with pytest.raises(TalkError):
            await client.rooms()
        assert state.up is True
    finally:
        await client.aclose()


@respx.mock
async def test_a_long_poll_read_timeout_does_not_mark_nextcloud_down() -> None:
    state = ConnectionState("Nextcloud")
    respx.get(CHAT_URL).mock(side_effect=httpx.ReadTimeout("held"))
    respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("held"))
    client = TalkClient(BACKEND, USER, PASSWORD, state=state)
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.poll(ROOM, 5, timeout=1)
        assert state.up is None
        # Any other call that times out still means Nextcloud is not answering.
        with pytest.raises(httpx.ReadTimeout):
            await client.send_message(ROOM, "hi")
        assert state.up is False
    finally:
        await client.aclose()


def test_conversations_are_listed_under_v4() -> None:
    assert ROOMS_API_BASE == "/ocs/v2.php/apps/spreed/api/v4"


# -- reading one message back, and leaving ----------------------------------- #


@respx.mock
async def test_message_asks_for_the_context_and_picks_the_message_by_id() -> None:
    route = respx.get(f"{CHAT_URL}/100/context").mock(
        return_value=ocs([{"id": 99, "message": "before"}, {"id": 100, "message": "this"}])
    )
    client = make()
    try:
        found = await client.message(ROOM, 100)
    finally:
        await client.aclose()
    assert found == {"id": 100, "message": "this"}
    assert route.calls.last.request.url.params["limit"] == "3"


@respx.mock
async def test_message_is_none_for_a_404_and_for_an_answer_without_it() -> None:
    respx.get(f"{CHAT_URL}/100/context").mock(return_value=httpx.Response(404))
    respx.get(f"{CHAT_URL}/101/context").mock(return_value=ocs([{"id": 7}]))
    respx.get(f"{CHAT_URL}/102/context").mock(return_value=ocs({"unexpected": True}))
    client = make()
    try:
        assert await client.message(ROOM, 100) is None
        assert await client.message(ROOM, 101) is None
        assert await client.message(ROOM, 102) is None
    finally:
        await client.aclose()


@respx.mock
async def test_message_raises_on_other_failures() -> None:
    respx.get(f"{CHAT_URL}/100/context").mock(return_value=httpx.Response(412, text="lobby"))
    client = make()
    try:
        with pytest.raises(TalkError) as caught:
            await client.message(ROOM, 100)
    finally:
        await client.aclose()
    assert caught.value.status == 412


@respx.mock
async def test_leave_is_a_delete_on_participants_self_under_v4() -> None:
    route = respx.delete(f"{BACKEND}{ROOMS_API_BASE}/room/{ROOM}/participants/self").mock(
        return_value=ocs({})
    )
    client = make()
    try:
        assert await client.leave(ROOM) is True
    finally:
        await client.aclose()
    assert route.called
    assert route.calls.last.request.headers["ocs-apirequest"] == "true"


@respx.mock
async def test_leaving_a_conversation_we_are_not_in_is_not_an_error() -> None:
    respx.delete(f"{BACKEND}{ROOMS_API_BASE}/room/{ROOM}/participants/self").mock(
        return_value=httpx.Response(404)
    )
    client = make()
    try:
        assert await client.leave(ROOM) is False
    finally:
        await client.aclose()


@respx.mock
async def test_the_last_moderator_cannot_leave_and_it_says_so() -> None:
    respx.delete(f"{BACKEND}{ROOMS_API_BASE}/room/{ROOM}/participants/self").mock(
        return_value=httpx.Response(400, text="last owner")
    )
    client = make()
    try:
        with pytest.raises(TalkError) as caught:
            await client.leave(ROOM)
    finally:
        await client.aclose()
    assert caught.value.status == 400
