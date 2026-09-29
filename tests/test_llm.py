from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from sable.config import LLMConfig
from sable.llm import LLMClient, LLMError

URL = "https://llm.example.org/v1/chat/completions"
MESSAGES = [{"role": "user", "content": "hi"}]


def make_client(**overrides) -> LLMClient:
    config = LLMConfig(
        base_url="https://llm.example.org/v1",
        api_key="sk-test",
        model="some-model",
        **overrides,
    )
    return LLMClient(config)


def completion(text: str | list | None, **extra) -> dict:
    message = {"role": "assistant", "content": text}
    message.update(extra)
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


@respx.mock
async def test_sends_an_openai_shaped_request_and_returns_the_text() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=completion("hello")))
    client = make_client(temperature=0.2, max_tokens=256)
    try:
        assert await client.complete(MESSAGES) == "hello"
    finally:
        await client.aclose()

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["model"] == "some-model"
    assert body["messages"] == MESSAGES
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 256
    assert request.headers["authorization"] == "Bearer sk-test"


@respx.mock
async def test_omits_optional_parameters_and_auth_when_unset() -> None:

    route = respx.post(URL).mock(return_value=httpx.Response(200, json=completion("ok")))
    config = LLMConfig(base_url="https://llm.example.org/v1", model="local-model")
    client = LLMClient(config)
    try:
        await client.complete(MESSAGES)
    finally:
        await client.aclose()

    body = json.loads(route.calls.last.request.content)
    assert "temperature" not in body and "max_tokens" not in body
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_extra_body_reaches_the_backend_and_wins() -> None:

    route = respx.post(URL).mock(return_value=httpx.Response(200, json=completion("ok")))
    client = make_client(temperature=0.1, extra_body={"temperature": 0.9, "top_k": 40})
    try:
        await client.complete(MESSAGES)
    finally:
        await client.aclose()

    body = json.loads(route.calls.last.request.content)
    assert body["temperature"] == 0.9
    assert body["top_k"] == 40


@respx.mock
async def test_model_can_be_overridden_per_call() -> None:

    route = respx.post(URL).mock(return_value=httpx.Response(200, json=completion("ok")))
    client = make_client()
    try:
        await client.complete(MESSAGES, model="other-model")
    finally:
        await client.aclose()
    assert json.loads(route.calls.last.request.content)["model"] == "other-model"


@respx.mock
async def test_accepts_content_parts() -> None:
    payload = completion([{"type": "text", "text": "part one "}, {"type": "text", "text": "two"}])
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))
    client = make_client()
    try:
        assert await client.complete(MESSAGES) == "part one two"
    finally:
        await client.aclose()


@respx.mock
async def test_falls_back_to_reasoning_content() -> None:
    payload = completion("", reasoning_content="thought out loud")
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))
    client = make_client()
    try:
        assert await client.complete(MESSAGES) == "thought out loud"
    finally:
        await client.aclose()


@respx.mock
async def test_http_error_becomes_llm_error() -> None:
    respx.post(URL).mock(return_value=httpx.Response(500, text="boom"))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="HTTP 500"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_transport_failure_becomes_llm_error() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="could not reach"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_timeout_becomes_llm_error() -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="did not answer"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"index": 0}]},
        {"error": {"message": "nope"}},
        {},
    ],
)
async def test_unusable_responses_become_llm_errors(payload: dict) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))
    client = make_client()
    try:
        with pytest.raises(LLMError):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_empty_content_is_an_error() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=completion("   ")))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="empty message"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


async def test_no_model_configured() -> None:
    client = LLMClient(LLMConfig(base_url="https://llm.example.org/v1"))
    try:
        with pytest.raises(LLMError, match="no model"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_a_backend_error_is_logged_with_its_url(caplog) -> None:
    respx.post(URL).mock(return_value=httpx.Response(502, text="upstream is down"))
    client = make_client()
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(LLMError):
                await client.complete(MESSAGES)
    finally:
        await client.aclose()
    assert URL in caplog.text
    assert "returned HTTP 502" in caplog.text
    assert "upstream is down" in caplog.text


@respx.mock
async def test_an_unreachable_backend_logs_the_lost_connection(caplog) -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    client = make_client()
    try:
        with caplog.at_level(logging.ERROR):
            with pytest.raises(LLMError):
                await client.complete(MESSAGES)
    finally:
        await client.aclose()
    assert "lost connection to the model backend at" in caplog.text


@respx.mock
async def test_a_timeout_names_the_limit(caplog) -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    client = make_client(timeout=7.0)
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(LLMError):
                await client.complete(MESSAGES)
    finally:
        await client.aclose()
    assert "did not answer within 7.0s" in caplog.text


@respx.mock
async def test_a_successful_completion_logs_the_model_and_duration(caplog) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=completion("hello")))
    client = make_client()
    try:
        with caplog.at_level(logging.INFO):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()
    assert "some-model answered in" in caplog.text
    assert "5 chars" in caplog.text


# --------------------------------------------------------------------------- #
# Tool calls nobody executed
#
# A backend that offers tools but does not run them hands the call straight back.
# There is no answer in that response, and three different things in it look
# enough like one to get posted by accident.
# --------------------------------------------------------------------------- #


def tool_call(name: str = "search_web", **extra) -> dict:
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "type": "function",
                "id": "abc",
                "function": {"name": name, "arguments": '{"query":"gold"}'},
            }
        ],
    }
    message.update(extra)
    return {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}]}


@respx.mock
async def test_an_unexecuted_tool_call_names_the_tool() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=tool_call()))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="called search_web and nothing executed it"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_the_reasoning_behind_a_tool_call_is_never_posted() -> None:
    # This is a list of the tools the model considered. It is not an answer, and
    # it is a page long.
    payload = tool_call(reasoning_content="1. Scan available tools: ... (Not suitable)")
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="nothing executed it"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_tool_markup_in_the_text_is_refused() -> None:
    # Some backends leave the model's own guess at a tool-call format in the
    # content. Posting it would also store it in history and teach the model to
    # keep doing it.
    payload = completion('<|tool_call>call: search_web{query: "gold"}<tool_call|>')
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))
    client = make_client()
    try:
        with pytest.raises(LLMError, match="wrote a tool call as text"):
            await client.complete(MESSAGES)
    finally:
        await client.aclose()


@respx.mock
async def test_reasoning_still_stands_in_for_an_ordinary_empty_answer() -> None:
    payload = completion("", reasoning_content="I think the answer is 4")
    respx.post(URL).mock(return_value=httpx.Response(200, json=payload))
    client = make_client()
    try:
        assert await client.complete(MESSAGES) == "I think the answer is 4"
    finally:
        await client.aclose()
