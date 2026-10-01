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
    PASSWORD,
    ROOM,
    TALK,
    USER,
    FakeLLM,
    make_config,
    message_payload,
)

from sable.app import create_app, megabytes
from sable.bot import Bot
from sable.config import Config
from sable.llm import Message

MESSAGE_URL = f"{TALK}/chat/{ROOM}"
USER_URL = f"{BACKEND}/ocs/v2.php/cloud/user"


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
                # So a test can feed the poller's dispatch without a Talk server.
                client.app = app  # type: ignore[attr-defined]
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
    assert body["user"] == "sable"
    assert body["llm"] == "some-model"


async def test_root_is_a_plain_banner(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/")
    assert response.status_code == 200
    assert response.text.startswith("sable ")


@respx.mock
async def test_a_dispatched_message_reaches_the_bot_and_is_answered() -> None:
    route = message_route()
    llm = FakeLLM(reply="42")
    async for client in client_for(make_config(), llm=llm):
        client.app.state.poller._dispatch(ROOM, message_payload("@sable what is 6*7"))
        await wait_for(route)
    assert json.loads(route.calls.last.request.content)["message"] == "42"
    assert llm.last_prompt == "Alice: what is 6*7"


@respx.mock
async def test_a_poller_is_not_started_for_a_bot_handed_in_by_a_test() -> None:
    async for client in client_for(make_config()):
        assert client.app.state.poller.following == []


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
    assert f"nextcloud:      {BACKEND} as sable" in text
    assert "receiving:      long polls of up to 30s, conversations rescanned every 60s" in text
    assert "command prefix: '!'" in text
    assert "some-model at https://api.openai.com/v1" in text
    assert "alerting:       enabled, aliases: alerts" in text
    assert "backend pin" not in text
    assert PASSWORD not in text
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


async def test_the_admin_commands_are_named_at_startup(caplog) -> None:
    config = make_config(
        admin_commands=["*"], normal_commands=["help", "ping"], admin_users=["maser"]
    )
    with caplog.at_level(logging.INFO):
        async for _client in client_for(config):
            pass
    assert "admin commands: *, except help, ping - only for maser" in caplog.text


@respx.mock
async def test_the_startup_check_signs_in_when_enabled(caplog) -> None:
    route = respx.get(USER_URL).mock(
        return_value=httpx.Response(
            200, json={"ocs": {"meta": {}, "data": {"id": "sable", "displayname": "Sable"}}}
        )
    )
    with caplog.at_level(logging.INFO):
        async for _client in client_for(make_config(startup_check=True)):
            pass
    assert route.called
    assert f"signed in to {BACKEND} as sable (Sable)" in caplog.text


@respx.mock
async def test_a_refused_password_is_logged_as_an_error_at_startup(caplog) -> None:
    respx.get(USER_URL).mock(return_value=httpx.Response(401, text="no"))
    with caplog.at_level(logging.INFO):
        async for client in client_for(make_config(startup_check=True)):
            # Not fatal: the process stays up and says so on /healthz.
            assert (await client.get("/healthz")).status_code == 200
    assert any(
        r.levelno == logging.ERROR and "SABLE_NEXTCLOUD_PASSWORD" in r.getMessage()
        for r in caplog.records
    )
    assert PASSWORD not in caplog.text


async def test_the_startup_check_can_be_turned_off() -> None:
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

DAV = f"{BACKEND}/remote.php/dav/files/{USER}"
UPLOADS = dict(notify_token="alert-token", notify_rooms={"alerts": ROOM})


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


@respx.mock
async def test_an_attachment_is_uploaded_and_shared_as_the_chat_account() -> None:
    """One credential throughout: the account that reads and posts chat is the one
    that owns the upload and makes the share, with nothing extra to configure."""
    put, share = upload_routes()
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
    assert response.status_code == 201
    assert put.calls.last.request.url.path.startswith(f"/remote.php/dav/files/{USER}/sable/")
    expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    assert put.calls.last.request.headers["authorization"] == f"Basic {expected}"
    assert share.calls.last.request.headers["authorization"] == f"Basic {expected}"


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
    assert "attachments:    into /sable, up to 100 MB" in caplog.text


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
    """Two mentions, two message ids - two replies to run."""
    for message_id in (101, 102):
        client.app.state.poller._dispatch(  # type: ignore[attr-defined]
            ROOM, message_payload("@sable hello", message_id=message_id)
        )


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
    each of them was already read from the conversation before it was queued."""
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
# Nextcloud reachability on /healthz
# --------------------------------------------------------------------------- #

STATUS_PHP = USER_URL
STATUS_BODY = {"ocs": {"meta": {}, "data": {"id": "sable", "displayname": "Sable"}}}


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


# --------------------------------------------------------------------------- #
# Request body caps, enforced by the app itself
# --------------------------------------------------------------------------- #


async def raw_post(
    app, path: str, chunks, headers: list[tuple[bytes, bytes]], *, query: bytes = b""
) -> tuple[int, bytes, int]:
    """POST straight at the ASGI app, feeding ``chunks`` one per receive().

    Returns (status, response body, how many chunks the app pulled). Bypassing
    httpx is what makes a lying Content-Length expressible.
    """
    pulled = 0
    iterator = iter(chunks)
    sent_messages: list[dict] = []

    async def receive() -> dict:
        nonlocal pulled
        try:
            chunk = next(iterator)
        except StopIteration:
            return {"type": "http.disconnect"}
        pulled += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: dict) -> None:
        sent_messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("sable.test", 80),
        "app": app,
    }
    await app(scope, receive, send)
    start = next(m for m in sent_messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent_messages if m["type"] == "http.response.body")
    return start["status"], body, pulled


async def test_a_declared_length_over_the_cap_is_refused_before_the_body_is_read() -> None:
    config = make_config(max_upload_bytes=1024, **UPLOADS)
    async for client in client_for(config):
        status_code, body, pulled = await raw_post(
            client.app,  # type: ignore[attr-defined]
            "/notify",
            [b"x" * 10],
            [
                (b"content-type", b"application/json"),
                (b"content-length", b"10000000"),
                (b"authorization", b"Bearer alert-token"),
            ],
        )
    assert status_code == 413
    assert pulled == 0, "not one byte of the body should have been read"
    assert "larger than" in json.loads(body)["detail"]


async def test_a_streamed_body_is_cut_off_the_moment_it_passes_the_cap() -> None:
    config = make_config(max_upload_bytes=1024, **UPLOADS)
    cap = 1366 + 64 * 1024  # ceil(1024 * 4 / 3) + 64 KiB
    chunks = [b"x" * 16384] * 100  # far more than the cap, no Content-Length at all
    async for client in client_for(config):
        status_code, body, pulled = await raw_post(
            client.app,  # type: ignore[attr-defined]
            "/notify",
            chunks,
            [
                (b"content-type", b"application/json"),
                (b"transfer-encoding", b"chunked"),
                (b"authorization", b"Bearer alert-token"),
            ],
        )
    assert status_code == 413
    assert json.loads(body)["detail"]
    assert pulled * 16384 <= cap + 16384, "the app kept reading after the cap"


async def test_a_lying_content_length_does_not_get_past_the_cap() -> None:
    config = make_config(max_upload_bytes=1024, **UPLOADS)
    async for client in client_for(config):
        status_code, _, pulled = await raw_post(
            client.app,  # type: ignore[attr-defined]
            "/notify",
            [b"y" * 40000] * 5,
            [
                (b"content-type", b"application/json"),
                (b"content-length", b"12"),
                (b"authorization", b"Bearer alert-token"),
            ],
        )
    assert status_code == 413
    assert pulled == 2  # 80000 > the 67 KiB cap: stopped on the second read


async def test_a_chunked_body_over_the_cap_is_a_clean_413_through_httpx() -> None:
    async def body() -> AsyncIterator[bytes]:
        for _ in range(50):
            yield b"z" * 8192

    config = make_config(max_upload_bytes=1024, **UPLOADS)
    async for client in client_for(config):
        response = await client.post(
            "/notify",
            content=body(),
            headers={"Authorization": "Bearer alert-token", "Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/json"


async def test_an_oversized_multipart_upload_never_reaches_the_form_parser(monkeypatch) -> None:
    from starlette.requests import Request

    async def boom(self, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the form parser ran on an oversized body")

    monkeypatch.setattr(Request, "_get_form", boom)
    async for client in client_for(make_config(max_upload_bytes=1024, **UPLOADS)):
        response = await client.post(
            "/notify",
            data={"room": "alerts"},
            files={"file": ("big.bin", b"x" * 200_000, "application/octet-stream")},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 413


@respx.mock
async def test_a_file_within_the_notify_cap_still_goes_through() -> None:
    """The cap is ceil(N * 4 / 3) + 64 KiB, so a base64 file of exactly N bytes fits."""
    upload_routes()
    config = make_config(max_upload_bytes=30000, **UPLOADS)
    async for client in client_for(config):
        response = await client.post(
            "/notify",
            json={
                "room": "alerts",
                "file": {"name": "ok.bin", "content": base64.b64encode(b"x" * 30000).decode()},
            },
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 201


async def test_the_hook_cap_is_the_setting_plus_a_little_slack() -> None:
    async for client in client_for(make_config(max_hook_bytes=64, **HOOKS)):
        small = await client.post("/hook/komodo?token=hook-token", content=b"x" * 5000)
        status_code, _, pulled = await raw_post(
            client.app,  # type: ignore[attr-defined]
            "/hook/komodo",
            [b"x" * 5000],
            [(b"content-length", b"5000")],
            query=b"token=hook-token",
        )
    assert small.status_code == 413
    assert status_code == 413 and pulled == 0


async def test_every_other_route_is_capped_at_64_kib() -> None:
    async for client in client_for(make_config()):
        response = await client.post("/healthz", content=b"x" * (64 * 1024 + 1))
        ok = await client.post("/healthz", content=b"x" * 1000)
    assert response.status_code == 413
    assert ok.status_code == 405


async def test_the_cap_comes_before_authentication() -> None:
    """It reveals nothing, so a bad token and a big body is simply a 413."""
    async for client in client_for(make_config(max_upload_bytes=1024, **UPLOADS)):
        response = await client.post(
            "/notify",
            content=b"x" * 200_000,
            headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"},
        )
    assert response.status_code == 413


async def test_a_small_unauthenticated_notify_is_still_401() -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post("/notify", json={"room": "alerts", "message": "x"})
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Secrets are compared as bytes
# --------------------------------------------------------------------------- #

ODD_SECRETS = ["é", "\U0001f600", "café-token", "\ud800", "‮"]


def test_same_secret_never_raises_on_non_ascii() -> None:
    from sable.app import _same_secret

    for odd in ODD_SECRETS:
        assert _same_secret(odd, "alert-token") is False
        assert _same_secret(odd, odd) is True
        assert _same_secret("alert-token", odd) is False


async def test_non_ascii_bearer_tokens_are_401_not_500() -> None:
    async for client in client_for(make_config(**UPLOADS)):
        for odd in ("é", "\U0001f600", "ÿþ"):
            response = await client.post(
                "/notify",
                json={"room": "alerts", "message": "x"},
                headers={"Authorization": b"Bearer " + odd.encode("utf-8")},
            )
            assert response.status_code == 401, odd


async def test_non_ascii_hook_tokens_are_401_not_500() -> None:
    async for client in client_for(make_config(**HOOKS)):
        for query in ("%C3%A9", "%F0%9F%98%80", "%ED%A0%80", "%FF%FE"):
            response = await client.post(f"/hook/komodo?token={query}", json={"a": 1})
            assert response.status_code == 401, query
        response = await client.post(
            "/hook/komodo",
            json={"a": 1},
            headers={"Authorization": b"Bearer " + "é".encode("utf-8")},
        )
        assert response.status_code == 401


async def test_non_ascii_health_tokens_are_401_not_500() -> None:
    async for client in client_for(make_config(health_token="h" * 20)):
        for odd in ("é", "\U0001f600"):
            response = await client.get(
                "/healthz", headers={"X-Health-Token": odd.encode("utf-8")}
            )
            assert response.status_code == 401, odd


# --------------------------------------------------------------------------- #
# A body that is wrong is a 4xx, never a 500
# --------------------------------------------------------------------------- #

AUTH = {"Authorization": "Bearer alert-token"}


@pytest.mark.parametrize("body", [[1, 2], "text", 5, True, 1.5])
async def test_a_json_body_that_is_not_an_object_is_422(body) -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post("/notify", json=body, headers=AUTH)
    assert response.status_code == 422
    assert "JSON object" in response.json()["detail"]


async def test_a_json_null_body_is_422() -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify", content=b"null", headers={**AUTH, "Content-Type": "application/json"}
        )
    assert response.status_code == 422
    assert "null" in response.json()["detail"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "room"),
        ({"room": ""}, "room"),
        ({"room": "alerts", "message": 5}, "message"),
        ({"room": "alerts", "replyTo": -1}, "replyTo"),
        ({"room": "alerts", "replyTo": "abc"}, "replyTo"),
        ({"room": "alerts", "silent": "perhaps"}, "silent"),
        ({"room": "alerts", "file": "nope"}, "file"),
        ({"room": "alerts", "file": {"name": "", "content": "eA=="}}, "file.name"),
    ],
)
async def test_an_invalid_notify_body_is_422_with_the_field_named(body, field) -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post("/notify", json=body, headers=AUTH)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) and field in detail
    assert "Traceback" not in detail and "pydantic" not in detail


async def test_a_deeply_nested_notify_body_is_a_4xx() -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            content=b"[" * 30_000 + b"]" * 30_000,
            headers={**AUTH, "Content-Type": "application/json"},
        )
    assert response.status_code == 400


async def test_invalid_utf8_notify_json_is_400() -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            content=b'{"room": "' + bytes([0xFF, 0xFE]) + b'"}',
            headers={**AUTH, "Content-Type": "application/json"},
        )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "data",
    [{}, {"room": ""}, {"room": "alerts", "replyTo": "-3"}, {"room": "alerts", "replyTo": "x"}],
)
async def test_an_invalid_multipart_notify_is_422(data) -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            data=data,
            files={"file": ("a.txt", b"hi", "text/plain")},
            headers=AUTH,
        )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


async def test_a_multipart_notify_with_a_huge_field_is_a_4xx() -> None:
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            data={"room": "alerts", "message": "m" * (300 * 1024)},
            files={"file": ("a.txt", b"hi", "text/plain")},
            headers=AUTH,
        )
    assert response.status_code == 400  # Starlette: "Part exceeded maximum size"


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        bytes([0, 1, 2, 255, 254]) + b" binary",
        bytes([255, 254, 250]),
        b"[" * 50_000,
        b"[" * 600 + b"]" * 600,
        b"9" * 5000,
    ],
)
async def test_a_hook_survives_odd_bodies(body: bytes) -> None:
    route = message_route()
    async for client in client_for(make_config(**HOOKS)):
        response = await client.post("/hook/komodo?token=hook-token", content=body)
    assert response.status_code in {201, 422}, response.text
    if response.status_code == 201:
        assert route.called


@respx.mock
async def test_a_hook_with_a_template_survives_deep_nesting() -> None:
    route = message_route()
    config = make_config(
        hooks={"komodo": ROOM},
        hook_tokens={"komodo": "hook-token"},
        hook_templates={"komodo": "got: {a}"},
    )
    async for client in client_for(config):
        response = await client.post(
            "/hook/komodo?token=hook-token", content=b"[" * 800 + b"]" * 800
        )
    assert response.status_code == 201
    assert route.called


# --------------------------------------------------------------------------- #
# Hook text cannot mention everyone
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_hook_cannot_ping_everyone() -> None:
    route = message_route()
    async for client in client_for(make_config(**HOOKS)):
        response = await client.post(
            "/hook/komodo?token=hook-token", json={"message": "disk full @all see @\"team/ops\" x"}
        )
    assert response.status_code == 201
    text = sent(route)[0]["message"]
    assert "@all" not in text and '@"team/' not in text
    assert "all" in text and "disk full" in text


@respx.mock
async def test_notify_leaves_mentions_alone() -> None:
    route = message_route()
    async for client in client_for(make_config(**NOTIFY_CONFIG)):
        await client.post(
            "/notify",
            json={"room": "alerts", "message": "hello @all"},
            headers=AUTH,
        )
    assert sent(route)[0]["message"] == "hello @all"


# --------------------------------------------------------------------------- #
# The bound on replies waiting for a slot
# --------------------------------------------------------------------------- #


def dispatch(client: httpx.AsyncClient, *ids: int) -> None:
    for message_id in ids:
        client.app.state.poller._dispatch(  # type: ignore[attr-defined]
            ROOM, message_payload("@sable hello", message_id=message_id)
        )


@respx.mock
async def test_replies_past_the_queue_bound_are_dropped(caplog) -> None:
    route = message_route()
    llm = GatedLLM()
    config = make_config(max_concurrent_replies=1, max_queued_replies=20, rate_limit=0)
    async for client in client_for(config, llm=llm):
        with caplog.at_level(logging.WARNING):
            dispatch(client, *range(100, 122))  # 22: 1 running + 20 waiting + 1 over
            await settle(200)
            assert llm.open == 1
            llm.release.set()
            await wait_for_calls(route, 21)
            await settle(200)
    assert len(route.calls) == 21, "the 22nd should have been dropped, the rest answered"
    assert caplog.text.count("dropping replies") == 1


@respx.mock
async def test_chatter_and_our_own_replies_do_not_take_queue_slots() -> None:
    """With the one slot busy and room for one waiter, events that handle would
    ignore anyway must not fill the queue and cost a real trigger its place."""
    route = message_route()
    llm = GatedLLM()
    config = make_config(max_concurrent_replies=1, max_queued_replies=1, rate_limit=0)
    async for client in client_for(config, llm=llm):
        poller = client.app.state.poller  # type: ignore[attr-defined]
        poller._dispatch(ROOM, message_payload("@sable hello", message_id=101))  # runs
        await settle()
        assert llm.open == 1
        for n in range(110, 120):
            poller._dispatch(ROOM, message_payload("just chatting", message_id=n))
        poller._dispatch(
            ROOM, message_payload("a reply", message_id=130, actor_id="users/sable")
        )
        poller._dispatch(
            ROOM, message_payload("beep", message_id=131, actor_id="bots/relay")
        )
        poller._dispatch(ROOM, message_payload("@sable second", message_id=140))  # waits
        poller._dispatch(ROOM, message_payload("@sable third", message_id=141))  # over
        await settle()
        llm.release.set()
        await wait_for_calls(route, 2)
        await settle(200)
    assert len(route.calls) == 2, "the second trigger queued, the third was over the bound"


@respx.mock
async def test_a_queue_of_zero_lets_nothing_wait() -> None:
    route = message_route()
    llm = GatedLLM()
    config = make_config(max_concurrent_replies=1, max_queued_replies=0)
    async for client in client_for(config, llm=llm):
        dispatch(client, 101, 102)
        await settle()
        llm.release.set()
        await wait_for_calls(route, 1)
        await settle(200)
    assert len(route.calls) == 1


@respx.mock
async def test_slots_free_up_so_later_replies_are_accepted_again() -> None:
    route = message_route()
    llm = GatedLLM()
    llm.release.set()
    config = make_config(max_concurrent_replies=1, max_queued_replies=0)
    async for client in client_for(config, llm=llm):
        dispatch(client, 101)
        await wait_for_calls(route, 1)
        await settle(200)
        dispatch(client, 102)
        await wait_for_calls(route, 2)
    assert len(route.calls) == 2


async def test_the_drop_warning_is_rate_limited(caplog) -> None:
    llm = GatedLLM()
    with respx.mock:
        message_route()
        config = make_config(max_concurrent_replies=1, max_queued_replies=0)
        async for client in client_for(config, llm=llm):
            with caplog.at_level(logging.WARNING):
                dispatch(client, *range(100, 140))
                await settle()
                llm.release.set()
                await settle(200)
    assert caplog.text.count("dropping replies") == 1


async def test_a_dropped_reply_is_closed_not_left_unawaited() -> None:
    llm = GatedLLM()
    config = make_config(max_concurrent_replies=1, max_queued_replies=0)
    with respx.mock:
        message_route()
        async for client in client_for(config, llm=llm):
            spawn = client.app.state.poller._spawn  # type: ignore[attr-defined]

            async def work() -> None:
                await asyncio.sleep(0)

            first = work()
            spawn(first)  # takes the only slot
            second = work()
            spawn(second)  # dropped
            assert second.cr_frame is None, "a dropped coroutine must be closed"
            llm.release.set()
            await settle()


async def test_with_no_ceiling_there_is_no_queue_to_bound() -> None:
    llm = GatedLLM()
    config = make_config(max_concurrent_replies=0, max_queued_replies=0)
    with respx.mock:
        message_route()
        async for client in client_for(config, llm=llm):
            dispatch(client, 101, 102, 103)
            await settle()
            assert llm.open == 3
            llm.release.set()
            await settle(200)


async def test_the_queue_bound_is_named_at_startup(caplog) -> None:
    with caplog.at_level(logging.INFO):
        async for _client in client_for(
            make_config(max_concurrent_replies=3, max_queued_replies=7)
        ):
            pass
    assert "at most 7 waiting" in caplog.text


# --------------------------------------------------------------------------- #
# Review additions: legitimate large and tiny-limit /notify calls
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_multipart_file_far_larger_than_the_field_limit_goes_through() -> None:
    """max_part_size caps fields; an upload of several MB is a file, not a field."""
    put, _ = upload_routes()
    async for client in client_for(make_config(**UPLOADS)):
        response = await client.post(
            "/notify",
            data={"room": "alerts", "message": "big", "silent": "false", "replyTo": "3"},
            files={"file": ("big.bin", b"x" * 3_000_000, "application/octet-stream")},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 201, response.text
    assert len(put.calls.last.request.content) == 3_000_000


@respx.mock
@pytest.mark.parametrize("limit", [1, 2])
async def test_a_tiny_upload_limit_still_allows_a_normal_json_message(limit: int) -> None:
    route = message_route()
    async for client in client_for(make_config(max_upload_bytes=limit, **UPLOADS)):
        response = await client.post(
            "/notify",
            json={"room": "alerts", "message": "m" * 40_000},
            headers={"Authorization": "Bearer alert-token"},
        )
    assert response.status_code == 201, response.text
    assert route.called


async def test_the_body_cap_passes_lifespan_and_websocket_scopes_untouched() -> None:
    from sable.limits import BodyLimitMiddleware

    seen: list[str] = []

    async def inner(scope, receive, send) -> None:
        seen.append(scope["type"])

    wrapped = BodyLimitMiddleware(inner, cap_for=lambda method, path: 1)
    for kind in ("lifespan", "websocket"):
        # No "method"/"path" in these scopes: reading them would raise KeyError.
        await wrapped({"type": kind}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "websocket"]
