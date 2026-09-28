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
from conftest import BACKEND, ROOM, SECRET, FakeLLM, make_config, message_payload, signed_headers

from sable.app import create_app, megabytes
from sable.bot import Bot
from sable.config import Config
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
