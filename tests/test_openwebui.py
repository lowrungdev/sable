"""Open WebUI's agentic loop, which is four calls pretending to be one."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from sable.config import LLMConfig
from sable.llm import LLMError
from sable.openwebui import OpenWebUIClient

BASE = "https://ai.example.org/api"
NEW_CHAT = f"{BASE}/v1/chats/new"
COMPLETIONS = f"{BASE}/chat/completions"
TASKS = f"{BASE}/tasks/chat/chat-1"
CHAT = f"{BASE}/v1/chats/chat-1"

MESSAGES = [
    {"role": "system", "content": "you are sable"},
    {"role": "user", "content": "what is gold worth"},
]


def make_client(**overrides: Any) -> OpenWebUIClient:
    settings: dict[str, Any] = {
        "base_url": BASE,
        "api_key": "sk-test",
        "model": "gemma-focused",
        "backend": "openwebui",
        "tool_ids": ["server:mcp:1"],
        "features": ["web_search"],
        "poll_interval": 0.0,
    }
    settings.update(overrides)
    return OpenWebUIClient(LLMConfig(**settings))


class Instance:
    """A stand-in Open WebUI that remembers the message id sable invented."""

    def __init__(self, answer: str = "$4,144.60 an ounce", **message: Any) -> None:
        self.assistant_id = ""
        self.answer = answer
        self.extra = message
        self.deleted = False
        self.polls = 0
        self.pending = 1

    def install(self, *, tasks: bool = True) -> None:
        respx.post(NEW_CHAT).mock(side_effect=self._new_chat)
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(200, json={"status": True, "task_ids": ["t1"]})
        )
        if tasks:
            respx.get(TASKS).mock(side_effect=self._tasks)
        respx.get(CHAT).mock(side_effect=self._chat)
        respx.delete(CHAT).mock(side_effect=self._delete)

    def _new_chat(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.assistant_id = body["chat"]["history"]["currentId"]
        return httpx.Response(200, json={"id": "chat-1"})

    def _tasks(self, request: httpx.Request) -> httpx.Response:
        self.polls += 1
        remaining = ["t1"] if self.polls <= self.pending else []
        return httpx.Response(200, json={"task_ids": remaining})

    def _chat(self, request: httpx.Request) -> httpx.Response:
        message = {"id": self.assistant_id, "content": self.answer, **self.extra}
        return httpx.Response(
            200,
            json={"chat": {"history": {"messages": {self.assistant_id: message}}}},
        )

    def _delete(self, request: httpx.Request) -> httpx.Response:
        self.deleted = True
        return httpx.Response(200, json=True)


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_answer_comes_from_the_chat_record_not_the_response() -> None:
    owui = Instance()
    owui.install()
    client = make_client()
    try:
        assert await client.complete(MESSAGES) == "$4,144.60 an ounce"
    finally:
        await client.aclose()
    assert owui.deleted, "the conversation existed only to be written into"


@respx.mock
async def test_the_request_carries_what_makes_the_loop_run() -> None:
    owui = Instance()
    owui.install()
    client = make_client()
    try:
        await client.complete(MESSAGES, tools=True)
    finally:
        await client.aclose()

    body = json.loads(respx.calls[1].request.content)
    # All three together, or Open WebUI executes nothing at all.
    assert body["stream"] is True
    assert body["chat_id"] == "chat-1"
    assert body["id"] == owui.assistant_id
    # A session id is what puts the built-in tools in front of the model.
    assert body["session_id"].startswith("sable-")
    assert body["features"] == {
        "code_interpreter": False,
        "image_generation": False,
        "memory": False,
        "web_search": True,
    }
    assert body["tool_ids"] == ["server:mcp:1"]
    assert body["messages"] == MESSAGES
    # Three extra model calls we have no use for.
    assert body["background_tasks"] == {
        "title_generation": False,
        "tags_generation": False,
        "follow_up_generation": False,
    }
    assert "tools" not in body, "sending tools makes Open WebUI skip its own"


@respx.mock
async def test_without_the_room_gate_no_tool_ids_and_no_features_are_sent() -> None:
    """tools defaults to off: a call that was not told its room may use tools
    gets none, whatever SABLE_LLM_TOOL_IDS and SABLE_LLM_FEATURES say."""
    owui = Instance()
    owui.install(tasks=False)  # a tools-off room must never poll
    client = make_client()
    try:
        assert await client.complete(MESSAGES) == "$4,144.60 an ounce"
        assert await client.complete(MESSAGES, tools=False) == "$4,144.60 an ounce"
    finally:
        await client.aclose()

    bodies = [
        json.loads(call.request.content)
        for call in respx.calls
        if call.request.url.path.endswith("/chat/completions")
    ]
    assert len(bodies) == 2
    for body in bodies:
        # The blocking variant: a session id would bring Open WebUI's knowledge,
        # files, notes, channels and calendar tools with it, flag or no flag.
        assert body.pop("id")  # a fresh assistant message id per question
        assert body == {
            "model": "gemma-focused",
            "messages": MESSAGES,
            "stream": True,
            "chat_id": "chat-1",
            "background_tasks": {
                "title_generation": False,
                "tags_generation": False,
                "follow_up_generation": False,
            },
        }
    assert owui.deleted, "chat discarding still works in blocking mode"
    assert not any("/tasks/" in call.request.url.path for call in respx.calls)


@respx.mock
async def test_it_waits_for_the_loop_to_finish() -> None:
    owui = Instance()
    owui.pending = 3
    owui.install()
    client = make_client()
    try:
        await client.complete(MESSAGES, tools=True)
    finally:
        await client.aclose()
    assert owui.polls == 4


@respx.mock
async def test_the_key_travels_as_a_bearer_token() -> None:
    Instance().install()
    client = make_client()
    try:
        await client.complete(MESSAGES)
    finally:
        await client.aclose()
    assert respx.calls[0].request.headers["authorization"] == "Bearer sk-test"


# --------------------------------------------------------------------------- #
# The blocking variant
# --------------------------------------------------------------------------- #


@respx.mock
async def test_without_builtins_there_is_nothing_to_poll() -> None:
    owui = Instance()
    owui.install(tasks=False)
    client = make_client(builtin_tools=False, features=[])
    try:
        assert await client.complete(MESSAGES) == "$4,144.60 an ounce"
    finally:
        await client.aclose()

    body = json.loads(respx.calls[1].request.content)
    assert "session_id" not in body and "features" not in body


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_loop_that_never_finishes_names_the_setting() -> None:
    owui = Instance()
    owui.pending = 99
    owui.install()
    client = make_client(timeout=0.0)
    try:
        with pytest.raises(LLMError, match="SABLE_LLM_TIMEOUT"):
            await client.complete(MESSAGES, tools=True)
    finally:
        await client.aclose()
    assert owui.deleted, "a conversation is not left behind when the loop hangs"


@respx.mock
async def test_an_empty_answer_points_at_the_setting_that_causes_it() -> None:
    # The model's own Stream Chat Response parameter overrides stream: true in
    # the request, and then no tool runs and nothing is written.
    owui = Instance(answer="")
    owui.install()
    client = make_client()
    try:
        with pytest.raises(LLMError, match="Stream Chat Response"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_an_http_failure_is_reported_with_its_status() -> None:
    respx.post(NEW_CHAT).mock(return_value=httpx.Response(401, text="no"))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="HTTP 401"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_a_transport_failure_is_reported() -> None:
    respx.post(NEW_CHAT).mock(side_effect=httpx.ConnectError("refused"))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="could not reach"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_a_failed_delete_does_not_lose_the_answer() -> None:
    owui = Instance()
    owui.install()
    respx.delete(CHAT).mock(return_value=httpx.Response(500, text="nope"))
    client = make_client()
    try:
        assert await client.complete(MESSAGES) == "$4,144.60 an ounce"
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


@respx.mock
async def test_conversations_can_be_kept() -> None:
    owui = Instance()
    owui.install()
    client = make_client(keep_chats=True)
    try:
        await client.complete(MESSAGES)
    finally:
        await client.aclose()
    assert not owui.deleted


@respx.mock
async def test_sources_can_be_appended() -> None:
    owui = Instance(
        sources=[
            {
                "source": {"name": "1_search-services__search_web"},
                "metadata": [{"source": "1_search-services__search_web"}],
            }
        ]
    )
    owui.install()
    client = make_client(show_sources=True)
    try:
        answer = await client.complete(MESSAGES)
    finally:
        await client.aclose()
    assert answer.endswith("_sources: 1_search-services__search_web_")


@respx.mock
async def test_extra_body_still_reaches_the_request() -> None:
    Instance().install()
    client = make_client(extra_body={"params": {"function_calling": "native"}})
    try:
        await client.complete(MESSAGES)
    finally:
        await client.aclose()
    body = json.loads(respx.calls[1].request.content)
    assert body["params"] == {"function_calling": "native"}


# --------------------------------------------------------------------------- #
# The chat id is the server's word, and goes into URL paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("chat_id", "quoted"),
    [
        ("a/b", "a%2Fb"),
        ("../admin", "..%2Fadmin"),
        ("x?y=1", "x%3Fy%3D1"),
        ("x#frag", "x%23frag"),
        ("two words", "two%20words"),
    ],
)
@respx.mock
async def test_a_chat_id_is_quoted_into_every_path(chat_id: str, quoted: str) -> None:
    paths: list[tuple[str, str]] = []
    assistant: dict[str, str] = {}

    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        paths.append((request.method, path))
        if path.endswith("/v1/chats/new"):
            assistant["id"] = json.loads(request.content)["chat"]["history"]["currentId"]
            return httpx.Response(200, json={"id": chat_id})
        if path.endswith("/chat/completions"):
            return httpx.Response(200, json={"status": True})
        if "/tasks/chat/" in path:
            return httpx.Response(200, json={"task_ids": []})
        if request.method == "GET":
            message = {"id": assistant["id"], "content": "fine"}
            return httpx.Response(
                200, json={"chat": {"history": {"messages": {assistant["id"]: message}}}}
            )
        return httpx.Response(200, json=True)

    respx.route(host="ai.example.org").mock(side_effect=route)
    client = make_client()
    try:
        assert await client.complete(MESSAGES, tools=True) == "fine"
    finally:
        await client.aclose()

    expected = {
        ("GET", f"/api/tasks/chat/{quoted}"),
        ("GET", f"/api/v1/chats/{quoted}"),
        ("DELETE", f"/api/v1/chats/{quoted}"),
    }
    assert expected <= set(paths)
    # And the id in the completion body is the real one, not the quoted form.
    body = json.loads(respx.calls[1].request.content)
    assert body["chat_id"] == chat_id


@respx.mock
async def test_a_blank_chat_id_is_refused() -> None:
    respx.post(NEW_CHAT).mock(return_value=httpx.Response(200, json={"id": "  "}))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="no conversation"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()
