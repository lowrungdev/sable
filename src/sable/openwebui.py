"""Open WebUI's server-side tool calling, which is not chat completions.

``LLMClient`` sends one request and reads the answer out of the response. That
works everywhere and executes nothing: a model offered tools hands the call
back, and nobody runs it.

Open WebUI will run them, but only on one path, and that path is not
``/chat/completions`` as an OpenAI client understands it. The loop that executes
a tool, feeds the result back and asks again lives in the code that streams
events into a *chat*, so it only runs when the request names a chat and an
assistant message inside it, and only with ``stream: true``. The answer is then
written into that chat rather than returned. Four calls:

1. create a conversation holding the question and an empty assistant message;
2. start the completion, which returns task ids, not an answer;
3. wait for the tasks to drain;
4. read the assistant message, and delete the conversation.

The conversation exists only because the loop needs somewhere to write. sable
keeps its own history and sends it in ``messages`` as before, so each question
creates and destroys one.

Everything here is Open WebUI's own API, not a standard. It is deliberately a
separate client rather than a mode of ``LLMClient``, which stays portable.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from .config import BUILTIN_FEATURES, LLMConfig
from .llm import LLMError, Message
from .state import ConnectionState


def _segment(value: str) -> str:
    """One URL path segment, safe to splice into a path.

    The chat id comes back from the server and goes straight into later paths;
    an id holding ``/``, ``..``, ``?`` or ``#`` would otherwise reach a different
    endpoint than the one meant. Nothing is left unquoted, ``/`` included.
    """
    return quote(str(value), safe="")

log = logging.getLogger(__name__)


class OpenWebUIClient:
    """Runs one question through Open WebUI's agentic loop and returns the text.

    Interchangeable with ``LLMClient``: same ``complete`` signature, same
    ``LLMError``, so ``Bot`` does not know which one it holds.
    """

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

    # --- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        """One request against Open WebUI, with its failures named usefully."""
        url = f"{self.config.base_url}{path}"
        try:
            response = await self._client.request(
                method, url, headers=self._headers(), **kwargs
            )
        except httpx.TimeoutException as exc:
            self._state.record_failure(exc)
            raise LLMError(f"{url} did not answer within {self.config.timeout}s") from exc
        except httpx.HTTPError as exc:
            self._state.record_failure(exc)
            raise LLMError(f"could not reach {url}: {exc}") from exc

        self._state.record_success()
        if response.status_code >= 400:
            log.warning(
                "%s returned HTTP %s: %s", url, response.status_code, response.text[:200]
            )
            raise LLMError(
                f"{url} returned HTTP {response.status_code}: {response.text[:400]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # The blocking variant answers with a bare null, and a delete with
            # nothing at all. Neither is a failure.
            return None

    # --- the four calls ----------------------------------------------------

    async def _create_chat(self, model: str, prompt: str) -> tuple[str, str]:
        """A conversation holding the question and somewhere to write the answer."""
        user_id, assistant_id = str(uuid.uuid4()), str(uuid.uuid4())
        stamp = int(time.time())
        payload = {
            "chat": {
                "title": "sable",
                "models": [model],
                "history": {
                    "currentId": assistant_id,
                    "messages": {
                        user_id: {
                            "id": user_id,
                            "role": "user",
                            "content": prompt,
                            "timestamp": stamp,
                            "models": [model],
                            "childrenIds": [assistant_id],
                        },
                        assistant_id: {
                            "id": assistant_id,
                            "role": "assistant",
                            "content": "",
                            "parentId": user_id,
                            "childrenIds": [],
                            "model": model,
                            "modelName": model,
                            "modelIdx": 0,
                            "done": False,
                            "timestamp": stamp + 1,
                        },
                    },
                },
            }
        }
        created = await self._call("POST", "/v1/chats/new", json=payload)
        chat_id = (created or {}).get("id")
        if not chat_id or not str(chat_id).strip():
            raise LLMError("Open WebUI created no conversation to answer in")
        return str(chat_id), assistant_id

    def _async_session(self, tools: bool) -> bool:
        """Does this request carry a session id (built-in tools, polled)?

        ``tools`` is the per-conversation gate (SABLE_LLM_TOOL_ROOMS). A session
        also brings Open WebUI's knowledge, files, notes, channels and calendar
        tools, which need no flag, so a conversation that may not have tools
        gets the blocking variant: no session, no polling, no built-ins.
        """
        return tools and self.config.builtin_tools

    def _completion_body(
        self,
        messages: list[Message],
        model: str,
        chat_id: str,
        assistant_id: str,
        tools: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            # All three are required together. Without them no tool runs, and
            # the reply is a tool call for somebody else to execute.
            "stream": True,
            "chat_id": chat_id,
            "id": assistant_id,
            "background_tasks": {
                "title_generation": False,
                "tags_generation": False,
                "follow_up_generation": False,
            },
        }
        if self._async_session(tools):
            # Any non-empty value. This is what puts web search and the rest in
            # front of the model - and what makes the request asynchronous.
            body["session_id"] = f"sable-{uuid.uuid4()}"
            body["features"] = {
                name: name in self.config.features for name in sorted(BUILTIN_FEATURES)
            }
        if tools and self.config.tool_ids:
            body["tool_ids"] = list(self.config.tool_ids)
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            body["max_tokens"] = self.config.max_tokens
        body.update(self.config.extra_body)
        return body

    async def _drain(self, chat_id: str, deadline: float) -> None:
        """Wait for the loop to finish, or say how long we waited."""
        while True:
            tasks = await self._call("GET", f"/tasks/chat/{_segment(chat_id)}") or {}
            if not tasks.get("task_ids"):
                return
            if time.monotonic() >= deadline:
                raise LLMError(
                    f"the model was still working after {self.config.timeout}s "
                    f"(SABLE_LLM_TIMEOUT)"
                )
            await asyncio.sleep(min(self.config.poll_interval, max(0.0, deadline - time.monotonic())))

    async def _read_answer(self, chat_id: str, assistant_id: str) -> str:
        record = await self._call("GET", f"/v1/chats/{_segment(chat_id)}") or {}
        chat = record.get("chat") if isinstance(record, dict) else None
        messages = ((chat or {}).get("history") or {}).get("messages") or {}
        message = messages.get(assistant_id) or {}
        text = str(message.get("content") or "").strip()
        if not text:
            raise LLMError(
                "the loop finished without writing an answer; check the model's "
                "Stream Chat Response setting, which overrides the request"
            )
        if self.config.show_sources:
            text += _citations(message.get("sources"))
        return text

    # --- the whole thing ---------------------------------------------------

    async def complete(
        self, messages: list[Message], *, model: str | None = None, tools: bool = False
    ) -> str:
        """``tools`` says whether this question may be offered tool_ids and
        features. Off unless the caller (the bot, for a room in
        SABLE_LLM_TOOL_ROOMS) turns it on."""
        used_model = model or self.config.model
        if not used_model:
            raise LLMError("no model configured (set SABLE_LLM_MODEL)")

        prompt = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        started = time.monotonic()
        deadline = started + self.config.timeout

        chat_id, assistant_id = await self._create_chat(used_model, prompt)
        try:
            body = self._completion_body(messages, used_model, chat_id, assistant_id, tools)
            log.debug(
                "asking %s through Open WebUI (chat %s, %d messages, tools: %s)",
                used_model,
                chat_id,
                len(messages),
                ", ".join(self.config.tool_ids) if tools and self.config.tool_ids else "none",
            )
            await self._call("POST", "/chat/completions", json=body)
            # Without a session id the call above already blocked until the loop
            # finished, and there is nothing left to poll for.
            if self._async_session(tools):
                await self._drain(chat_id, deadline)
            answer = await self._read_answer(chat_id, assistant_id)
        finally:
            if not self.config.keep_chats:
                await self._discard(chat_id)

        log.info(
            "%s answered in %.1fs (%d chars)",
            used_model,
            time.monotonic() - started,
            len(answer),
        )
        return answer

    async def _discard(self, chat_id: str) -> None:
        """Delete the conversation. Never fatal: the answer is already in hand."""
        try:
            await self._call("DELETE", f"/v1/chats/{_segment(chat_id)}")
        except LLMError as exc:
            log.warning("could not delete the conversation %s: %s", chat_id, exc)


def _citations(sources: Any) -> str:
    """The tools and pages an answer came from, as a trailing line."""
    if not isinstance(sources, list):
        return ""
    seen: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        origin = source.get("source") if isinstance(source.get("source"), dict) else {}
        label = str(origin.get("name") or origin.get("id") or "").strip()
        for meta in source.get("metadata") or []:
            if isinstance(meta, dict) and meta.get("source"):
                label = str(meta["source"])
                break
        if label and label not in seen:
            seen.append(label)
    return "\n\n_sources: " + ", ".join(seen) + "_" if seen else ""
