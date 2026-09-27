"""Configuration, read from the environment.

Every setting is a ``SABLE_``-prefixed environment variable so the bot can run
from a systemd unit, a container, or a ``.env`` file without code changes.
See ``docs/configuration.md`` for the full reference and ``.env.example`` for a
copy-ready template.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


class ConfigError(ValueError):
    """Raised when the environment is missing or contradicts itself."""


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    if not raw:
        return default
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float | None) -> float | None:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _csv(name: str) -> list[str]:
    return [part.strip() for part in _str(name).split(",") if part.strip()]


def _mapping(name: str) -> dict[str, str]:
    """Parse ``alias=token,other=token2`` or a JSON object into a dict."""
    raw = _str(name)
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{name} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigError(f"{name} JSON must be an object")
        return {str(k): str(v) for k, v in parsed.items()}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ConfigError(f"{name} entries must look like alias=token, got {pair!r}")
        alias, token = pair.split("=", 1)
        out[alias.strip()] = token.strip()
    return out


def _json_object(name: str) -> dict[str, object]:
    raw = _str(name)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{name} must be a JSON object")
    return parsed


@dataclass(frozen=True)
class LLMConfig:
    """An OpenAI ``/chat/completions``-compatible backend.

    Provider-agnostic on purpose: point ``base_url`` at OpenAI, Ollama,
    vLLM, llama.cpp, LiteLLM, OpenRouter, or any gateway that speaks the
    same shape.
    """

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    system_prompt: str = (
        "You are sable, a helpful assistant in a Nextcloud Talk chat room. "
        "Answer concisely. You may use Markdown."
    )
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float = 120.0
    extra_body: dict[str, object] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.model)


@dataclass(frozen=True)
class Config:
    # --- Talk bot identity -------------------------------------------------
    bot_secret: str
    bot_name: str = "sable"
    nextcloud_url: str = ""
    pin_backend: bool = True

    # --- chat behaviour ----------------------------------------------------
    command_prefix: str = "!"
    ai_rooms: list[str] = field(default_factory=list)
    reply_as_reply: bool = False
    thinking_reaction: str = ""
    #: React with this to send a message to the model. Empty disables the
    #: feature, and with it the message cache that makes it possible.
    ask_reaction: str = "⁉️"
    #: Messages remembered per conversation, so a reaction can refer to one.
    message_cache: int = 200
    report_errors: bool = True
    unknown_command_hint: bool = True
    max_message_chars: int = 30000

    # --- conversation memory ----------------------------------------------
    history_turns: int = 12
    history_ttl: int = 3600

    # --- LLM ---------------------------------------------------------------
    llm: LLMConfig = field(default_factory=LLMConfig)

    # --- inbound alerting endpoint ----------------------------------------
    notify_token: str = ""
    notify_rooms: dict[str, str] = field(default_factory=dict)

    # --- process -----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    @property
    def notify_enabled(self) -> bool:
        return bool(self.notify_token)

    def ai_room_allowed(self, token: str) -> bool:
        """Should a plain (non-command, non-mention) message go to the LLM?"""
        return "*" in self.ai_rooms or token in self.ai_rooms

    @classmethod
    def from_env(cls) -> Config:
        secret = _str("SABLE_BOT_SECRET")
        if not secret:
            raise ConfigError(
                "SABLE_BOT_SECRET is required; it is the secret you passed to "
                "`occ talk:bot:install`."
            )
        if not 40 <= len(secret) <= 128:
            raise ConfigError(
                "SABLE_BOT_SECRET must be 40-128 characters, matching what Nextcloud "
                f"accepts for a bot secret (got {len(secret)})."
            )

        llm = LLMConfig(
            base_url=_str("SABLE_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=_str("SABLE_LLM_API_KEY"),
            model=_str("SABLE_LLM_MODEL"),
            system_prompt=_str("SABLE_LLM_SYSTEM_PROMPT") or LLMConfig.system_prompt,
            temperature=_float("SABLE_LLM_TEMPERATURE", None),
            max_tokens=_int("SABLE_LLM_MAX_TOKENS", 0) or None,
            timeout=_float("SABLE_LLM_TIMEOUT", 120.0) or 120.0,
            extra_body=_json_object("SABLE_LLM_EXTRA_BODY"),
        )

        config = cls(
            bot_secret=secret,
            bot_name=_str("SABLE_BOT_NAME", "sable"),
            nextcloud_url=_str("SABLE_NEXTCLOUD_URL").rstrip("/"),
            pin_backend=_bool("SABLE_PIN_BACKEND", True),
            command_prefix=_str("SABLE_COMMAND_PREFIX", "!") or "!",
            ai_rooms=_csv("SABLE_AI_ROOMS"),
            reply_as_reply=_bool("SABLE_REPLY_AS_REPLY", False),
            thinking_reaction=_str("SABLE_THINKING_REACTION"),
            ask_reaction=_str("SABLE_ASK_REACTION", "⁉️"),
            message_cache=_int("SABLE_MESSAGE_CACHE", 200),
            report_errors=_bool("SABLE_REPORT_ERRORS", True),
            unknown_command_hint=_bool("SABLE_UNKNOWN_COMMAND_HINT", True),
            max_message_chars=_int("SABLE_MAX_MESSAGE_CHARS", 30000),
            history_turns=_int("SABLE_HISTORY_TURNS", 12),
            history_ttl=_int("SABLE_HISTORY_TTL", 3600),
            llm=llm,
            notify_token=_str("SABLE_NOTIFY_TOKEN"),
            notify_rooms=_mapping("SABLE_NOTIFY_ROOMS"),
            host=_str("SABLE_HOST", "0.0.0.0"),
            port=_int("SABLE_PORT", 8080),
            log_level=_str("SABLE_LOG_LEVEL", "INFO").upper(),
        )

        if config.notify_enabled and not config.nextcloud_url:
            raise ConfigError(
                "SABLE_NEXTCLOUD_URL is required when SABLE_NOTIFY_TOKEN is set: "
                "outbound-only messages have no incoming request to learn the "
                "server URL from."
            )
        if config.pin_backend and not config.nextcloud_url:
            # Nothing to pin against; fall back to trusting the signed header.
            object.__setattr__(config, "pin_backend", False)
        return config
