"""The HTTP surface: two ways in for alerts, and a health check.

Chat is not received here. sable reads Talk by long-polling as a user account
(see :mod:`sable.poller`), which this app starts and stops with its lifespan;
every event that poll finds is handled in the background under a ceiling on how
many model calls run at once.

Routes
------
``POST /notify``      Inbound alerting: other systems post JSON here with a
                      bearer token and we relay it into a conversation.
``POST /hook/{name}`` The same, for services that cannot speak that shape. Each
                      hook has its own token and its own conversation.
``GET  /healthz``     Liveness probe, plus the few settings worth confirming from
                      outside and whether Nextcloud is answering. Open unless
                      SABLE_HEALTH_TOKEN is set.
``GET  /``            The version, as plain text.

Every one of them checks its credential before doing anything else. FastAPI's
schema and the /docs and /redoc pages built from it are not served unless
SABLE_API_DOCS is on.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, ValidationError
# Starlette's own class: request.form() yields these, and FastAPI's UploadFile is a
# subclass, so checking against the base accepts both.
from starlette.datastructures import UploadFile

from . import __version__
from .bot import Bot
from .config import TOKEN_HINT, TOKEN_RE, Config
from .files import FilesError
from .hooks import render, render_with_template
from .limits import BodyLimitMiddleware
from .mentions import defang_mentions
from .poller import Poller
from .talk import TalkError

log = logging.getLogger(__name__)

#: How long shutdown waits for in-flight replies to finish.
DRAIN_TIMEOUT = 30.0

#: Guards GET /healthz when SABLE_HEALTH_TOKEN is set. A header rather than a
#: query parameter, so the value stays out of proxy and access logs.
HEADER_HEALTH_TOKEN = "X-Health-Token"

#: Cap on any request body that has no reason to be large: health checks, the
#: root, a wrong method on a real route. 64 KiB.
SMALL_BODY_BYTES = 64 * 1024

#: Room on top of SABLE_MAX_HOOK_BYTES, so a payload just over the setting still
#: reaches the handler and gets its own, more specific, 413.
HOOK_SLACK_BYTES = 1024

#: At most one "dropping replies" warning per this many seconds.
DROP_WARNING_INTERVAL = 30.0

#: Limits handed to Starlette's multipart parser. Only /notify takes a form: at
#: most the one file and a handful of short fields. max_part_size caps a *field*
#: (message, room, ...) - an uploaded file is not a field, and is capped by the
#: body limit and _read_upload instead.
FORM_MAX_FILES = 1
FORM_MAX_FIELDS = 10
FORM_MAX_FIELD_BYTES = 256 * 1024


def notify_body_cap(max_upload_bytes: int) -> int:
    """The largest POST /notify body worth reading.

    A file of N bytes costs ceil(N * 4 / 3) as base64 in a JSON body (4 output
    characters per 3 input bytes, so this is the worst case; padding adds at most
    two more), and N plus boundary lines as multipart, which is the smaller of the
    two. 64 KiB on top covers the JSON syntax, the other fields and a message.
    """
    return math.ceil(max_upload_bytes * 4 / 3) + 64 * 1024


def _same_secret(supplied: str, expected: str) -> bool:
    """Constant-time comparison of two secrets, as UTF-8 bytes.

    hmac.compare_digest raises TypeError for a str holding a non-ASCII character,
    and whatever a client puts in a header or a query string is not ours to
    constrain. Comparing bytes cannot raise. ``surrogatepass`` keeps a lone
    surrogate (which a decoder can hand us) from raising on encode as well.
    """
    return hmac.compare_digest(
        supplied.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass")
    )


class NotifyFile(BaseModel):
    """A file attached to an alert, base64 encoded in a JSON request."""

    name: str = Field(min_length=1, max_length=255, description="Filename to show in Talk")
    content: str = Field(min_length=1, description="The file's bytes, base64 encoded")


class NotifyRequest(BaseModel):
    """An alert to relay into a conversation.

    ``message`` is optional when a file is attached, in which case it becomes the
    file's caption rather than a second chat message.
    """

    room: str = Field(min_length=1, description="Conversation token, or an alias from SABLE_NOTIFY_ROOMS")
    message: str = Field(default="", description="Markdown message body, or a caption for a file")
    silent: bool = Field(default=False, description="Post without triggering notifications")
    reply_to: int = Field(default=0, ge=0, alias="replyTo", description="Message id to reply to")
    file: NotifyFile | None = Field(default=None, description="An attachment, base64 encoded")

    model_config = {"populate_by_name": True}


def create_app(
    config: Config | None = None,
    bot: Bot | None = None,
    *,
    receive: bool | None = None,
) -> FastAPI:
    """Build the ASGI app. Pass ``config``/``bot`` in tests; otherwise read the env.

    ``receive`` says whether the lifespan starts reading chat. It defaults to on
    when this function builds the Bot itself and off when it is handed one, so a
    test that supplies a Bot with a fake transport never finds a poller running
    behind its back.
    """
    config = config or (bot.config if bot else Config.from_env())
    receiving = (bot is None) if receive is None else receive
    tasks: set[asyncio.Task[None]] = set()
    #: The ceiling on concurrent replies, built in the lifespan below: a
    #: Semaphore binds to the loop it was created on, and create_app runs before
    #: there is one (uvicorn calls it as a factory, tests build the app and drive
    #: it later). None means no ceiling at all.
    slots: asyncio.Semaphore | None = None
    #: Replies spawned and not yet finished, running or waiting. With the
    #: ceiling on, at most max_concurrent_replies + max_queued_replies of them
    #: are allowed; the rest are dropped in spawn().
    live = 0
    last_drop_warning = float("-inf")
    dropped = 0

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal slots
        app.state.bot = bot or Bot(config)
        poller = Poller(app.state.bot, spawn)
        app.state.poller = poller
        slots = (
            asyncio.Semaphore(config.max_concurrent_replies)
            if config.max_concurrent_replies
            else None
        )

        # What is running, and with what. An operator reading only the first
        # dozen lines of the log should be able to tell whether the thing is
        # configured the way they meant.
        log.info("sable %s starting", __version__)
        log.info("  listening on:   http://%s:%s", config.host, config.port)
        log.info("  nextcloud:      %s as %s", config.nextcloud_url, config.nextcloud_user)
        log.info(
            "  receiving:      long polls of up to %ss, conversations rescanned every %ss",
            config.poll_timeout,
            config.room_refresh,
        )
        log.info("  command prefix: %r", config.command_prefix)
        log.info(
            "  model:          %s",
            f"{config.llm.model} at {config.llm.base_url}" if config.llm.enabled else "disabled",
        )
        if config.llm.enabled and config.llm.agentic:
            log.info("  tools:          %s", tools_summary(config))
        log.info("  concurrency:    %s", concurrency_summary(config))
        log.info("  ask reaction:   %s", ask_reaction_summary(config))
        log.info("  rooms:          %s", rooms_summary(config))
        log.info(
            "  model users:    %s",
            ", ".join(config.llm_users) + " and the administrators"
            if config.llm_users
            else "everyone",
        )
        log.info(
            "  rate limit:     %s",
            f"{config.rate_limit} triggers a minute per person"
            if config.rate_limit
            else "off",
        )
        log.info("  admin commands: %s", admin_summary(config))
        log.info(
            "  ai rooms:       %s",
            ", ".join(config.ai_rooms) if config.ai_rooms else "(mentions only)",
        )
        log.info(
            "  alerting:       %s",
            f"enabled, aliases: {', '.join(config.notify_rooms)}"
            if config.notify_enabled and config.notify_rooms
            else ("enabled" if config.notify_enabled else "disabled (/notify answers 404)"),
        )
        log.info(
            "  attachments:    into %s, up to %s",
            config.upload_path,
            megabytes(config.max_upload_bytes),
        )
        log.info(
            "  hooks:          %s",
            ", ".join(
                f"/hook/{name} -> {config.hook_room(name)}"
                + (f" (alias {room})" if config.hook_room(name) != room else "")
                for name, room in sorted(config.hooks.items())
            )
            or "(none)",
        )
        log.info(
            "  ignoring:       %s",
            ", ".join(config.ignore_users) if config.ignore_users else "(nobody)",
        )
        if config.fragile_ignore_users:
            # Deliberately not a banner line: it is a warning about the line
            # above, and an operator with no such entries should see nothing.
            log.warning(
                "SABLE_IGNORE_USERS holds whitespace in %s, so that can only be "
                "matching a display name - which the person themselves can change, "
                "and their ignore then quietly lapses. Prefer the user id.",
                ", ".join(repr(entry) for entry in config.fragile_ignore_users),
            )
        log.info("  proxy trust:    %s", proxy_trust_summary(config))
        log.info(
            "  api docs:       %s",
            "/docs, /redoc, /openapi.json"
            if config.api_docs
            else "disabled (SABLE_API_DOCS=true to serve them)",
        )
        log.info(
            "  health check:   %s",
            f"GET /healthz ({HEADER_HEALTH_TOKEN} required)"
            if config.health_guarded
            else "GET /healthz (open)",
        )
        log.info("  log level:      %s", config.log_level)

        for warning in config.warnings:
            # Settings sable cannot rule out but doubts. Said after the block, so
            # the reader has the resolved configuration in front of them.
            log.warning("%s", warning)

        if config.startup_check:
            await app.state.bot.check_nextcloud()
        if receiving:
            poller.start()

        log.info("sable %s ready", __version__)
        try:
            yield
        finally:
            log.info("sable %s stopping", __version__)
            # First, so nothing new is promised a reply while the rest drains.
            await poller.stop()
            if tasks:
                # In flight or still waiting for a slot: both are tasks that were
                # promised a 200, so both are worth draining.
                log.info("waiting for %d reply/replies to finish", len(tasks))
                await asyncio.wait(tasks, timeout=DRAIN_TIMEOUT)
            if bot is None:
                await app.state.bot.aclose()
            log.info("sable %s stopped", __version__)

    app = FastAPI(
        title="sable",
        version=__version__,
        description="A Nextcloud Talk assistant that runs as a user account.",
        lifespan=lifespan,
        # None removes the route entirely rather than hiding it. The schema
        # describes every endpoint and body shape to whoever can reach the
        # service, so this is off unless asked for.
        docs_url="/docs" if config.api_docs else None,
        redoc_url="/redoc" if config.api_docs else None,
        openapi_url="/openapi.json" if config.api_docs else None,
    )

    def body_cap(method: str, path: str) -> int | None:
        if method == "POST" and path == "/notify":
            return notify_body_cap(config.max_upload_bytes)
        if method == "POST" and path.startswith("/hook/"):
            return config.max_hook_bytes + HOOK_SLACK_BYTES
        return SMALL_BODY_BYTES

    app.add_middleware(BodyLimitMiddleware, cap_for=body_cap)

    async def under_the_ceiling(coro) -> None:
        """Wait for a free slot, then run the handler.

        The waiting happens here, inside the background task, and never in the
        poll loop: a loop that stopped reading while it waited would fall behind
        the conversation. What the ceiling delays is the reply.
        """
        if slots is None:
            await coro
            return
        if slots.locked():
            log.info(
                "all %d reply slots are busy; this one waits its turn "
                "(SABLE_MAX_CONCURRENT_REPLIES)",
                config.max_concurrent_replies,
            )
        async with slots:
            await coro

    def spawn(coro) -> None:
        """Run a handler detached from the poll loop, keeping a strong reference.

        With a ceiling, the number of replies waiting for a slot is bounded by
        SABLE_MAX_QUEUED_REPLIES: past that the new work is dropped, because an
        unbounded pile of parked tasks is the same flood the ceiling was meant to
        stop, only cheaper. Dropping happens here, in the poll loop, which is why
        it must be quick and never wait.
        """
        nonlocal live, last_drop_warning, dropped
        if slots is not None and live >= config.max_concurrent_replies + config.max_queued_replies:
            coro.close()  # never started, so close it rather than leave "never awaited"
            dropped += 1
            now = time.monotonic()
            if now - last_drop_warning >= DROP_WARNING_INTERVAL:
                log.warning(
                    "dropping replies: %d are running or queued, the most "
                    "SABLE_MAX_CONCURRENT_REPLIES (%d) + SABLE_MAX_QUEUED_REPLIES "
                    "(%d) allow; %d dropped so far",
                    live,
                    config.max_concurrent_replies,
                    config.max_queued_replies,
                    dropped,
                )
                last_drop_warning = now
            return
        live += 1

        def finished(task: asyncio.Task[None]) -> None:
            nonlocal live
            live -= 1
            tasks.discard(task)

        task = asyncio.create_task(under_the_ceiling(coro))
        tasks.add(task)
        task.add_done_callback(finished)
        task.add_done_callback(_log_task_failure)

    def current_bot(request: Request) -> Bot:
        """The Bot built during startup. Read from app.state rather than through
        Depends: a locally defined alias is not resolvable under postponed
        annotations, and FastAPI would take it for a query parameter."""
        return request.app.state.bot

    @app.get("/healthz", tags=["ops"])
    async def healthz(
        request: Request,
        health_token: Annotated[str, Header(alias=HEADER_HEALTH_TOKEN)] = "",
    ) -> dict[str, object]:
        """Liveness, plus the handful of settings worth confirming from outside.

        Open unless SABLE_HEALTH_TOKEN is set: a container healthcheck and a
        kubelet probe both expect to call this without credentials. With a token
        set, the answer names the model and the version, so it is guarded.

        ``nextcloud`` reports what the last call to it did: true reachable, false
        unreachable, null nothing tried yet. The status stays "ok" either way and
        so does the 200 - this is a liveness probe, and failing it because a
        dependency is down asks an orchestrator to restart a process that is
        working perfectly. Whoever is reading decides what an outage means.
        """
        if config.health_token and not _same_secret(health_token.strip(), config.health_token):
            log.warning("rejected a health check with a bad or missing token")
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                f"a valid {HEADER_HEALTH_TOKEN} header is required",
            )
        return {
            "status": "ok",
            "version": __version__,
            "user": current_bot(request).user_id,
            "llm": config.llm.model or None,
            "notify": config.notify_enabled,
            "nextcloud": current_bot(request).nextcloud.up,
        }

    @app.post("/notify", status_code=status.HTTP_201_CREATED, tags=["alerting"])
    async def notify(
        request: Request,
        authorization: Annotated[str, Header()] = "",
    ) -> dict[str, object]:
        """Relay a message, optionally with a file, into a conversation.

        One URL, one call, three accepted shapes: JSON, JSON with a base64
        ``file``, or multipart/form-data with an uploaded ``file``.
        """
        if not config.notify_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "alerting endpoint is disabled")
        _check_bearer(authorization, config.notify_token)

        payload, attachment = await _parse_notify(request, config.max_upload_bytes)
        if not payload.message and attachment is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "send a message, a file, or both",
            )

        room = config.notify_rooms.get(payload.room, payload.room)
        if not TOKEN_RE.match(room):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{payload.room!r} is not a known alias or a conversation "
                f"token: {TOKEN_HINT}",
            )

        if attachment is not None:
            return await _relay_file(request, payload, room, attachment)

        try:
            message_id = await current_bot(request).send(
                room, payload.message, silent=payload.silent, reply_to=payload.reply_to
            )
        except TalkError as exc:
            log.warning("relaying an alert to %s failed: %s", room, exc)
            # 404/400 from Talk is the caller's problem; anything else is ours.
            code = (
                status.HTTP_400_BAD_REQUEST
                if exc.status in {400, 404}
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(code, f"Talk rejected the message: {exc}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"could not reach Nextcloud: {exc}"
            ) from exc

        log.info(
            "relayed an alert to %s%s as message %s",
            room,
            f" (alias {payload.room})" if payload.room != room else "",
            message_id or "?",
        )
        return {"ok": True, "room": room, "messageId": message_id}

    @app.post("/hook/{name}", status_code=status.HTTP_201_CREATED, tags=["alerting"])
    async def hook(name: str, request: Request) -> dict[str, object]:
        """Receive a webhook from another service and post it to a conversation.

        For services that cannot speak `/notify`'s shape - Komodo, Alertmanager,
        Grafana and most of the rest. Each hook has its own conversation and its
        own token, and the payload is rendered generically unless the hook has a
        format string.
        """
        room = config.hook_room(name)
        if not room:
            # A name that is not configured and hooks not being configured at all
            # answer the same 404, so this does not say whether the feature is in
            # use. It does distinguish a configured hook, which answers 401 for a
            # bad token, from an unconfigured one - so hook names are guessable by
            # probing. Each hook carries its own token, so what that costs is the
            # name rather than access.
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no hook named {name!r}")

        # Komodo and friends cannot set headers, so the token may come from the
        # query string. That is a real exposure and is documented as one.
        supplied = request.query_params.get("token", "")
        header = request.headers.get("authorization", "")
        if not supplied and header:
            scheme, _, value = header.partition(" ")
            supplied = value.strip() if scheme.lower() == "bearer" else ""
        if not _same_secret(supplied, config.hook_token(name)):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "this hook needs its own token, as a bearer header or ?token=",
                headers={"WWW-Authenticate": "Bearer"},
            )

        body = await request.body()
        if len(body) > config.max_hook_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"the payload is larger than SABLE_MAX_HOOK_BYTES "
                f"({config.max_hook_bytes} bytes)",
            )

        text = body.decode("utf-8", "replace")
        try:
            payload = json.loads(body) if body.strip() else {}
        except (ValueError, RecursionError):
            # Not everything sends JSON, and some of it is not even text: invalid
            # UTF-8 and absurdly deep nesting land here as well. Text is better
            # than a rejection. (JSONDecodeError and UnicodeDecodeError are both
            # ValueErrors; json.loads also raises ValueError past the int limit.)
            payload = text

        template = config.hook_templates.get(name.strip().lower(), "")
        try:
            message = _render_hook(name, template, payload)
        except RecursionError:
            # Parsed, but nested deeper than the renderer can walk.
            message = _render_hook(name, template, text)
        # The payload is third-party text: it must not be able to ping everyone.
        # (/notify is not treated this way; its caller may mean it.)
        message = defang_mentions(message)

        if not message.strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "the payload rendered as nothing"
            )

        talk_bot = current_bot(request)
        try:
            message_id = await talk_bot.send(room, message)
        except TalkError as exc:
            log.warning("posting hook %r to %s failed: %s", name, room, exc)
            code = (
                status.HTTP_400_BAD_REQUEST
                if exc.status in {400, 404}
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(code, f"Talk rejected the message: {exc}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"could not reach Nextcloud: {exc}"
            ) from exc

        log.info(
            "hook %r posted %d chars to %s as message %s",
            name,
            len(message),
            room,
            message_id or "?",
        )
        return {"ok": True, "hook": name, "room": room, "messageId": message_id}

    @app.get("/", include_in_schema=False)
    async def root() -> Response:
        return Response(f"sable {__version__}\n", media_type="text/plain")

    return app


#: Who the trusted-proxy list is actually a setting on. __main__ hands it to
#: uvicorn.run as forwarded_allow_ips, so serving create_app() under any other
#: ASGI server - gunicorn, hypercorn, an embedding process - leaves it unread.
#: The startup line is logged from here either way, so it names the server the
#: setting belongs to rather than claiming it as sable's own behaviour.
UVICORN = "sable's own uvicorn"


def proxy_trust_summary(config: Config) -> str:
    """The proxy trust line in the startup block."""
    if not config.trusted_proxies:
        return f"nobody - {UVICORN} ignores X-Forwarded-For and X-Forwarded-Proto"
    if "*" in config.trusted_proxies:
        return (
            f"* - ANY client's X-Forwarded-For is believed by {UVICORN}; only safe "
            f"behind a proxy that overwrites it"
        )
    return (
        f"{', '.join(config.trusted_proxies)} - believed by {UVICORN}, "
        f"and read by nothing else"
    )


def ask_reaction_summary(config: Config) -> str:
    """The ask-reaction line in the startup block."""
    if not config.ask_reaction:
        return "disabled"
    if config.ask_admins_only:
        return f"{config.ask_reaction}  (administrators only)"
    return config.ask_reaction


def rooms_summary(config: Config) -> str:
    """Which conversations the account follows, and whether it leaves the rest."""
    if not config.allowed_rooms:
        return "every conversation the account is in (SABLE_ALLOWED_ROOMS is empty)"
    line = ", ".join(config.allowed_rooms)
    if config.leave_unlisted_rooms:
        line += " (leaves the others, except /notify and /hook destinations)"
    return line


def tools_summary(config: Config) -> str:
    """What the Open WebUI backend will let the model reach.

    Worth printing in full: these run with the API key's own permissions, so the
    line answers "what can a stranger in a chat room set off" at a glance.
    """
    parts = [
        "server-side loop via Open WebUI",
        f"tools: {', '.join(config.llm.tool_ids) or 'none'}",
        "in rooms: "
        + (", ".join(config.llm.tool_rooms) if config.llm.tool_rooms else "none (off everywhere)"),
    ]
    if not config.llm.builtin_tools:
        parts.append("built-ins off (one blocking request, SABLE_LLM_BUILTIN_TOOLS)")
    else:
        parts.append(f"built-ins: {', '.join(config.llm.features) or 'none enabled'}")
    if config.llm.keep_chats:
        parts.append("conversations kept")
    return " · ".join(parts)


def concurrency_summary(config: Config) -> str:
    """The concurrency line in the startup block.

    Spelled out in both states, because the ceiling is what stands between a
    burst of messages and as many open model calls as there were events, each
    holding SABLE_LLM_TIMEOUT open.
    """
    if not config.max_concurrent_replies:
        return (
            "no ceiling (SABLE_MAX_CONCURRENT_REPLIES=0) - every event that "
            "arrives starts a model call of its own"
        )
    return (
        f"up to {config.max_concurrent_replies} replies at once, the rest queued "
        f"(at most {config.max_queued_replies} waiting, then dropped: "
        f"SABLE_MAX_QUEUED_REPLIES)"
    )


def admin_summary(config: Config) -> str:
    """The command authorization line in the startup block."""
    if not config.admin_commands:
        return "(none - anyone in a conversation can run any command)"
    which = ", ".join(config.admin_commands)
    if config.normal_commands:
        which += f", except {', '.join(config.normal_commands)}"
    return f"{which} - only for {', '.join(config.admin_users)}"


def megabytes(value: int) -> str:
    """A byte count as something a person can read at a glance."""
    size = value / (1024 * 1024)
    return f"{size:.0f} MB" if abs(size - round(size)) < 0.05 else f"{size:.1f} MB"


def _render_hook(name: str, template: str, payload: object) -> str:
    """The message for a hook payload: its format string if it has one, else generic."""
    if not template:
        return render(payload)
    message, missing = render_with_template(template, payload)
    if missing:
        log.warning(
            "hook %r: its format string asks for %s, which the payload does not have",
            name,
            ", ".join(sorted(set(missing))),
        )
    return message


def _form_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _form_int(value: object) -> int:
    try:
        return int(str(value or "0"))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"not a number: {value!r}"
        ) from exc


@dataclass(frozen=True)
class Attachment:
    """A file to attach, however it arrived on the wire."""

    name: str
    data: bytes


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    """Read an uploaded file, refusing anything over the limit.

    Read in chunks and stop at the cap rather than trusting a declared length:
    /notify is reachable by whoever holds the token, and the whole body would
    otherwise land in memory.
    """
    data = bytearray()
    while chunk := await upload.read(64 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"the attachment is larger than SABLE_MAX_UPLOAD_BYTES ({limit} bytes)",
            )
    return bytes(data)


async def _parse_notify(
    request: Request, limit: int
) -> tuple[NotifyRequest, Attachment | None]:
    """One endpoint, three shapes: JSON, JSON with a base64 file, or multipart.

    Content-Type decides. Everything ends up as a NotifyRequest plus an optional
    Attachment, so the handler below does not care which shape it arrived in.
    """
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    if content_type == "multipart/form-data":
        form = await request.form(
            max_files=FORM_MAX_FILES,
            max_fields=FORM_MAX_FIELDS,
            max_part_size=FORM_MAX_FIELD_BYTES,
        )
        upload = form.get("file")
        if upload is not None and not isinstance(upload, UploadFile):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "'file' must be an uploaded file"
            )
        payload = _validated(
            lambda: NotifyRequest(
                room=str(form.get("room") or ""),
                message=str(form.get("message") or ""),
                silent=_form_bool(form.get("silent")),
                reply_to=_form_int(form.get("replyTo") or form.get("reply_to") or 0),
            )
        )
        if upload is None:
            return payload, None
        return payload, Attachment(
            upload.filename or "attachment", await _read_upload(upload, limit)
        )

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"invalid JSON: {exc}"
        ) from exc
    except RecursionError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "invalid JSON: nested too deeply"
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the body must be a JSON object, not {_json_kind(body)}",
        )
    payload = _validated(lambda: NotifyRequest.model_validate(body))
    if payload.file is None:
        return payload, None
    try:
        content = base64.b64decode(payload.file.content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"file.content is not valid base64: {exc}",
        ) from exc
    if len(content) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"the attachment is larger than SABLE_MAX_UPLOAD_BYTES ({limit} bytes)",
        )
    return payload, Attachment(payload.file.name, content)


def _json_kind(value: object) -> str:
    if value is None:
        return "null"
    return {list: "an array", str: "a string", bool: "a boolean"}.get(
        type(value), "a number"
    )


def _validated(build) -> NotifyRequest:
    """Run ``build`` and turn a pydantic failure into a readable 422.

    Only the field names and what is wrong with them are reported: not the
    offending input, not pydantic's URL, not a traceback.
    """
    try:
        return build()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'body'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False, include_context=False)
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid request: {problems}"
        ) from exc


def _check_bearer(header: str, expected: str) -> None:
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not _same_secret(token.strip(), expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "a valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _relay_file(
    request: Request, payload: NotifyRequest, room: str, attachment: Attachment
) -> dict[str, object]:
    """Upload the attachment and share it into the conversation."""
    talk_bot = request.app.state.bot
    files = talk_bot.files()
    try:
        shared = await files.send_file(
            room,
            attachment.name,
            attachment.data,
            caption=payload.message,
            silent=payload.silent,
            reply_to=payload.reply_to,
        )
    except FilesError as exc:
        log.warning("attaching %s to %s failed: %s", attachment.name, room, exc)
        code = (
            status.HTTP_400_BAD_REQUEST
            if exc.status in {400, 403, 404}
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(code, str(exc)) from exc

    log.info(
        "attached %s (%d bytes) to %s%s",
        shared.name,
        shared.size,
        room,
        f" with a caption of {len(payload.message)} chars" if payload.message else "",
    )
    return {
        "ok": True,
        "room": room,
        "file": {"name": shared.name, "path": shared.path, "size": shared.size},
        "shareId": shared.share_id,
    }


def _log_task_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("handling an event failed", exc_info=exc)
