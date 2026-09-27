"""Configuration, read from the environment.

Every setting is a ``SABLE_``-prefixed environment variable so the bot can run
from a systemd unit, a container, or a ``.env`` file without code changes.
See ``docs/configuration.md`` for the full reference and ``.env.example`` for a
copy-ready template.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

#: A Talk conversation token, as it appears at the end of the conversation's
#: URL. Talk's own routes only match lowercase, so anything else can never
#: reach a conversation - and in practice means someone pasted the room's
#: *name* where its token belongs.
TOKEN_RE = re.compile(r"^[a-z0-9]{4,64}$")

#: Said whenever a token turns out not to be one.
TOKEN_HINT = (
    "a conversation token is the lowercase string at the end of the "
    "conversation's URL (.../call/abcd1234), not the name of the room"
)


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


def _prefixed(prefix: str) -> dict[str, str]:
    """Collect ``PREFIX_NAME=value`` into ``{"name": "value"}``.

    One variable per entry rather than one variable holding all of them, so a
    secret store can inject each token separately.
    """
    found: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(prefix) and len(key) > len(prefix) and value.strip():
            found[key[len(prefix) :].strip().lower()] = value.strip()
    return found


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
    startup_check: bool = True
    unknown_command_hint: bool = True
    max_message_chars: int = 30000

    # --- conversation memory ----------------------------------------------
    history_turns: int = 12
    history_ttl: int = 3600

    # --- LLM ---------------------------------------------------------------
    llm: LLMConfig = field(default_factory=LLMConfig)

    # --- people to ignore --------------------------------------------------
    #: Events from these users are dropped entirely. Entries match the bare user
    #: id, the full actor id, or the display name.
    ignore_users: list[str] = field(default_factory=list)

    # --- inbound alerting endpoint ----------------------------------------
    notify_token: str = ""
    notify_rooms: dict[str, str] = field(default_factory=dict)

    # --- generic webhook receivers ----------------------------------------
    #: Hook name to conversation, for services that cannot speak sable's own
    #: /notify shape. Each needs its own token in SABLE_HOOK_TOKEN_<NAME>.
    hooks: dict[str, str] = field(default_factory=dict)
    hook_tokens: dict[str, str] = field(default_factory=dict)
    #: Optional per-hook format string; without one the payload is rendered
    #: generically. `{dotted.path}` is substituted from the payload.
    hook_templates: dict[str, str] = field(default_factory=dict)
    #: Largest webhook body accepted on /hook. Alerts are small; this is a cap
    #: on abuse rather than a size anyone should reach.
    max_hook_bytes: int = 256 * 1024

    # --- file attachments -------------------------------------------------
    #: A Nextcloud *user* account, used only to upload and share files. The bot
    #: API cannot attach anything to a message, so this is the second, larger
    #: credential that buys attachments. Leave empty and /notify stays text-only.
    nextcloud_user: str = ""
    nextcloud_password: str = ""
    #: Folder inside that user's own Files where attachments are put.
    upload_path: str = "/sable"
    #: Largest attachment /notify will accept, in bytes.
    max_upload_bytes: int = 25 * 1024 * 1024

    # --- process -----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    @property
    def notify_enabled(self) -> bool:
        return bool(self.notify_token)

    def hook_room(self, name: str) -> str:
        """The conversation a hook posts into, or '' if there is no such hook.

        The configured value may be a ``SABLE_NOTIFY_ROOMS`` alias rather than a
        token, resolved here so both endpoints name conversations the same way.
        """
        room = self.hooks.get(name.strip().lower(), "")
        return self.notify_rooms.get(room, room)

    def hook_token(self, name: str) -> str:
        return self.hook_tokens.get(name.strip().lower(), "")

    @property
    def uploads_enabled(self) -> bool:
        """Can /notify accept a file? Needs the user account as well as the URL."""
        return bool(self.nextcloud_user and self.nextcloud_password and self.nextcloud_url)

    def is_ignored(self, actor_id: str, name: str = "") -> bool:
        """Should everything from this actor be dropped?

        An entry matches the bare user id (``alice``), the full actor id
        (``users/alice``, which is what the log prints), or the display name,
        ignoring case.

        Prefer ids: a display name can be changed by the person themselves, and
        for an ignore list that means they quietly stop being ignored.
        """
        if not self.ignore_users:
            return False
        bare = actor_id.split("/", 1)[1] if "/" in actor_id else actor_id
        candidates = {
            value.casefold()
            for value in (actor_id, bare, name.strip())
            if value
        }
        return any(entry.strip().casefold() in candidates for entry in self.ignore_users)

    def ai_room_allowed(self, token: str, name: str = "") -> bool:
        """Should a plain (non-command, non-mention) message go to the LLM?

        An entry matches either the conversation token - ``abcd1234``, the last
        segment of the conversation's URL - or its display name, ignoring case
        and surrounding space.

        Prefer tokens where it matters: a token is permanent, while any moderator
        can rename a conversation, which would silently change whether the bot
        answers everything in it.
        """
        if "*" in self.ai_rooms:
            return True
        if token and token in self.ai_rooms:
            return True
        wanted = name.strip().casefold()
        return bool(wanted) and any(
            entry.strip().casefold() == wanted for entry in self.ai_rooms
        )

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
            startup_check=_bool("SABLE_STARTUP_CHECK", True),
            unknown_command_hint=_bool("SABLE_UNKNOWN_COMMAND_HINT", True),
            max_message_chars=_int("SABLE_MAX_MESSAGE_CHARS", 30000),
            history_turns=_int("SABLE_HISTORY_TURNS", 12),
            history_ttl=_int("SABLE_HISTORY_TTL", 3600),
            llm=llm,
            ignore_users=_csv("SABLE_IGNORE_USERS"),
            notify_token=_str("SABLE_NOTIFY_TOKEN"),
            notify_rooms=_mapping("SABLE_NOTIFY_ROOMS"),
            hooks={
                name.lower(): room for name, room in _mapping("SABLE_HOOKS").items()
            },
            hook_tokens=_prefixed("SABLE_HOOK_TOKEN_"),
            hook_templates=_prefixed("SABLE_HOOK_TEMPLATE_"),
            max_hook_bytes=_int("SABLE_MAX_HOOK_BYTES", 256 * 1024),
            nextcloud_user=_str("SABLE_NEXTCLOUD_USER"),
            nextcloud_password=_str("SABLE_NEXTCLOUD_PASSWORD"),
            upload_path="/" + _str("SABLE_UPLOAD_PATH", "/sable").strip("/"),
            max_upload_bytes=_int("SABLE_MAX_UPLOAD_BYTES", 25 * 1024 * 1024),
            host=_str("SABLE_HOST", "0.0.0.0"),
            port=_int("SABLE_PORT", 8080),
            log_level=_str("SABLE_LOG_LEVEL", "INFO").upper(),
        )

        missing = sorted(set(config.hooks) - set(config.hook_tokens))
        if missing:
            raise ConfigError(
                "every hook needs its own token: "
                + ", ".join(f"SABLE_HOOK_TOKEN_{name.upper()}" for name in missing)
                + " not set"
            )
        stray = sorted(set(config.hook_templates) - set(config.hooks))
        if stray:
            raise ConfigError(
                "SABLE_HOOK_TEMPLATE_"
                + ", SABLE_HOOK_TEMPLATE_".join(name.upper() for name in stray)
                + " has no matching entry in SABLE_HOOKS"
            )
        unused = sorted(set(config.hook_tokens) - set(config.hooks))
        if unused:
            raise ConfigError(
                "SABLE_HOOK_TOKEN_"
                + ", SABLE_HOOK_TOKEN_".join(name.upper() for name in unused)
                + " has no matching entry in SABLE_HOOKS"
            )
        for hook, room in sorted(config.hooks.items()):
            if not TOKEN_RE.match(config.hook_room(hook)):
                raise ConfigError(
                    f"SABLE_HOOKS entry {hook}={room!r} is neither a conversation "
                    f"token nor a SABLE_NOTIFY_ROOMS alias: {TOKEN_HINT}."
                )
        for alias, room in sorted(config.notify_rooms.items()):
            if not TOKEN_RE.match(room):
                raise ConfigError(
                    f"SABLE_NOTIFY_ROOMS entry {alias}={room!r} is not a "
                    f"conversation token: {TOKEN_HINT}."
                )
        if config.hooks and not config.nextcloud_url:
            raise ConfigError(
                "SABLE_NEXTCLOUD_URL is required when SABLE_HOOKS is set: a webhook "
                "from another service carries no Nextcloud address to reply to."
            )
        if config.max_hook_bytes <= 0:
            raise ConfigError("SABLE_MAX_HOOK_BYTES must be greater than zero")
        if bool(config.nextcloud_user) != bool(config.nextcloud_password):
            raise ConfigError(
                "SABLE_NEXTCLOUD_USER and SABLE_NEXTCLOUD_PASSWORD go together: "
                "set both to enable file attachments, or neither to keep /notify "
                "text-only."
            )
        if config.nextcloud_user and not config.nextcloud_url:
            raise ConfigError(
                "SABLE_NEXTCLOUD_URL is required for file attachments: there is no "
                "incoming request to learn the server address from when uploading."
            )
        if config.max_upload_bytes <= 0:
            raise ConfigError("SABLE_MAX_UPLOAD_BYTES must be greater than zero")
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
