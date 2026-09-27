"""A deliberately thin OpenAI ``/chat/completions`` client.

Model-agnostic: anything that speaks the chat-completions shape works -
OpenAI, Azure OpenAI (via a gateway), Ollama, vLLM, llama.cpp, LiteLLM,
OpenRouter, Together, Groq. Point ``SABLE_LLM_BASE_URL`` at it and set
``SABLE_LLM_MODEL``; provider-specific knobs go in ``SABLE_LLM_EXTRA_BODY``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import LLMConfig
from .state import ConnectionState

log = logging.getLogger(__name__)

Message = dict[str, str]


class LLMError(RuntimeError):
    """The completion request failed or came back unusable."""


class LLMClient:
    def __init__(
        self, config: LLMConfig, *, client: httpx.AsyncClient | None = None
    ) -> None:
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
            log.warning(
                "%s did not answer within %ss", url, self.config.timeout
            )
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
            raise LLMError(
                f"{url} returned HTTP {response.status_code}: {response.text[:400]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            log.warning("%s answered with something that is not JSON", url)
            raise LLMError("response was not JSON") from exc

        text = _extract_text(payload)
        log.info(
            "%s answered in %.1fs (%d chars)", used_model or "the model", elapsed, len(text)
        )
        return text


def _extract_text(payload: Any) -> str:
    """Pull the assistant text out of a chat-completions response."""
    if not isinstance(payload, dict):
        raise LLMError("response was not a JSON object")
    if "error" in payload and payload["error"]:
        raise LLMError(f"backend error: {str(payload['error'])[:400]}")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("response contained no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise LLMError("first choice had no message")

    content = message.get("content")
    if isinstance(content, list):
        # Some gateways return content parts instead of a plain string.
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    text = (content or "").strip()
    if not text:
        # Reasoning models sometimes spend the whole budget before answering.
        reasoning = str(message.get("reasoning_content") or "").strip()
        if reasoning:
            return reasoning
        finish = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
        raise LLMError(f"the model returned an empty message (finish_reason={finish})")
    return text
