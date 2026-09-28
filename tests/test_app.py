from __future__ import annotations

import asyncio
import base64
import json
import logging
from urllib.parse import parse_qs
from typing import AsyncIterator

import httpx
import pytest
import respx
from conftest import (
    BACKEND,
    REJECTED_TOKENS,
    ROOM,
    SECRET,
    VALID_TOKENS,
    FakeLLM,
    make_config,
    message_payload,
    signed_headers,
)

from sable.app import create_app, megabytes
from sable.bot import Bot
from sable.config import TOKEN_HINT, Config
from sable.llm import Message
from sable.signing import HEADER_BOT_RANDOM, HEADER_BOT_SIGNATURE, digest, verify
from sable.talk import API_BASE

MESSAGE_URL = f"{BACKEND}{API_BASE}/bot/{ROOM}/message"


def sent(route) -> list[dict]:
    """The JSON bodies posted to Talk, in order."""
    return [json.loads(call.request.content) for call in route.calls]


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


def signed(
    body: bytes, *, random: str = "r" * 64, secret: str = SECRET, backend: str = BACKEND
) -> dict[str, str]:
    """Like conftest's signed_headers, with the random spelled out.

    Which random a request carries decides whether it is a replay, so every test
    that sends more than one webhook has to choose them itself.
    """
    return {
        "X-Nextcloud-Talk-Random": random,
        "X-Nextcloud-Talk-Signature": digest(random, body, secret),
        "X-Nextcloud-Talk-Backend": backend,
        "Content-Type": "application/json",
    }


async def settle(times: int = 50) -> None:
    """Let the background tasks run as far as they can get on their own."""
    for _ in range(times):
        await asyncio.sleep(0)


async def wait_for_calls(route, count: int, tries: int = 500) -> None:
    """Yield to the loop until Talk has been called this many times."""
    for _ in range(tries):
        if len(route.calls) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Talk was called {len(route.calls)} times, expected {count}")


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


# --------------------------------------------------------------------------- #
# The HTTP surface that is not the three endpoints
# --------------------------------------------------------------------------- #


async def test_the_schema_and_its_doc_pages_are_off_by_default() -> None:
    """They describe every route and body shape to whoever can reach us."""
    async for client in client_for(make_config()):
        for path in ["/openapi.json", "/docs", "/redoc"]:
            assert (await client.get(path)).status_code == 404, path


async def test_the_schema_can_be_turned_on() -> None:
    async for client in client_for(make_config(api_docs=True)):
        for path in ["/openapi.json", "/docs", "/redoc"]:
            assert (await client.get(path)).status_code == 200, path


async def test_healthz_is_open_when_no_token_is_set() -> None:
    """A container healthcheck and a kubelet probe both call it bare."""
    async for client in client_for(make_config()):
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_healthz_needs_its_token_once_one_is_set() -> None:
    async for client in client_for(make_config(health_token="h" * 20)):
        bare = await client.get("/healthz")
        wrong = await client.get("/healthz", headers={"X-Health-Token": "nope"})
        right = await client.get("/healthz", headers={"X-Health-Token": "h" * 20})
    assert bare.status_code == 401
    assert wrong.status_code == 401
    assert "X-Health-Token" in wrong.json()["detail"]
    assert right.status_code == 200
    assert right.json()["version"]


async def test_a_guarded_healthz_tolerates_surrounding_space() -> None:
    async for client in client_for(make_config(health_token="h" * 20)):
        response = await client.get("/healthz", headers={"X-Health-Token": " " + "h" * 20 + " "})
    assert response.status_code == 200


async def test_startup_and_shutdown_are_logged_with_the_configuration(caplog) -> None:
    config = make_config(notify_token="t", notify_rooms={"alerts": ROOM})
    with caplog.at_level(logging.INFO):
        async for _client in client_for(config):
            pass
    text = caplog.text
    assert "starting" in text and "ready" in text
    assert "stopping" in text and "stopped" in text
    assert "listening on:   http://0.0.0.0:8080" in text
    assert "POST /webhook" in text
    assert f"nextcloud:      {BACKEND}" in text
    assert "some-model at https://api.openai.com/v1" in text
    assert "alerting:       enabled, aliases: alerts" in text
    assert f"backend pin:    on, replies only to {BACKEND}" in text
    assert "admin commands: (none" in text
    assert "api docs:       disabled" in text
    assert "health check:   GET /healthz (open)" in text
    assert "proxy trust:    127.0.0.1, ::1" in text


async def test_trusting_every_proxy_is_called_out_at_startup(caplog) -> None:
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(trusted_proxies=["*"])):
            pass
    assert "proxy trust:    * - ANY client" in caplog.text


async def test_trusting_no_proxy_is_called_out_at_startup(caplog) -> None:
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(trusted_proxies=[])):
            pass
    assert "proxy trust:    nobody" in caplog.text


async def test_the_guarded_surface_is_named_at_startup(caplog) -> None:
    config = make_config(api_docs=True, health_token="h" * 20)
    with caplog.at_level(logging.INFO):
        async for _client in client_for(config):
            pass
    assert "api docs:       /docs, /redoc, /openapi.json" in caplog.text
    assert "health check:   GET /healthz (X-Health-Token required)" in caplog.text


async def test_an_unpinned_backend_says_so_at_startup(caplog) -> None:
    """Off is the state worth spelling out: with nothing to pin against, the
    unsigned backend header on a replayed webhook chooses where replies go."""
    config = make_config(nextcloud_url="", pin_backend=False)
    with caplog.at_level(logging.INFO):
        async for _client in client_for(config):
            pass
    assert "backend pin:    OFF (no SABLE_NEXTCLOUD_URL)" in caplog.text


async def test_turning_the_pin_off_by_hand_says_which_it_was(caplog) -> None:
    config = make_config(pin_backend=False)
    with caplog.at_level(logging.INFO):
        async for _client in client_for(config):
            pass
    assert "backend pin:    OFF (SABLE_PIN_BACKEND is off)" in caplog.text


async def test_the_admin_commands_are_named_at_startup(caplog) -> None:
    config = make_config(
        admin_commands=["*"], normal_commands=["help", "ping"], admin_users=["maser"]
    )
    with caplog.at_level(logging.INFO):
        async for _client in client_for(config):
            pass
    assert "admin commands: *, except help, ping - only for maser" in caplog.text


@respx.mock
async def test_the_startup_probe_runs_when_enabled(caplog) -> None:
    route = respx.get(f"{BACKEND}/status.php").mock(
        return_value=httpx.Response(
            200, json={"installed": True, "maintenance": False, "versionstring": "31.0.4"}
        )
    )
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(startup_check=True)):
            pass
    assert route.called
    assert "connected to Nextcloud 31.0.4" in caplog.text


async def test_the_startup_probe_can_be_turned_off() -> None:
    # No respx mock at all: if it tried to call out, this would raise.
    async for client in client_for(make_config(startup_check=False)):
        assert (await client.get("/healthz")).status_code == 200


@respx.mock
async def test_a_relayed_alert_is_logged(caplog) -> None:
    message_route()
    async for client in client_for(
        make_config(notify_token="alert-token", notify_rooms={"alerts": ROOM})
    ):
        with caplog.at_level(logging.INFO):
            await client.post(
                "/notify",
                json={"room": "alerts", "message": "disk full"},
                headers={"Authorization": "Bearer alert-token"},
            )
    assert f"relayed an alert to {ROOM} (alias alerts) as message 1" in caplog.text


# --------------------------------------------------------------------------- #
# /notify with an attachment: one URL, JSON base64 or multipart
# --------------------------------------------------------------------------- #

USER = "sable-bot"
DAV = f"{BACKEND}/remote.php/dav/files/{USER}"
UPLOADS = dict(
    notify_token="alert-token",
    notify_rooms={"alerts": ROOM},
    nextcloud_user=USER,
    nextcloud_password="app-password",
)


def share_fields(route) -> dict[str, str]:
    """The share request is form encoded; read it back as fields."""
    return {k: v[0] for k, v in parse_qs(route.calls.last.request.content.decode()).items()}


def upload_routes(share_status: int = 200):
    respx.request("MKCOL", f"{DAV}/sable").mock(return_value=httpx.Response(405))
    put = respx.put(url__startswith=f"{DAV}/sable/").mock(return_value=httpx.Response(201))
    share = respx.post(f"{BACKEND}/ocs/v2.php/apps/files_sharing/api/v1/shares").mock(
        return_value=httpx.Response(
            share_status, json={"ocs": {"meta": {"status": "ok"}, "data": {"id": 99}}}
        )
    )
    respx.delete(url__startswith=f"{DAV}/sable/").mock(return_value=httpx.Response(204))
    return put, share


@respx.mock
async def test_notify_accepts_a_base64_file_in_json() -> None:
    put, share = upload_routes()
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            json={
                "room": "alerts",
                "message": "nightly build",
                "file": {"name": "report.pdf", "content": base64.b64encode(b"PDF!").decode()},
            },
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True and body["room"] == ROOM and body["shareId"] == 99
    assert body["file"]["name"].endswith("-report.pdf")
    assert body["file"]["size"] == 4
    assert put.calls.last.request.content == b"PDF!"
    # The share body is form encoded, and the caption rides in talkMetaData so
    # the file and its text arrive as one chat message rather than two.
    fields = share_fields(share)
    assert fields["shareType"] == "10"
    assert fields["shareWith"] == ROOM
    assert json.loads(fields["talkMetaData"]) == {
        "messageType": "comment",
        "caption": "nightly build",
    }


@respx.mock
async def test_notify_accepts_a_multipart_upload() -> None:
    put, share = upload_routes()
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            data={"room": "alerts", "message": "chart", "silent": "true"},
            files={"file": ("chart.png", b"\x89PNG data", "image/png")},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 201
    assert response.json()["file"]["name"].endswith("-chart.png")
    assert put.calls.last.request.content == b"\x89PNG data"
    meta = json.loads(share_fields(share)["talkMetaData"])
    assert meta["caption"] == "chart"
    assert meta["silent"] is True


@respx.mock
async def test_a_file_with_no_caption_is_fine() -> None:
    upload_routes()
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            data={"room": "alerts"},
            files={"file": ("a.txt", b"x", "text/plain")},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 201


async def test_neither_message_nor_file_is_rejected() -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            json={"room": "alerts"},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 422
    assert "message, a file, or both" in response.json()["detail"]


async def test_an_attachment_without_the_user_account_is_503() -> None:
    # notify is on, but no SABLE_NEXTCLOUD_USER: text still works, files cannot.
    async for client in client_for(
        make_config(notify_token="alert-token", notify_rooms={"alerts": ROOM})
    ):
        response = await client.post(
            "/notify",
            json={
                "room": "alerts",
                "file": {"name": "a.txt", "content": base64.b64encode(b"x").decode()},
            },
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 503
    assert "SABLE_NEXTCLOUD_USER" in response.json()["detail"]


async def test_content_that_is_not_base64_is_rejected() -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            json={"room": "alerts", "file": {"name": "a.txt", "content": "not base64!!"}},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 422
    assert "base64" in response.json()["detail"]


async def test_an_oversized_base64_attachment_is_refused() -> None:
    config = make_config(max_upload_bytes=16, **UPLOADS)
    async for client in client_for(config):
        response = await client.post(
            "/notify",
            json={
                "room": "alerts",
                "file": {"name": "big.bin", "content": base64.b64encode(b"x" * 64).decode()},
            },
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 413
    assert "SABLE_MAX_UPLOAD_BYTES" in response.json()["detail"]


async def test_an_oversized_multipart_attachment_is_refused() -> None:
    async for client in client_for(make_config(max_upload_bytes=16, **UPLOADS)):
        response = await client.post(
            "/notify",
            data={"room": "alerts"},
            files={"file": ("big.bin", b"x" * 64, "application/octet-stream")},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 413


@respx.mock
async def test_a_rejected_share_surfaces_as_400() -> None:
    upload_routes(share_status=404)
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            data={"room": "alerts"},
            files={"file": ("a.txt", b"x", "text/plain")},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 400


async def test_the_text_only_contract_is_unchanged() -> None:
    # The original JSON shape must behave exactly as before.
    with respx.mock:
        message_route()
        async for client in client_for(make_config(**UPLOADS)):
            response = await client.post(
                "/notify",
                json={"room": "alerts", "message": "hi"},
                headers={"Authorization": "Bearer alert-token"},
            )
    assert response.status_code == 201
    assert response.json() == {"ok": True, "room": ROOM, "messageId": 1}


@pytest.mark.parametrize(
    "value, expected",
    [
        (104857600, "100 MB"),
        (26214400, "25 MB"),
        (536870912, "512 MB"),
        (1048576, "1 MB"),
        (1572864, "1.5 MB"),
        (0, "0 MB"),
    ],
)
def test_byte_counts_read_as_megabytes(value: int, expected: str) -> None:
    assert megabytes(value) == expected


async def test_startup_names_the_upload_user_folder_and_limit(caplog) -> None:
    config = make_config(max_upload_bytes=104857600, **UPLOADS)
    with caplog.at_level(logging.INFO):
        async for _client in client_for(config):
            pass
    assert "attachments:    as sable-bot into /sable, up to 100 MB" in caplog.text


async def test_startup_says_when_attachments_are_off(caplog) -> None:
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(notify_token="t")):
            pass
    assert "attachments:    disabled (set SABLE_NEXTCLOUD_USER" in caplog.text


async def test_startup_lists_ignored_users(caplog) -> None:
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(ignore_users=["alice", "users/bob"])):
            pass
    assert "ignoring:       alice, users/bob" in caplog.text


async def test_startup_says_when_nobody_is_ignored(caplog) -> None:
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config()):
            pass
    assert "ignoring:       (nobody)" in caplog.text


# --------------------------------------------------------------------------- #
# /hook/{name}: webhooks from services that cannot speak /notify
# --------------------------------------------------------------------------- #

HOOKS = dict(
    hooks={"komodo": ROOM},
    hook_tokens={"komodo": "hook-token"},
)

KOMODO_PAYLOAD = {
    "level": "CRITICAL",
    "resolved": False,
    "data": {
        "type": "StackStateChange",
        "data": {"name": "sable", "server_name": "prod-1", "from": "Running", "to": "Unhealthy"},
    },
}


@respx.mock
async def test_a_hook_posts_a_rendered_message() -> None:
    route = message_route()
    async for client in client_for(make_config(**HOOKS)):
        response = await client.post(
            "/hook/komodo",
            json=KOMODO_PAYLOAD,
            headers={"Authorization": "Bearer hook-token"},
        )
    assert response.status_code == 201
    assert response.json() == {"ok": True, "hook": "komodo", "room": ROOM, "messageId": 1}
    body = sent(route)[0]["message"]
    assert body.startswith("**CRITICAL**")
    assert "prod-1" in body and "Unhealthy" in body


@respx.mock
async def test_a_hook_accepts_its_token_in_the_query_string() -> None:
    # Komodo and friends cannot set headers.
    route = message_route()
    async for client in client_for(make_config(**HOOKS)):
        response = await client.post("/hook/komodo?token=hook-token", json=KOMODO_PAYLOAD)
    assert response.status_code == 201
    assert route.called


@respx.mock
async def test_a_hook_template_replaces_the_generic_rendering() -> None:
    route = message_route()
    config = make_config(
        hook_templates={"komodo": "{level}: {data.data.name} is {data.data.to}"}, **HOOKS
    )
    async for client in client_for(config):
        await client.post("/hook/komodo?token=hook-token", json=KOMODO_PAYLOAD)
    assert sent(route)[0]["message"] == "CRITICAL: sable is Unhealthy"


@respx.mock
async def test_a_template_asking_for_a_missing_path_still_posts(caplog) -> None:
    route = message_route()
    config = make_config(hook_templates={"komodo": "{level} {not.there}"}, **HOOKS)
    async for client in client_for(config):
        with caplog.at_level(logging.WARNING):
            await client.post("/hook/komodo?token=hook-token", json=KOMODO_PAYLOAD)
    assert sent(route)[0]["message"] == "CRITICAL ?"
    assert "not.there" in caplog.text


async def test_a_hook_needs_its_own_token() -> None:
    async for client in client_for(make_config(**HOOKS)):
        for url, headers in (
            ("/hook/komodo", {}),
            ("/hook/komodo?token=wrong", {}),
            ("/hook/komodo", {"Authorization": "Bearer wrong"}),
            ("/hook/komodo", {"Authorization": "hook-token"}),
        ):
            response = await client.post(url, json=KOMODO_PAYLOAD, headers=headers)
            assert response.status_code == 401


async def test_the_notify_token_does_not_open_a_hook() -> None:
    config = make_config(notify_token="alert-token", **HOOKS)
    async for client in client_for(config):
        response = await client.post(
            "/hook/komodo?token=alert-token", json=KOMODO_PAYLOAD
        )
    assert response.status_code == 401


async def test_an_unknown_hook_is_404() -> None:
    async for client in client_for(make_config(**HOOKS)):
        response = await client.post("/hook/grafana?token=hook-token", json=KOMODO_PAYLOAD)
    assert response.status_code == 404


@respx.mock
async def test_a_hook_accepts_a_payload_that_is_not_json() -> None:
    # Text beats a rejection: the alert still reaches the room.
    route = message_route()
    async for client in client_for(make_config(**HOOKS)):
        response = await client.post(
            "/hook/komodo?token=hook-token",
            content=b"disk is full",
            headers={"Content-Type": "text/plain"},
        )
    assert response.status_code == 201
    assert sent(route)[0]["message"] == "disk is full"


async def test_an_oversized_payload_is_refused() -> None:
    async for client in client_for(make_config(max_hook_bytes=64, **HOOKS)):
        response = await client.post(
            "/hook/komodo?token=hook-token", json={"padding": "x" * 500}
        )
    assert response.status_code == 413
    assert "SABLE_MAX_HOOK_BYTES" in response.json()["detail"]


async def test_startup_lists_the_hooks(caplog) -> None:
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(**HOOKS)):
            pass
    assert f"hooks:          /hook/komodo -> {ROOM}" in caplog.text


# --------------------------------------------------------------------------- #
# The ceiling on concurrent replies
# --------------------------------------------------------------------------- #


class GatedLLM(FakeLLM):
    """A FakeLLM that parks inside complete() until it is released.

    ``peak`` is the whole point: how many completions were open at once, which is
    the number SABLE_MAX_CONCURRENT_REPLIES exists to bound.
    """

    def __init__(self, reply: str = "answered") -> None:
        super().__init__(reply=reply)
        self.release = asyncio.Event()
        self.open = 0
        self.peak = 0

    async def complete(self, messages: list[Message], *, model: str | None = None) -> str:
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            await self.release.wait()
            return await super().complete(messages, model=model)
        finally:
            self.open -= 1


async def ask_twice(client: httpx.AsyncClient) -> None:
    """Two mentions, two message ids, two randoms - two replies to run."""
    for message_id, random in ((101, "a" * 64), (102, "b" * 64)):
        body = post_body(message_payload("@sable hello", message_id=message_id))
        response = await client.post(
            "/webhook", content=body, headers=signed(body, random=random)
        )
        # The 200 never waits for a slot: Talk times out long before a model call
        # comes back, so only the work behind it is allowed to queue.
        assert response.status_code == 200


@respx.mock
async def test_the_reply_ceiling_holds_the_second_model_call_until_the_first_is_done() -> None:
    route = message_route()
    llm = GatedLLM()
    async for client in client_for(make_config(max_concurrent_replies=1), llm=llm):
        await ask_twice(client)
        await settle()
        assert llm.open == 1, "the second reply should be waiting for a slot"
        llm.release.set()
        await wait_for_calls(route, 2)
    assert llm.peak == 1
    assert len(route.calls) == 2


@respx.mock
async def test_lifting_the_ceiling_lets_both_model_calls_run_at_once() -> None:
    route = message_route()
    llm = GatedLLM()
    async for client in client_for(make_config(max_concurrent_replies=0), llm=llm):
        await ask_twice(client)
        await settle()
        assert llm.open == 2
        llm.release.set()
        await wait_for_calls(route, 2)
    assert llm.peak == 2


@respx.mock
async def test_a_reply_still_queued_for_a_slot_is_drained_at_shutdown() -> None:
    """The drain covers the ones that never started, not just the ones in flight:
    each of them was promised a 200 before it was queued."""
    route = message_route()
    llm = GatedLLM()
    async for client in client_for(make_config(max_concurrent_replies=1), llm=llm):
        await ask_twice(client)
        await settle()
        assert not route.called
        # Released, but not yet given a chance to run: the drain in the lifespan
        # is what gets both of them posted.
        llm.release.set()
    assert len(route.calls) == 2


async def test_a_queued_reply_is_named_in_the_log_when_the_ceiling_bites(caplog) -> None:
    llm = GatedLLM()
    with respx.mock:
        message_route()
        async for client in client_for(make_config(max_concurrent_replies=1), llm=llm):
            with caplog.at_level(logging.INFO):
                await ask_twice(client)
                await settle()
                llm.release.set()
                await settle()
    assert "all 1 reply slots are busy" in caplog.text


async def test_the_reply_ceiling_is_named_at_startup(caplog) -> None:
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(max_concurrent_replies=3)):
            pass
    assert "concurrency:    up to 3 replies at once, the rest queued" in caplog.text


async def test_having_no_reply_ceiling_says_so_at_startup(caplog) -> None:
    """Off is the state worth spelling out: one event, one open model call, no
    matter how many events arrive."""
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(max_concurrent_replies=0)):
            pass
    assert "concurrency:    no ceiling (SABLE_MAX_CONCURRENT_REPLIES=0)" in caplog.text


# --------------------------------------------------------------------------- #
# Replayed webhooks
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_webhook_replayed_with_the_same_random_is_refused() -> None:
    route = message_route()
    body = post_body(message_payload("!ping"))
    headers = signed(body, random="c" * 64)
    async for client in client_for(make_config()):
        first = await client.post("/webhook", content=body, headers=headers)
        second = await client.post("/webhook", content=body, headers=headers)
        assert first.status_code == 200
        assert second.status_code == 401
        assert "already delivered" in second.json()["detail"]
        await wait_for(route)
        await settle()
    assert len(route.calls) == 1, "the replay must not produce a second reply"


@respx.mock
async def test_the_same_body_signed_again_with_a_fresh_random_is_not_a_replay() -> None:
    """A new random is a new request. Talk's own redeliveries are caught further
    in, by Bot.seen on the message id, which is why only the random is checked
    here."""
    route = message_route()
    body = post_body(message_payload("!ping"))
    async for client in client_for(make_config()):
        for random in ("d" * 64, "e" * 64):
            response = await client.post(
                "/webhook", content=body, headers=signed(body, random=random)
            )
            assert response.status_code == 200
        await wait_for(route)
        await settle()
    assert len(route.calls) == 1, "Bot.seen should still refuse to answer twice"


@respx.mock
async def test_a_rejected_signature_does_not_reserve_the_random_it_carried() -> None:
    """The cache is written only after the signature verifies. The other order
    would let anybody who can reach the port spend the randoms Talk is about to
    use, and have the genuine webhooks carrying them refused."""
    message_route()
    body = post_body(message_payload("!ping"))
    random = "f" * 64
    async for client in client_for(make_config()):
        forged = await client.post(
            "/webhook", content=body, headers=signed(body, random=random, secret="x" * 40)
        )
        genuine = await client.post(
            "/webhook", content=body, headers=signed(body, random=random)
        )
    assert forged.status_code == 401
    assert genuine.status_code == 200


@respx.mock
async def test_the_random_cache_is_bounded_and_forgets_the_oldest(monkeypatch) -> None:
    """Honest about what it is: the last SEEN_RANDOMS values, not all of them.
    Nothing expires on age - Talk sends no timestamp to age anything against."""
    monkeypatch.setattr("sable.app.SEEN_RANDOMS", 2)
    message_route()
    body = post_body(message_payload("!ping"))
    first = signed(body, random="1" * 64)

    async def post(headers: dict[str, str]) -> int:
        return (await client.post("/webhook", content=body, headers=headers)).status_code

    async for client in client_for(make_config()):
        assert await post(first) == 200
        assert await post(first) == 401
        for random in ("2" * 64, "3" * 64):
            assert await post(signed(body, random=random)) == 200
        # Two newer randoms have pushed the first one out of the deque.
        assert await post(first) == 200


@respx.mock
async def test_a_replayed_webhook_is_logged_with_the_random_it_reused(caplog) -> None:
    message_route()
    body = post_body(message_payload("!ping"))
    headers = signed(body, random="9" * 64)
    async for client in client_for(make_config()):
        await client.post("/webhook", content=body, headers=headers)
        with caplog.at_level(logging.WARNING):
            await client.post("/webhook", content=body, headers=headers)
    assert "reusing random 99999999" in caplog.text


# --------------------------------------------------------------------------- #
# Rotating SABLE_BOT_SECRET
# --------------------------------------------------------------------------- #

CURRENT = "n" * 40
PREVIOUS = "o" * 40
ROTATING = dict(bot_secret=CURRENT, bot_secret_previous=PREVIOUS)


@respx.mock
async def test_a_webhook_signed_with_the_previous_secret_is_still_accepted() -> None:
    """The window this exists for: Talk holds one secret per install, so changing
    it means a reinstall, and events signed with the old value keep arriving."""
    route = message_route()
    body = post_body(message_payload("!ping"))
    async for client in client_for(make_config(**ROTATING)):
        response = await client.post(
            "/webhook", content=body, headers=signed(body, random="g" * 64, secret=PREVIOUS)
        )
        assert response.status_code == 200
        await wait_for(route)


@respx.mock
async def test_the_current_secret_is_accepted_while_a_rotation_is_in_progress() -> None:
    route = message_route()
    body = post_body(message_payload("!ping"))
    async for client in client_for(make_config(**ROTATING)):
        response = await client.post(
            "/webhook", content=body, headers=signed(body, random="h" * 64, secret=CURRENT)
        )
        assert response.status_code == 200
        await wait_for(route)


@respx.mock
async def test_replies_are_signed_with_the_current_secret_and_never_the_previous() -> None:
    """Outgoing calls keep signing with SABLE_BOT_SECRET alone: Talk has been
    given the new value by the time the old one is in SABLE_BOT_SECRET_PREVIOUS,
    so a call signed with the old one would be refused."""
    route = message_route()
    body = post_body(message_payload("!ping"))
    async for client in client_for(make_config(**ROTATING)):
        await client.post(
            "/webhook", content=body, headers=signed(body, random="i" * 64, secret=PREVIOUS)
        )
        await wait_for(route)
    request = route.calls.last.request
    # The bot API signs over the message text, not the serialised body.
    signed_value = json.loads(request.content)["message"].encode()
    random = request.headers[HEADER_BOT_RANDOM]
    signature = request.headers[HEADER_BOT_SIGNATURE]
    assert verify(random, signature, signed_value, CURRENT)
    assert not verify(random, signature, signed_value, PREVIOUS)


async def test_the_previous_secret_stops_working_once_the_variable_is_cleared() -> None:
    body = post_body(message_payload("!ping"))
    async for client in client_for(make_config(bot_secret=CURRENT)):
        response = await client.post(
            "/webhook", content=body, headers=signed(body, random="j" * 64, secret=PREVIOUS)
        )
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Nextcloud reachability on /healthz
# --------------------------------------------------------------------------- #

STATUS_PHP = f"{BACKEND}/status.php"
STATUS_BODY = {"installed": True, "maintenance": False, "versionstring": "31.0.4"}


async def test_healthz_says_nextcloud_is_unknown_before_anything_has_been_tried() -> None:
    """None and False are different answers: nothing has failed yet."""
    async for client in client_for(make_config(startup_check=False)):
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["nextcloud"] is None
    assert response.json()["status"] == "ok"


@respx.mock
async def test_healthz_says_nextcloud_is_reachable_after_a_successful_probe() -> None:
    respx.get(STATUS_PHP).mock(return_value=httpx.Response(200, json=STATUS_BODY))
    async for client in client_for(make_config(startup_check=True)):
        response = await client.get("/healthz")
    assert response.json()["nextcloud"] is True


@respx.mock
async def test_an_unreachable_nextcloud_is_reported_without_failing_the_probe() -> None:
    """Liveness must not start failing because a dependency is down - that has an
    orchestrator restart a process that is working perfectly."""
    respx.get(STATUS_PHP).mock(side_effect=httpx.ConnectError("refused"))
    async for client in client_for(make_config(startup_check=True)):
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["nextcloud"] is False
    assert response.json()["status"] == "ok"


@respx.mock
async def test_a_guarded_healthz_still_reports_reachability() -> None:
    respx.get(STATUS_PHP).mock(return_value=httpx.Response(200, json=STATUS_BODY))
    config = make_config(startup_check=True, health_token="h" * 20)
    async for client in client_for(config):
        response = await client.get("/healthz", headers={"X-Health-Token": "h" * 20})
    assert response.json()["nextcloud"] is True


# --------------------------------------------------------------------------- #
# The conversation token on the webhook path
# --------------------------------------------------------------------------- #

#: Everything TOKEN_RE refuses, minus the empty string: an event with no
#: conversation at all never reaches this check, because parse_event has already
#: refused it for having no token to read.
NOT_TOKENS = [(label, token) for label, token in REJECTED_TOKENS if token]


@pytest.mark.parametrize(
    "token", [token for _, token in NOT_TOKENS], ids=[label for label, _ in NOT_TOKENS]
)
async def test_a_webhook_naming_something_that_is_not_a_conversation_token_is_a_400(
    token: str,
) -> None:
    """It would otherwise go straight into the path of every reply we send, and
    httpx resolves `..` before the request leaves."""
    body = post_body(message_payload("hello", room=token))
    async for client in client_for(make_config()):
        response = await client.post("/webhook", content=body, headers=signed(body))
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert TOKEN_HINT in detail
    assert repr(token) in detail


async def test_an_unusable_token_is_not_reported_as_an_unparseable_event(caplog) -> None:
    """Where the check lives shows up here: parse_event still accepts the event,
    so the log and the 400 can both name the token instead of blaming the JSON."""
    body = post_body(message_payload("hello", room="../evil"))
    with caplog.at_level(logging.INFO):
        async for client in client_for(make_config()):
            response = await client.post("/webhook", content=body, headers=signed(body))
    assert response.status_code == 400
    assert "refusing a webhook for conversation '../evil'" in caplog.text
    assert "unparseable event" not in caplog.text


@pytest.mark.parametrize(
    "token",
    [token for _, token in VALID_TOKENS],
    ids=[label for label, _ in VALID_TOKENS],
)
async def test_every_token_the_regex_accepts_still_reaches_the_bot(token: str) -> None:
    # Plain text, no mention, not an AI room: accepted and handled, and nothing
    # is sent anywhere, so no respx mock is needed to prove the token got through.
    body = post_body(message_payload("hello", room=token))
    async for client in client_for(make_config()):
        response = await client.post("/webhook", content=body, headers=signed(body))
        assert response.status_code == 200
        await settle()


# --------------------------------------------------------------------------- #
# Two startup lines that have to be honest
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "proxies", [["127.0.0.1", "::1"], ["*"], []], ids=["a_list", "every_client", "nobody"]
)
async def test_the_proxy_trust_line_names_the_server_the_setting_is_on(
    proxies: list[str], caplog
) -> None:
    """SABLE_TRUSTED_PROXIES is read by __main__ and handed to uvicorn.run, so a
    deployment serving create_app() under anything else never applies it. The line
    is logged from here regardless, so it has to say whose setting it is."""
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(trusted_proxies=proxies)):
            pass
    line = next(line for line in caplog.messages if "proxy trust:" in line)
    assert "uvicorn" in line


async def test_an_ignore_entry_that_looks_like_a_display_name_is_called_out(caplog) -> None:
    """A display name is the one kind of entry the ignored person can defeat, by
    renaming themselves, so an operator should see it at boot and not in prose."""
    config = make_config(ignore_users=["alice", "Bob Smith"])
    with caplog.at_level(logging.INFO):
        async for _client in client_for(config):
            pass
    assert "ignoring:       alice, Bob Smith" in caplog.text
    warning = next(
        record
        for record in caplog.records
        if "SABLE_IGNORE_USERS holds whitespace" in record.getMessage()
    )
    assert warning.levelno == logging.WARNING
    assert "'Bob Smith'" in warning.getMessage()
    assert "'alice'" not in warning.getMessage()


async def test_nothing_is_said_when_every_ignore_entry_is_an_id(caplog) -> None:
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(ignore_users=["alice", "users/bob"])):
            pass
    assert "SABLE_IGNORE_USERS holds whitespace" not in caplog.text
