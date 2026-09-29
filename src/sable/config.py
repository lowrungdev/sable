"""Configuration, read from the environment.

Every setting is a ``SABLE_``-prefixed environment variable so the bot can run
from a systemd unit, a container, or a ``.env`` file without code changes.
See ``docs/configuration.md`` for the full reference and ``.env.example`` for a
copy-ready template.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: A Talk conversation token, as it appears at the end of the conversation's
#: URL. Talk's own routes only match lowercase, so anything else can never
#: reach a conversation - and in practice means someone pasted the room's
#: *name* where its token belongs.
#:
#: ``\Z`` and not ``$``: ``$`` also matches immediately before a final newline,
#: so ``abcd1234\n`` passed a check that is the whole boundary between a value
#: somebody supplied and a URL built around it. A token with a trailing newline
#: got as far as httpx, which refuses it as an invalid URL - an unhandled error,
#: so the /notify caller was told 500 where the same value uppercased got 400.
TOKEN_RE = re.compile(r"^[a-z0-9]{4,64}\Z")

#: How sable talks to a model backend.
LLM_BACKENDS = frozenset({"openai", "openwebui"})

#: Open WebUI's togglable built-in tools. The others it offers - knowledge,
#: files, notes, channels, calendar - need no flag and come with the session.
BUILTIN_FEATURES = frozenset(
    {"web_search", "code_interpreter", "image_generation", "memory"}
)

#: Said whenever a token turns out not to be one.
TOKEN_HINT = (
    "a conversation token is the lowercase string at the end of the "
    "conversation's URL (.../call/abcd1234), not the name of the room"
)


#: uvicorn's own default, and the right one for a proxy on the same host. A
#: proxy in another container arrives from the bridge network instead, so that
#: deployment has to name it - see docs/deployment.md.
DEFAULT_TRUSTED_PROXIES = ("127.0.0.1", "::1")


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


def _csv_or(name: str, default: list[str]) -> list[str]:
    """Like :func:`_csv`, but telling an unset variable from an empty one apart:
    setting it to nothing is a choice (trust nobody) and must not read as absent."""
    if name not in os.environ:
        return list(default)
    return _csv(name)


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

    # --- Open WebUI ---------------------------------------------------------
    #: ``openai`` is one request and one answer, against anything that speaks
    #: chat completions. ``openwebui`` runs Open WebUI's own agentic loop, where
    #: the server executes tools and the answer arrives in a chat record rather
    #: than in the HTTP response.
    backend: str = "openai"
    #: Workspace tools and MCP servers to offer, as Open WebUI names them:
    #: ``server:mcp:1``, ``my_workspace_tool``.
    tool_ids: list[str] = field(default_factory=list)
    #: Which of Open WebUI's own built-in tools to turn on.
    features: list[str] = field(default_factory=list)
    #: Send a session id, which is what makes the built-ins available at all.
    #: Without one the request blocks until the loop finishes instead of
    #: returning a task to poll - simpler, but with no built-in tools.
    builtin_tools: bool = True
    #: Seconds between checks on a running loop.
    poll_interval: float = 2.0
    #: Keep the conversation sable creates for each question. It exists only
    #: because the loop needs somewhere to write; by default it is deleted.
    keep_chats: bool = False
    #: Append the sources the loop cited. Worth having: it is how you notice an
    #: answer came from an encyclopaedia rather than from today's market.
    show_sources: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.model)

    @property
    def agentic(self) -> bool:
        return self.backend == "openwebui"


@dataclass(frozen=True)
class Config:
    # --- Talk bot identity -------------------------------------------------
    bot_secret: str
    #: The secret being rotated away from, accepted on incoming webhooks only.
    #: Talk holds one secret per bot install, so changing it means uninstalling
    #: and reinstalling the bot, and every event that arrives in between fails
    #: its signature check. Keeping the old value here covers that window.
    bot_secret_previous: str = ""
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
    #: Restrict that reaction to the admin_users. Anybody in a conversation can
    #: otherwise forward somebody else's words to the model backend without
    #: saying anything in the room, which is accepted risk 7 in security.md.
    ask_admins_only: bool = False
    #: Conversations whose messages are cached for the reaction, matched like
    #: ai_rooms. Empty means *every* conversation, which is the asymmetry to
    #: watch: an empty ai_rooms means no rooms. Deliberate - reading empty as
    #: none would switch the feature off for every existing deployment on
    #: upgrade, silently, which is the one outcome worth ruling out.
    ask_rooms: list[str] = field(default_factory=list)
    #: Messages remembered per conversation, so a reaction can refer to one.
    message_cache: int = 200
    report_errors: bool = True
    startup_check: bool = True
    unknown_command_hint: bool = True
    max_message_chars: int = 30000
    #: Model calls allowed to be in flight at once; 0 lifts the ceiling. Every
    #: trigger becomes a background task with no limit of its own, so a busy room
    #: or a burst of redeliveries means that many completions open together, each
    #: holding the llm.timeout open. Talk rate-limits the replies we send, not
    #: the events it sends us, so nothing upstream applies the brakes either.
    max_concurrent_replies: int = 8

    # --- conversation memory ----------------------------------------------
    history_turns: int = 12
    history_ttl: int = 3600

    # --- LLM ---------------------------------------------------------------
    llm: LLMConfig = field(default_factory=LLMConfig)

    # --- people to ignore --------------------------------------------------
    #: Events from these users are dropped entirely. Entries match the bare user
    #: id, the full actor id, or the display name.
    ignore_users: list[str] = field(default_factory=list)

    # --- who may run which command -----------------------------------------
    #: Commands only the admin users may run. ``*`` stands for every command,
    #: with normal_commands naming the exceptions.
    admin_commands: list[str] = field(default_factory=list)
    #: Commands everybody may run. Every command not in admin_commands is
    #: already one of these, so this only carries weight against ``*`` - and as
    #: somewhere to write the intent down where an operator will read it.
    normal_commands: list[str] = field(default_factory=list)
    #: Nextcloud user ids allowed to run the admin commands.
    admin_users: list[str] = field(default_factory=list)

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

    # --- the HTTP surface itself -------------------------------------------
    #: Serve FastAPI's generated schema and the two doc pages built from it.
    #: Off by default: they describe every route, header and body shape to
    #: anybody who can reach the service, and nothing needs them at runtime.
    api_docs: bool = False
    #: When set, GET /healthz requires it in an X-Health-Token header. Empty
    #: leaves the probe open, which is what a container or k8s check expects.
    health_token: str = ""
    #: Peers whose X-Forwarded-For and X-Forwarded-Proto we believe: IP
    #: addresses, CIDR ranges, or ``*`` for any client. Empty trusts nobody.
    #: Nothing in sable reads the client address, so this decides whether the
    #: access log tells the truth, not who gets in.
    trusted_proxies: list[str] = field(
        default_factory=lambda: list(DEFAULT_TRUSTED_PROXIES)
    )

    # --- things that look wrong but might not be ----------------------------
    #: Settings that are probably a mistake but that sable cannot rule out, so
    #: they are said once at startup rather than refused. An empty tuple is the
    #: normal case and prints nothing.
    warnings: tuple[str, ...] = ()

    # --- time ---------------------------------------------------------------
    #: IANA name for the zone the bot answers in, e.g. ``America/New_York``.
    #: Empty follows the host clock. The model is told the date either way: a
    #: model that does not know today will answer "what is it now" with whatever
    #: was true when it was trained, confidently and wrongly.
    timezone: str = ""

    # --- process -----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    @property
    def notify_enabled(self) -> bool:
        return bool(self.notify_token)

    @property
    def inbound_secrets(self) -> tuple[str, ...]:
        """The secrets an incoming signature may have been made with, current first.

        One entry normally, two while a rotation is in progress. Try them in this
        order and stop at the first that verifies.

        Incoming verification only. Everything sable *sends* - the bot API calls
        in bot.py - is signed with ``bot_secret`` and never with the previous one:
        Talk has already been given the new value by then, so signing with the old
        one would be rejected. The previous secret exists to keep believing events
        that were signed before the reinstall, nothing more.
        """
        if self.bot_secret_previous:
            return (self.bot_secret, self.bot_secret_previous)
        return (self.bot_secret,)

    @property
    def health_guarded(self) -> bool:
        """Does GET /healthz need a token?"""
        return bool(self.health_token)

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

    @property
    def fragile_ignore_users(self) -> list[str]:
        """The ignore_users entries that look like display names rather than ids.

        Whitespace is the practical signal: a Nextcloud user id has none, and a
        display name usually does. Worth putting in front of an operator at
        startup, because a name is the one kind of entry the ignored person can
        defeat themselves, by renaming - their ignore then quietly lapses, which
        is the opposite of what was configured.

        Returns the entries and judges nothing else; naming a person by their
        display name is allowed, and sometimes it is all an operator has.
        """
        return [entry for entry in self.ignore_users if re.search(r"\s", entry.strip())]

    @staticmethod
    def _listed(entries: list[str], name: str) -> bool:
        wanted = name.strip().casefold()
        return bool(wanted) and any(entry.strip().casefold() == wanted for entry in entries)

    def admin_only(self, *names: str) -> bool:
        """Does running this command need an admin?

        Pass the command's own name and its aliases. Either is a reasonable
        thing for an operator to have written down, and restricting ``reset``
        has to restrict ``forget`` with it or the restriction is decoration.
        """
        if any(self._listed(self.normal_commands, name) for name in names):
            return False
        if "*" in self.admin_commands:
            return True
        return any(self._listed(self.admin_commands, name) for name in names)

    def is_admin_user(self, user_id: str) -> bool:
        """May this Nextcloud user run the admin commands?

        Matched on the user id and nothing else. A display name is whatever the
        person says it is, so matching one would hand the admin commands to
        anybody who can join the room and rename themselves - which is why
        SABLE_IGNORE_USERS may match a name and this may not. Guests and bots
        have no user id at all, so they are never admins.
        """
        if not user_id or not self.admin_users:
            return False
        wanted = user_id.casefold()
        return any(
            entry.strip().casefold().removeprefix("users/") == wanted
            for entry in self.admin_users
        )

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

    def ask_room_allowed(self, token: str, name: str = "") -> bool:
        """Should this conversation's messages be cached for the ask reaction?

        Entries are matched exactly as :meth:`ai_room_allowed` matches its own -
        conversation token or display name, ignoring case and surrounding space,
        with ``*`` for all of them - and tokens are preferable here for the same
        reason: renaming a conversation would otherwise change what is cached.

        An empty list means **every** conversation, where an empty ``ai_rooms``
        means none. The asymmetry is deliberate: the cache is on today for every
        room the bot is in, and reading empty as none would turn the reaction off
        across every existing deployment the moment it upgraded.
        """
        if not self.ask_rooms or "*" in self.ask_rooms:
            return True
        if token and token in self.ask_rooms:
            return True
        wanted = name.strip().casefold()
        return bool(wanted) and any(
            entry.strip().casefold() == wanted for entry in self.ask_rooms
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
        previous = _str("SABLE_BOT_SECRET_PREVIOUS")
        if previous and not 40 <= len(previous) <= 128:
            raise ConfigError(
                "SABLE_BOT_SECRET_PREVIOUS must be 40-128 characters, the same range "
                f"Nextcloud accepts for the secret it replaces (got {len(previous)})."
            )
        if previous and previous == secret:
            raise ConfigError(
                "SABLE_BOT_SECRET_PREVIOUS is the same value as SABLE_BOT_SECRET, so "
                "nothing has been rotated. It is there to hold the secret you are "
                "rotating away from; set it to the old value, or unset it."
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
            backend=_str("SABLE_LLM_BACKEND", "openai").lower(),
            tool_ids=_csv("SABLE_LLM_TOOL_IDS"),
            features=[name.lower() for name in _csv("SABLE_LLM_FEATURES")],
            builtin_tools=_bool("SABLE_LLM_BUILTIN_TOOLS", True),
            # No `or 2.0` fallback: a zero here would busy-loop against the
            # task endpoint, so it is worth refusing rather than quietly
            # reading as "unset".
            poll_interval=_float("SABLE_LLM_POLL_INTERVAL", 2.0) or 0.0,
            keep_chats=_bool("SABLE_LLM_KEEP_CHATS", False),
            show_sources=_bool("SABLE_LLM_SHOW_SOURCES", False),
        )

        config = cls(
            bot_secret=secret,
            bot_secret_previous=previous,
            bot_name=_str("SABLE_BOT_NAME", "sable"),
            nextcloud_url=_str("SABLE_NEXTCLOUD_URL").rstrip("/"),
            pin_backend=_bool("SABLE_PIN_BACKEND", True),
            command_prefix=_str("SABLE_COMMAND_PREFIX", "!") or "!",
            ai_rooms=_csv("SABLE_AI_ROOMS"),
            reply_as_reply=_bool("SABLE_REPLY_AS_REPLY", False),
            thinking_reaction=_str("SABLE_THINKING_REACTION"),
            ask_reaction=_str("SABLE_ASK_REACTION", "⁉️"),
            ask_admins_only=_bool("SABLE_ASK_ADMINS_ONLY", False),
            ask_rooms=_csv("SABLE_ASK_ROOMS"),
            message_cache=_int("SABLE_MESSAGE_CACHE", 200),
            report_errors=_bool("SABLE_REPORT_ERRORS", True),
            startup_check=_bool("SABLE_STARTUP_CHECK", True),
            unknown_command_hint=_bool("SABLE_UNKNOWN_COMMAND_HINT", True),
            max_message_chars=_int("SABLE_MAX_MESSAGE_CHARS", 30000),
            max_concurrent_replies=_int("SABLE_MAX_CONCURRENT_REPLIES", 8),
            history_turns=_int("SABLE_HISTORY_TURNS", 12),
            history_ttl=_int("SABLE_HISTORY_TTL", 3600),
            llm=llm,
            ignore_users=_csv("SABLE_IGNORE_USERS"),
            admin_commands=_csv("SABLE_ADMIN_COMMANDS"),
            normal_commands=_csv("SABLE_NORMAL_COMMANDS"),
            admin_users=_csv("SABLE_ADMIN_USERS"),
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
            api_docs=_bool("SABLE_API_DOCS", False),
            trusted_proxies=_csv_or("SABLE_TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES),
            health_token=_str("SABLE_HEALTH_TOKEN"),
            timezone=_str("SABLE_TIMEZONE"),
            host=_str("SABLE_HOST", "0.0.0.0"),
            port=_int("SABLE_PORT", 8080),
            log_level=_str("SABLE_LOG_LEVEL", "INFO").upper(),
        )

        if "*" in config.trusted_proxies and len(config.trusted_proxies) > 1:
            raise ConfigError(
                "SABLE_TRUSTED_PROXIES lists '*' alongside other entries, but '*' "
                "already means every client. Drop one or the other."
            )
        for entry in config.trusted_proxies:
            if entry == "*":
                continue
            try:
                # strict=True, matching uvicorn: it keeps an unparseable entry as
                # a literal that can never match a peer address, so a typo would
                # silently stop the header being believed. Refuse it here instead.
                ipaddress.ip_network(entry) if "/" in entry else ipaddress.ip_address(entry)
            except ValueError as exc:
                raise ConfigError(
                    f"SABLE_TRUSTED_PROXIES entry {entry!r} is not an IP address "
                    f"or a CIDR range ({exc}). A range must have no host bits set, "
                    f"so 172.17.0.0/16 rather than 172.17.0.5/16."
                ) from exc

        if "*" in config.normal_commands:
            raise ConfigError(
                "SABLE_NORMAL_COMMANDS cannot be '*': every command not named in "
                "SABLE_ADMIN_COMMANDS is open to everyone already. Use it to name "
                "the exceptions to SABLE_ADMIN_COMMANDS=*."
            )
        contested = sorted(
            {name.strip().casefold() for name in config.admin_commands}
            & {name.strip().casefold() for name in config.normal_commands}
        )
        if contested:
            raise ConfigError(
                f"{', '.join(contested)}: in both SABLE_ADMIN_COMMANDS and "
                "SABLE_NORMAL_COMMANDS, so who may run them is not decided. Name "
                "each command in one list or the other."
            )
        if config.admin_commands and not config.admin_users:
            raise ConfigError(
                "SABLE_ADMIN_COMMANDS is set but SABLE_ADMIN_USERS is empty, so "
                "nobody at all could run those commands. Name the administrators, "
                "or drop the commands from the list to leave them open."
            )
        if config.ask_admins_only and not config.admin_users:
            raise ConfigError(
                "SABLE_ASK_ADMINS_ONLY is on but SABLE_ADMIN_USERS is empty, so "
                "nobody at all could use the reaction. Name the administrators, or "
                "turn it off to leave the reaction open to everyone."
            )
        if config.max_concurrent_replies < 0:
            raise ConfigError(
                "SABLE_MAX_CONCURRENT_REPLIES cannot be negative. Use 0 for no "
                "ceiling at all, or a count of model calls to allow at once "
                f"(got {config.max_concurrent_replies})."
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
        if config.timezone:
            try:
                ZoneInfo(config.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ConfigError(
                    f"SABLE_TIMEZONE={config.timezone!r} is not an IANA time zone "
                    f"name such as America/New_York or Europe/Berlin ({exc})"
                ) from exc
        warnings: list[str] = []
        if config.llm.backend not in LLM_BACKENDS:
            raise ConfigError(
                f"SABLE_LLM_BACKEND must be one of {', '.join(sorted(LLM_BACKENDS))}, "
                f"got {config.llm.backend!r}"
            )
        if config.llm.agentic:
            if not config.llm.api_key:
                raise ConfigError(
                    "SABLE_LLM_API_KEY is required for the openwebui backend: the "
                    "key is the account whose permissions the tools run with"
                )
            if not config.llm.base_url.endswith("/api"):
                # Not fatal: a proxy may rewrite the path, so this address can
                # be right even when it does not look it. Everything hangs off
                # this URL though, so a mistake here 404s every question - worth
                # saying once at startup rather than leaving to be discovered.
                warnings.append(
                    f"SABLE_LLM_BASE_URL is {config.llm.base_url!r}, which does "
                    "not end in /api. Open WebUI serves the completion, chat and "
                    "task endpoints under /api, so unless a proxy rewrites the "
                    "path, every question will fail with a 404. Expected "
                    "something like https://ai.example.org/api"
                )
            if config.llm.poll_interval <= 0:
                raise ConfigError("SABLE_LLM_POLL_INTERVAL must be greater than zero")
        unknown = sorted(set(config.llm.features) - BUILTIN_FEATURES)
        if unknown:
            raise ConfigError(
                "SABLE_LLM_FEATURES may name "
                + ", ".join(sorted(BUILTIN_FEATURES))
                + "; got "
                + ", ".join(unknown)
            )
        if config.llm.features and not config.llm.agentic:
            raise ConfigError(
                "SABLE_LLM_FEATURES only applies to the openwebui backend; set "
                "SABLE_LLM_BACKEND=openwebui or clear it"
            )
        if config.llm.features and not config.llm.builtin_tools:
            raise ConfigError(
                "SABLE_LLM_FEATURES needs SABLE_LLM_BUILTIN_TOOLS on: without a "
                "session id Open WebUI does not offer the built-in tools at all"
            )
        forbidden = sorted({"stream", "messages"} & set(config.llm.extra_body))
        if forbidden:
            raise ConfigError(
                "SABLE_LLM_EXTRA_BODY must not set "
                + ", ".join(forbidden)
                + ": sable builds those itself, and overriding them breaks the "
                "reply it gets back"
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
        object.__setattr__(config, "warnings", tuple(warnings))
        return config
