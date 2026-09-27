from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx
import pytest

from sable.bot import Bot
from sable.config import Config, LLMConfig
from sable.events import parse_event
from sable.llm import Message
from sable.signing import digest

SECRET = "s" * 40
BACKEND = "https://cloud.example.org"
ROOM = "abcd1234"


@pytest.fixture
def config() -> Config:
    return make_config()


def make_config(**overrides: Any) -> Config:
    defaults: dict[str, Any] = {
        "bot_secret": SECRET,
        "bot_name": "sable",
        "nextcloud_url": BACKEND,
        "pin_backend": True,
        "llm": LLMConfig(model="some-model", api_key="sk-test"),
    }
    defaults.update(overrides)
    return Config(**defaults)


class FakeLLM:
    """Stands in for :class:`sable.llm.LLMClient`."""

    def __init__(self, reply: str = "mock answer", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message], *, model: str | None = None) -> str:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.reply

    async def aclose(self) -> None:
        return None

    @property
    def last_prompt(self) -> str:
        return self.calls[-1][-1]["content"]


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        yield client


@pytest.fixture
def bot(config: Config, llm: FakeLLM, http_client: httpx.AsyncClient) -> Bot:
    return Bot(config, http_client=http_client, llm=llm)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #


def message_payload(
    text: str = "hello",
    *,
    message_id: int = 100,
    room: str = ROOM,
    room_name: str = "Team chat",
    actor_id: str = "users/alice",
    actor_name: str = "Alice",
    actor_type: str = "Person",
    parameters: dict | None = None,
    in_reply_to: int = 0,
) -> dict:
    content: dict[str, Any] = {"message": text, "parameters": parameters or {}}
    obj: dict[str, Any] = {
        "type": "Note",
        "id": str(message_id),
        "name": "message",
        "content": json.dumps(content),
        "mediaType": "text/markdown",
    }
    if in_reply_to:
        obj["inReplyTo"] = {"type": "Note", "id": str(in_reply_to), "name": "message"}
    return {
        "type": "Create",
        "actor": {
            "type": actor_type,
            "id": actor_id,
            "name": actor_name,
            "talkParticipantType": "OWNER",
        },
        "object": obj,
        "target": {"type": "Collection", "id": room, "name": room_name},
    }


def event(text: str = "hello", **kwargs: Any):
    return parse_event(message_payload(text, **kwargs), backend=BACKEND)


def signed_headers(body: bytes, secret: str = SECRET, backend: str = BACKEND) -> dict[str, str]:
    random = "r" * 64
    return {
        "X-Nextcloud-Talk-Random": random,
        "X-Nextcloud-Talk-Signature": digest(random, body, secret),
        "X-Nextcloud-Talk-Backend": backend,
        "Content-Type": "application/json",
    }
