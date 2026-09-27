from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx
import pytest
import respx
from conftest import BACKEND, ROOM, SECRET, FakeLLM, make_config, message_payload, signed_headers

from sable.app import create_app
from sable.bot import Bot
from sable.config import Config
from sable.talk import API_BASE

MESSAGE_URL = f"{BACKEND}{API_BASE}/bot/{ROOM}/message"


def message_route():
    return respx.post(MESSAGE_URL).mock(
        return_value=httpx.Response(201, json={"ocs": {"data": {"id": 1}}})
    )


async def client_for(config: Config, llm: FakeLLM | None = None) -> AsyncIterator[httpx.AsyncClient]:
    """Drive the app in-process, with the lifespan running."""
    bot = Bot(config, llm=llm or FakeLLM())  # type: ignore[arg-type]
    app = create_app(config, bot=bot)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://sable.test"
            ) as client:
                yield client
    finally:
        await bot.aclose()


@pytest.fixture
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    async for client in client_for(make_config()):
        yield client


async def wait_for(route, tries: int = 50) -> None:
    """Yield to the loop until a background reply lands."""
    for _ in range(tries):
        if route.called:
            return
        await asyncio.sleep(0)
    raise AssertionError("the bot never called Talk")


def post_body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


async def test_healthz(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["bot"] == "sable"
    assert body["llm"] == "some-model"


async def test_root_is_a_plain_banner(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/")
    assert response.status_code == 200
    assert response.text.startswith("sable ")


@respx.mock
async def test_a_signed_webhook_is_accepted_and_answered() -> None:
    route = message_route()
    body = post_body(message_payload("!ping"))
    async for client in client_for(make_config()):
        response = await client.post("/webhook", content=body, headers=signed_headers(body))
        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}
        await wait_for(route)
    assert json.loads(route.calls.last.request.content)["message"] == "pong 🏓"


@respx.mock
async def test_the_llm_path_works_end_to_end() -> None:
    route = message_route()
    llm = FakeLLM(reply="42")
    body = post_body(message_payload("@sable what is 6*7"))
    async for client in client_for(make_config(), llm=llm):
        await client.post("/webhook", content=body, headers=signed_headers(body))
        await wait_for(route)
    assert json.loads(route.calls.last.request.content)["message"] == "42"
    assert llm.last_prompt == "Alice: what is 6*7"


@respx.mock
async def test_a_bad_signature_is_rejected() -> None:
    route = message_route()
    body = post_body(message_payload("!ping"))
    headers = signed_headers(body)
    headers["X-Nextcloud-Talk-Signature"] = "0" * 64
    async for client in client_for(make_config()):
        response = await client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 401
    assert not route.called


@respx.mock
async def test_a_body_rewritten_after_signing_is_rejected() -> None:
    body = post_body(message_payload("!ping"))
    headers = signed_headers(body)
    async for client in client_for(make_config()):
        response = await client.post(
            "/webhook", content=post_body(message_payload("!echo pwned")), headers=headers
        )
    assert response.status_code == 401


async def test_missing_signature_headers_are_rejected() -> None:
    async for client in client_for(make_config()):
        response = await client.post("/webhook", json={"type": "Create"})
    assert response.status_code == 401


async def test_an_unexpected_backend_is_rejected() -> None:
    body = post_body(message_payload("!ping"))
    headers = signed_headers(body, backend="https://evil.example.org")
    async for client in client_for(make_config()):
        response = await client.post("/webhook", content=body, headers=headers)
    assert response.status_code == 403


@respx.mock
async def test_the_backend_header_is_trusted_when_pinning_is_off() -> None:
    other = "https://other.example.org"
    route = respx.post(f"{other}{API_BASE}/bot/{ROOM}/message").mock(
        return_value=httpx.Response(201, json={"ocs": {"data": {"id": 1}}})
    )
    body = post_body(message_payload("!ping"))
    headers = signed_headers(body, backend=other)
    async for client in client_for(make_config(pin_backend=False, nextcloud_url="")):
        response = await client.post("/webhook", content=body, headers=headers)
        assert response.status_code == 200
        await wait_for(route)


async def test_invalid_json_is_a_400() -> None:
    body = b"{not json"
    async for client in client_for(make_config()):
        response = await client.post("/webhook", content=body, headers=signed_headers(body))
    assert response.status_code == 400


async def test_an_unparseable_event_is_a_400() -> None:
    body = post_body({"type": "Create", "object": {}, "target": {}})
    async for client in client_for(make_config()):
        response = await client.post("/webhook", content=body, headers=signed_headers(body))
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# /notify
# --------------------------------------------------------------------------- #

NOTIFY_CONFIG = dict(notify_token="alert-token", notify_rooms={"alerts": ROOM})


@respx.mock
async def test_notify_relays_to_an_alias() -> None:
    route = message_route()
    async for client in client_for(make_config(**NOTIFY_CONFIG)):
        response = await client.post(
            "/notify",
            json={"room": "alerts", "message": "**disk full** on db01", "silent": True},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 201
    assert response.json() == {"ok": True, "room": ROOM, "messageId": 1}
    body = json.loads(route.calls.last.request.content)
    assert body == {"message": "**disk full** on db01", "silent": True}


@respx.mock
async def test_notify_accepts_a_raw_token_and_reply_to() -> None:
    route = message_route()
    async for client in client_for(make_config(**NOTIFY_CONFIG)):
        response = await client.post(
            "/notify",
            json={"room": ROOM, "message": "hi", "replyTo": 9},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 201
    assert json.loads(route.calls.last.request.content)["replyTo"] == 9


async def test_notify_needs_the_right_token() -> None:
    async for client in client_for(make_config(**NOTIFY_CONFIG)):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "alert-token"}):
            response = await client.post(
                "/notify", json={"room": "alerts", "message": "hi"}, headers=headers
            )
            assert response.status_code == 401


async def test_notify_rejects_an_unknown_room() -> None:
    async for client in client_for(make_config(**NOTIFY_CONFIG)):
        response = await client.post(
            "/notify",
            json={"room": "not an alias!", "message": "hi"},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 400


async def test_notify_validates_its_body() -> None:
    async for client in client_for(make_config(**NOTIFY_CONFIG)):
        response = await client.post(
            "/notify",
            json={"room": "alerts", "message": ""},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 422


async def test_notify_is_404_when_disabled() -> None:
    async for client in client_for(make_config()):
        response = await client.post(
            "/notify",
            json={"room": ROOM, "message": "hi"},
            headers={"Authorization": "Bearer anything"},
        )
    assert response.status_code == 404


@respx.mock
async def test_notify_surfaces_a_talk_rejection() -> None:
    respx.post(MESSAGE_URL).mock(return_value=httpx.Response(404, text="unknown room"))
    async for client in client_for(make_config(**NOTIFY_CONFIG)):
        response = await client.post(
            "/notify",
            json={"room": "alerts", "message": "hi"},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 400
    assert "unknown room" in response.json()["detail"]


@respx.mock
async def test_notify_reports_a_server_side_failure_as_502() -> None:
    respx.post(MESSAGE_URL).mock(return_value=httpx.Response(500, text="nextcloud broke"))
    async for client in client_for(make_config(**NOTIFY_CONFIG)):
        response = await client.post(
            "/notify",
            json={"room": "alerts", "message": "hi"},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 502


@respx.mock
async def test_notify_reports_an_unreachable_nextcloud_as_502() -> None:
    respx.post(MESSAGE_URL).mock(side_effect=httpx.ConnectError("refused"))
    async for client in client_for(make_config(**NOTIFY_CONFIG)):
        response = await client.post(
            "/notify",
            json={"room": "alerts", "message": "hi"},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 502
