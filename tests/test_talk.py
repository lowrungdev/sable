from __future__ import annotations

import json

import httpx
import pytest
import respx
from conftest import BACKEND, ROOM, SECRET

from sable.signing import verify
from sable.talk import API_BASE, TalkClient, TalkError

MESSAGE_URL = f"{BACKEND}{API_BASE}/bot/{ROOM}/message"
REACTION_URL = f"{BACKEND}{API_BASE}/bot/{ROOM}/reaction/100"
FEATURES_URL = f"{BACKEND}{API_BASE}/bot/ask-features"


def ocs(data: dict, status: int = 201) -> httpx.Response:
    return httpx.Response(status, json={"ocs": {"meta": {"status": "ok"}, "data": data}})


def signed_value(request: httpx.Request) -> tuple[str, str]:
    return (
        request.headers["x-nextcloud-talk-bot-random"],
        request.headers["x-nextcloud-talk-bot-signature"],
    )


@respx.mock
async def test_send_message_signs_the_message_text_only() -> None:
    route = respx.post(MESSAGE_URL).mock(return_value=ocs({"id": 42}))
    client = TalkClient(BACKEND, SECRET)
    try:
        assert await client.send_message(ROOM, "hello **world**") == 42
    finally:
        await client.aclose()

    request = route.calls.last.request
    random, signature = signed_value(request)
    # Signed over the message, not the serialised JSON body.
    assert verify(random, signature, b"hello **world**", SECRET)
    assert not verify(random, signature, request.content, SECRET)
    assert request.headers["ocs-apirequest"] == "true"
    assert json.loads(request.content) == {"message": "hello **world**", "silent": False}


@respx.mock
async def test_send_message_passes_reply_and_reference() -> None:
    route = respx.post(MESSAGE_URL).mock(return_value=ocs({"id": 1}))
    client = TalkClient(BACKEND, SECRET)
    try:
        await client.send_message(
            ROOM, "hi", reply_to=7, silent=True, reference_id="ref-1"
        )
    finally:
        await client.aclose()
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "message": "hi",
        "silent": True,
        "replyTo": 7,
        "referenceId": "ref-1",
    }


@respx.mock
async def test_send_message_tolerates_a_response_without_an_id() -> None:
    respx.post(MESSAGE_URL).mock(return_value=httpx.Response(201, text="not json"))
    client = TalkClient(BACKEND, SECRET)
    try:
        assert await client.send_message(ROOM, "hi") == 0
    finally:
        await client.aclose()


@respx.mock
async def test_send_message_truncates_and_signs_the_truncated_text() -> None:
    route = respx.post(MESSAGE_URL).mock(return_value=ocs({"id": 1}))
    client = TalkClient(BACKEND, SECRET, max_message_chars=50)
    try:
        await client.send_message(ROOM, "x" * 500)
    finally:
        await client.aclose()

    sent = json.loads(route.calls.last.request.content)["message"]
    assert len(sent) == 50
    assert sent.endswith("_[truncated]_")
    random, signature = signed_value(route.calls.last.request)
    assert verify(random, signature, sent.encode(), SECRET)


async def test_send_message_refuses_empty_text() -> None:
    client = TalkClient(BACKEND, SECRET)
    try:
        with pytest.raises(ValueError):
            await client.send_message(ROOM, "   ")
    finally:
        await client.aclose()


@respx.mock
async def test_talk_errors_carry_the_status() -> None:
    respx.post(MESSAGE_URL).mock(return_value=httpx.Response(404, text="no such room"))
    client = TalkClient(BACKEND, SECRET)
    try:
        with pytest.raises(TalkError) as excinfo:
            await client.send_message(ROOM, "hi")
    finally:
        await client.aclose()
    assert excinfo.value.status == 404
    assert "no such room" in excinfo.value.body


@respx.mock
async def test_reactions_sign_the_emoji() -> None:
    add = respx.post(REACTION_URL).mock(return_value=ocs({}, 201))
    remove = respx.delete(REACTION_URL).mock(return_value=ocs({}, 200))
    client = TalkClient(BACKEND, SECRET)
    try:
        await client.react(ROOM, 100, "👀")
        await client.unreact(ROOM, 100, "👀")
    finally:
        await client.aclose()

    for route in (add, remove):
        random, signature = signed_value(route.calls.last.request)
        assert verify(random, signature, "👀".encode(), SECRET)


@respx.mock
async def test_features_signs_the_token() -> None:
    route = respx.post(FEATURES_URL).mock(return_value=ocs({"features": 3}, 200))
    client = TalkClient(BACKEND, SECRET)
    try:
        assert await client.features(ROOM) == 3
    finally:
        await client.aclose()
    random, signature = signed_value(route.calls.last.request)
    assert verify(random, signature, ROOM.encode(), SECRET)


@respx.mock
async def test_try_react_swallows_failures() -> None:
    respx.post(REACTION_URL).mock(return_value=httpx.Response(400, text="nope"))
    respx.delete(REACTION_URL).mock(side_effect=httpx.ConnectError("refused"))
    client = TalkClient(BACKEND, SECRET)
    try:
        assert await client.try_react(ROOM, 100, "👀") is False
        assert await client.try_react(ROOM, 100, "") is False
        await client.try_unreact(ROOM, 100, "👀")  # must not raise
    finally:
        await client.aclose()
