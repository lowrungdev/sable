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
        await client.complete(MESSAGES)
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
async def test_it_waits_for_the_loop_to_finish() -> None:
    owui = Instance()
    owui.pending = 3
    owui.install()
    client = make_client()
    try:
        await client.complete(MESSAGES)
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
            await client.complete(MESSAGES)
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
