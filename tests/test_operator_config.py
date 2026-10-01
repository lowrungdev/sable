"""A configuration shaped like a real deployment, end to end.

Built from the environment the way the operator's .env reads, so the seams
between settings (rooms, model users, tool rooms, admin commands) are exercised
together rather than one at a time.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
import pytest
import respx
from conftest import TALK, FakeLLM, event, message_payload, reaction_event

from sable.app import create_app
from sable.bot import NOT_ALLOWED, Bot
from sable.config import Config, ConfigError

AI = "mr2i54ur"
OTHER = "wxyz9876"
TOKENS = ["abcd1234", "bcde2345", "cdef3456", "defg4567", "efgh5678"]

ENV = {
    "SABLE_NEXTCLOUD_URL": "https://cloud.example.org",
    "SABLE_NEXTCLOUD_USER": "sable",
    "SABLE_NEXTCLOUD_PASSWORD": "app-password-1234",
    "SABLE_STARTUP_CHECK": "false",
    "SABLE_AI_ROOMS": AI,
    # Removed settings: a left-over display-name list must not break startup.
    "SABLE_ASK_ROOMS": "Team chat,Ops room",
    "SABLE_MESSAGE_CACHE": "200",
    "SABLE_LLM_BACKEND": "openwebui",
    "SABLE_LLM_BASE_URL": "https://ai.example.org/api",
    "SABLE_LLM_API_KEY": "sk-test",
    "SABLE_LLM_MODEL": "gemma",
    "SABLE_LLM_TOOL_IDS": "server:mcp:1",
    "SABLE_LLM_FEATURES": "web_search",
    "SABLE_ADMIN_COMMANDS": "reset,ai",
    "SABLE_ADMIN_USERS": "maser,korren",
    "SABLE_NOTIFY_TOKEN": "alert-token",
    "SABLE_NOTIFY_ROOMS": ",".join(f"a{i}={t}" for i, t in enumerate(TOKENS)),
    "SABLE_HOOKS": "komodo=a0",
    "SABLE_HOOK_TOKEN_KOMODO": "hook-token",
}


@pytest.fixture
def operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


def say(route) -> list[str]:
    return [json.loads(call.request.content)["message"] for call in route.calls]


def test_the_operators_environment_loads_and_says_what_is_off(operator_env) -> None:
    config = Config.from_env()
    assert config.ai_rooms == [AI]
    assert not config.llm.tools_in(AI), "tools are configured but no room may use them"
    assert any("no conversation may use them" in w for w in config.warnings)
    assert any("SABLE_ALLOWED_ROOMS is empty" in w for w in config.warnings)


@pytest.mark.parametrize("key", ["tool_ids", "features"])
def test_extra_body_carrying_tools_is_a_clear_startup_error(
    operator_env, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("SABLE_LLM_EXTRA_BODY", json.dumps({key: ["x"] if key == "tool_ids" else {}}))
    with pytest.raises(ConfigError) as caught:
        Config.from_env()
    text = str(caught.value)
    assert key in text and "SABLE_LLM_TOOL_ROOMS" in text


@respx.mock
async def test_startup_and_a_few_messages_behave_as_configured(operator_env, caplog) -> None:
    config = Config.from_env()
    llm = FakeLLM()
    bot = Bot(config, llm=llm)  # type: ignore[arg-type]
    ai_route = respx.post(f"{TALK}/chat/{AI}").mock(
        return_value=httpx.Response(201, json={"ocs": {"data": {"id": 1}}})
    )
    other_route = respx.post(f"{TALK}/chat/{OTHER}").mock(
        return_value=httpx.Response(201, json={"ocs": {"data": {"id": 2}}})
    )
    app = create_app(config, bot=bot)
    try:
        with caplog.at_level(logging.INFO):
            async with app.router.lifespan_context(app):
                pass
        assert "tools:" in caplog.text and "none (off everywhere)" in caplog.text

        # A plain message in the AI room is answered, with tools off.
        await bot.handle(event("what is 2+2?", room=AI, message_id=10))
        assert llm.tools == [False]
        assert len(say(ai_route)) == 1

        # `!reset` and `!ai` are for the administrators.
        await bot.handle(event("!reset", room=AI, message_id=11))
        await bot.handle(event("!ai hi", room=AI, message_id=12))
        assert say(ai_route)[1:] == ["`!reset` is for administrators only.",
                                     "`!ai` is for administrators only."]
        assert llm.tools == [False]

        # An administrator may; ids are matched without regard to case.
        await bot.handle(event("!ai hi", room=AI, message_id=13, actor_id="users/Maser", actor_name="m"))
        assert llm.tools == [False, False]

        # Mentioning the bot still reaches the model for everyone, anywhere.
        await bot.handle(event("@sable hello", room=OTHER, message_id=14))
        assert llm.tools == [False, False, False]
        assert len(say(other_route)) == 1

        # Chatter in a room that is not an AI room is not answered.
        await bot.handle(event("just talking", room=OTHER, message_id=15))
        assert len(say(other_route)) == 1
    finally:
        await bot.aclose()


@respx.mock
async def test_a_model_user_list_leaves_a_bystander_in_an_ai_room_unanswered(
    operator_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SABLE_LLM_USERS", "alice")
    config = Config.from_env()
    llm = FakeLLM()
    bot = Bot(config, llm=llm)  # type: ignore[arg-type]
    route = respx.post(f"{TALK}/chat/{AI}").mock(
        return_value=httpx.Response(201, json={"ocs": {"data": {"id": 1}}})
    )
    try:
        await bot.handle(event("hello", room=AI, message_id=1, actor_id="users/bob", actor_name="Bob"))
        assert not route.called and not llm.calls
        await bot.handle(event("@sable hello", room=AI, message_id=2, actor_id="users/bob", actor_name="Bob"))
        assert say(route) == [NOT_ALLOWED]
        await bot.handle(event("hello", room=AI, message_id=3))
        assert llm.tools == [False]
    finally:
        await bot.aclose()
