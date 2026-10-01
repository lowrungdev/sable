"""A deliberately thin OpenAI ``/chat/completions`` client.

Model-agnostic: anything that speaks the chat-completions shape works -
OpenAI, Azure OpenAI (via a gateway), Ollama, vLLM, llama.cpp, LiteLLM,
OpenRouter, Together, Groq. Point ``SABLE_LLM_BASE_URL`` at it and set
``SABLE_LLM_MODEL``; provider-specific knobs go in ``SABLE_LLM_EXTRA_BODY``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from .config import LLMConfig
from .state import ConnectionState

log = logging.getLogger(__name__)

Message = dict[str, str]

#: A model announcing a tool call as plain text rather than in ``tool_calls``.
#: Some backends leave this in the content when the model guesses at a format
#: the server's parser does not recognise; it is never something to post.
TOOL_MARKUP = re.compile(r"<\|?/?tool_call", re.IGNORECASE)


class LLMError(RuntimeError):
    """The completion request failed or came back unusable."""


class LLMClient:
    def __init__(self, config: LLMConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout)
        self._state = ConnectionState(f"the model backend at {config.base_url}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _body(self, messages: list[Message], model: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": messages,
        }
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            body["max_tokens"] = self.config.max_tokens
        # Escape hatch for provider-specific fields; it wins over our defaults.
        body.update(self.config.extra_body)
        return body

    async def complete(self, messages: list[Message], *, model: str | None = None) -> str:
        """Run one non-streaming completion and return the assistant text."""
        if not (model or self.config.model):
            raise LLMError("no model configured (set SABLE_LLM_MODEL)")

        url = f"{self.config.base_url}/chat/completions"
        used_model = model or self.config.model
        started = time.monotonic()
        log.debug("asking %s at %s (%d messages)", used_model, url, len(messages))
        try:
            response = await self._client.post(
                url,
                json=self._body(messages, model),
                headers=self._headers(),
                timeout=self.config.timeout,
            )
        except httpx.TimeoutException as exc:
            self._state.record_failure(exc)
            log.warning("%s did not answer within %ss", url, self.config.timeout)
            raise LLMError(f"the model did not answer within {self.config.timeout}s") from exc
        except httpx.HTTPError as exc:
            self._state.record_failure(exc)
            raise LLMError(f"could not reach {url}: {exc}") from exc

        self._state.record_success()
        elapsed = time.monotonic() - started

        if response.status_code >= 400:
            log.warning(
                "%s returned HTTP %s after %.1fs: %s",
                url,
                response.status_code,
                elapsed,
                response.text[:200],
            )
            raise LLMError(f"{url} returned HTTP {response.status_code}: {response.text[:400]}")

        try:
            payload = response.json()
        except ValueError as exc:
            log.warning("%s answered with something that is not JSON", url)
            raise LLMError("response was not JSON") from exc

        text = _extract_text(payload)
        log.info("%s answered in %.1fs (%d chars)", used_model or "the model", elapsed, len(text))
        return text


def _extract_text(payload: Any) -> str:
    """Pull the assistant text out of a chat-completions response."""
    if not isinstance(payload, dict):
        raise LLMError("response was not a JSON object")
    if payload.get("error"):
        raise LLMError(f"backend error: {str(payload['error'])[:400]}")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("response contained no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise LLMError("first choice had no message")

    finish = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None

    content = message.get("content")
    if isinstance(content, list):
        # Some gateways return content parts instead of a plain string.
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    text = (content or "").strip()

    calls = _tool_calls(message)
    if calls and not text:
        # The model asked for a tool and nobody ran it, so there is no answer to
        # post. Say which tool: the fix is almost always at the backend, and the
        # name is what tells you where to look.
        raise LLMError(
            f"the model called {', '.join(calls)} and nothing executed it (finish_reason={finish})"
        )

    if not text:
        # Reasoning models sometimes spend the whole budget before answering, and
        # their thinking is better than nothing. Thinking *about which tool to
        # call* is not - it is a list of tools the model considered, which
        # answers no question and reads as nonsense in a chat room.
        reasoning = str(message.get("reasoning_content") or "").strip()
        if reasoning and TOOL_MARKUP.search(reasoning):
            raise LLMError("the model tried to call a tool and nothing executed it")
        if reasoning:
            return reasoning
        raise LLMError(f"the model returned an empty message (finish_reason={finish})")

    if TOOL_MARKUP.search(text):
        # Not an answer, and posting it teaches the model to keep doing it, since
        # replies go back into the conversation history.
        raise LLMError(
            "the model wrote a tool call as text instead of calling one; nothing executed it"
        )
    return text


def _tool_calls(message: dict[str, Any]) -> list[str]:
    """The names of any tools the model asked for, in order."""
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    names: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        names.append(str(name or call.get("name") or "an unnamed tool"))
    return names
