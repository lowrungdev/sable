"""The HTTP surface: the Talk webhook, two ways in for alerts, and a health check.

Routes
------
``POST /webhook``     Nextcloud Talk posts events here. Signature-verified and
                      refused if its random has been seen before, then handled in
                      the background - under a ceiling on how many of those run at
                      once - so we answer well inside Talk's request timeout.
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
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
# Starlette's own class: request.form() yields these, and FastAPI's UploadFile is a
# subclass, so checking against the base accepts both.
from starlette.datastructures import UploadFile

from . import __version__
from .bot import Bot
from .config import TOKEN_HINT, TOKEN_RE, Config
from .events import EventError, parse_event
from .files import FilesError
from .hooks import render, render_with_template
from .signing import HEADER_BACKEND, HEADER_RANDOM, HEADER_SIGNATURE, verify_any
from .talk import TalkError

log = logging.getLogger(__name__)

#: How long shutdown waits for in-flight replies to finish.
DRAIN_TIMEOUT = 30.0

#: How many recently accepted ``X-Nextcloud-Talk-Random`` values to remember, so
#: a captured webhook cannot simply be sent again. Talk mints 32 bytes per
#: request, so collisions between genuine ones are not a thing worth planning
#: for; the size only decides how far back a replay has to reach to work.
SEEN_RANDOMS = 4096

#: Guards GET /healthz when SABLE_HEALTH_TOKEN is set. A header rather than a
#: query parameter, so the value stays out of proxy and access logs.
HEADER_HEALTH_TOKEN = "X-Health-Token"


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


def create_app(config: Config | None = None, bot: Bot | None = None) -> FastAPI:
    """Build the ASGI app. Pass ``config``/``bot`` in tests; otherwise read the env."""
    config = config or (bot.config if bot else Config.from_env())
    tasks: set[asyncio.Task[None]] = set()
    #: The ceiling on concurrent replies, built in the lifespan below: a
    #: Semaphore binds to the loop it was created on, and create_app runs before
    #: there is one (uvicorn calls it as a factory, tests build the app and drive
    #: it later). None means no ceiling at all.
    slots: asyncio.Semaphore | None = None
    #: Randoms from webhooks that verified, newest last, with a set beside the
    #: deque for the lookup - the pairing Bot._seen uses for the same job.
    randoms: deque[str] = deque(maxlen=SEEN_RANDOMS)
    random_set: set[str] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal slots
        app.state.bot = bot or Bot(config)
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
        log.info("  webhook URL:    POST /webhook  (give this to occ talk:bot:install)")
        log.info(
            "  nextcloud:      %s",
            config.nextcloud_url or "(taken from each signed webhook)",
        )
        log.info(
            "  bot name:       %r   command prefix: %r",
            config.bot_name,
            config.command_prefix,
        )
        log.info(
            "  model:          %s",
            f"{config.llm.model} at {config.llm.base_url}" if config.llm.enabled else "disabled",
        )
        log.info("  concurrency:    %s", concurrency_summary(config))
        log.info("  ask reaction:   %s", ask_reaction_summary(config))
        log.info("  ask rooms:      %s", ask_rooms_summary(config))
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
            "  attachments:    %s",
            f"as {config.nextcloud_user} into {config.upload_path}, "
            f"up to {megabytes(config.max_upload_bytes)}"
            if config.uploads_enabled
            else "disabled (set SABLE_NEXTCLOUD_USER and SABLE_NEXTCLOUD_PASSWORD)",
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
        log.info("  backend pin:    %s", backend_pin_summary(config))
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

        if config.startup_check:
            await app.state.bot.check_nextcloud()

        log.info("sable %s ready", __version__)
        try:
            yield
        finally:
            log.info("sable %s stopping", __version__)
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
        description="A Nextcloud Talk bot.",
        lifespan=lifespan,
        # None removes the route entirely rather than hiding it. The schema
        # describes every endpoint and body shape to whoever can reach the
        # service, and the webhook has to be reachable, so this is off unless
        # asked for.
        docs_url="/docs" if config.api_docs else None,
        redoc_url="/redoc" if config.api_docs else None,
        openapi_url="/openapi.json" if config.api_docs else None,
    )

    async def under_the_ceiling(coro) -> None:
        """Wait for a free slot, then run the handler.

        The waiting happens here, inside the background task, and never in the
        webhook handler: Talk gives up on us long before a model call comes back,
        so answering 200 cannot be made to depend on a slot. What the ceiling
        delays is the work, not the answer.
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
        """Run a handler detached from the request, keeping a strong reference."""
        task = asyncio.create_task(under_the_ceiling(coro))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(_log_task_failure)

    def replayed(random: str) -> bool:
        """Remember this webhook's random, and say whether it has been seen before.

        Only ever called *after* the signature verifies. The other order would let
        anybody who can reach the port fill this cache with randoms of their own
        invention and have the genuine webhooks carrying them refused.

        What it does not do: the cache lives in this process, so a restart forgets
        every random it held and a patient replay is accepted again. Talk sends no
        timestamp either, so there is no age to check and no window to enforce -
        a random is remembered until SEEN_RANDOMS newer ones have pushed it out.
        This raises the cost of replaying a captured request; keeping the request
        from being captured is still TLS's job, and nothing here substitutes for
        it.
        """
        nonlocal random_set
        if random in random_set:
            return True
        randoms.append(random)
        random_set.add(random)
        if len(random_set) > len(randoms):
            # A deque eviction dropped one; rebuild the membership set.
            random_set = set(randoms)
        return False

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
        if config.health_token and not hmac.compare_digest(
            health_token.strip(), config.health_token
        ):
            log.warning("rejected a health check with a bad or missing token")
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                f"a valid {HEADER_HEALTH_TOKEN} header is required",
            )
        return {
            "status": "ok",
            "version": __version__,
            "bot": config.bot_name,
            "llm": config.llm.model or None,
            "notify": config.notify_enabled,
            "nextcloud": current_bot(request).nextcloud.up,
        }

    @app.post("/webhook", status_code=status.HTTP_200_OK, tags=["talk"])
    async def webhook(
        request: Request,
        signature: Annotated[str, Header(alias=HEADER_SIGNATURE)] = "",
        random: Annotated[str, Header(alias=HEADER_RANDOM)] = "",
        backend: Annotated[str, Header(alias=HEADER_BACKEND)] = "",
    ) -> dict[str, str]:
        body = await request.body()

        # Any of the inbound secrets: one normally, two while SABLE_BOT_SECRET is
        # being rotated and events signed with the old one are still arriving.
        if not verify_any(random, signature, body, config.inbound_secrets):
            log.warning("rejected a webhook with a bad signature from %s", backend or "?")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

        # Second, and never first: see replayed(). A 401 rather than something
        # more descriptive, because whoever sent this either already had a
        # reply to it or is replaying somebody else's request.
        if replayed(random):
            log.warning(
                "rejected a webhook reusing random %s... from %s; it has been "
                "delivered already",
                random[:8],
                backend or "?",
            )
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "this webhook was already delivered"
            )

        backend = backend.rstrip("/")
        if config.pin_backend and backend != config.nextcloud_url:
            log.error(
                "rejected a webhook claiming backend %r; expected %r",
                backend,
                config.nextcloud_url,
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "unexpected backend")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid JSON: {exc}") from exc

        try:
            event = parse_event(payload, backend=backend)
        except EventError as exc:
            log.warning("unparseable event: %s", exc)
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        # The token goes into the path of every call we make back - talk.py builds
        # /bot/{token}/message - and httpx resolves ``..`` segments before sending,
        # so a token that is not one could move the request off the bot API's own
        # base path. Getting a webhook this far takes the bot secret or a
        # compromised Nextcloud, so this is depth rather than the first line.
        #
        # Checked here and not in parse_event: the handler can say which token and
        # why, where a refusal inside the parser reads as "unparseable event",
        # which is a different and misleading thing to put in front of an operator.
        if not TOKEN_RE.match(event.room_token):
            log.error(
                "refusing a webhook for conversation %r: %s",
                event.room_token,
                TOKEN_HINT,
            )
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{event.room_token!r} is not a conversation token: {TOKEN_HINT}",
            )

        log.debug(
            "accepted %s from %s in %s (message %s)",
            event.type,
            event.actor.id or "?",
            event.room_token,
            event.message_id or "-",
        )
        # Answer now, work later: a model call can take longer than Talk waits.
        spawn(current_bot(request).handle(event))
        return {"status": "accepted"}

    @app.post("/notify", status_code=status.HTTP_201_CREATED, tags=["alerting"])
    async def notify(
        request: Request,
        authorization: Annotated[str, Header()] = "",
    ) -> dict[str, object]:
        """Relay a message, optionally with a file, into a conversation.

        One URL, one call, three accepted shapes: JSON as before, JSON with a
        base64 ``file``, or multipart/form-data with an uploaded ``file``.
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
        if not hmac.compare_digest(supplied, config.hook_token(name)):
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

        try:
            payload = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            # Not everything sends JSON. Text is better than a rejection.
            payload = body.decode("utf-8", "replace")

        template = config.hook_templates.get(name.strip().lower(), "")
        if template:
            message, missing = render_with_template(template, payload)
            if missing:
                log.warning(
                    "hook %r: its format string asks for %s, which the payload "
                    "does not have",
                    name,
                    ", ".join(sorted(set(missing))),
                )
        else:
            message = render(payload)

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


def backend_pin_summary(config: Config) -> str:
    """The backend pin line in the startup block.

    Spelled out rather than "off", because off is the interesting state: the
    backend header sits outside the signature, so with nothing to pin against, a
    replayed webhook chooses where the replies to it go.
    """
    if config.pin_backend:
        return f"on, replies only to {config.nextcloud_url}"
    reason = (
        "no SABLE_NEXTCLOUD_URL"
        if not config.nextcloud_url
        else "SABLE_PIN_BACKEND is off"
    )
    return (
        f"OFF ({reason}) - the unsigned backend header on each webhook decides "
        f"where replies to it go"
    )


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


def ask_rooms_summary(config: Config) -> str:
    """Which conversations have their messages remembered, and so which ones the
    reaction can answer in. Worth a line of its own: this is the setting that
    decides how much chat content sits in memory."""
    if not config.ask_reaction:
        return "(none - the reaction is disabled, so nothing is cached)"
    if not config.ask_rooms:
        return "every conversation the bot is in"
    return ", ".join(config.ask_rooms)


def concurrency_summary(config: Config) -> str:
    """The concurrency line in the startup block.

    Spelled out in both states, because the ceiling is what stands between a
    redelivery storm and as many open model calls as there were events, each
    holding SABLE_LLM_TIMEOUT open.
    """
    if not config.max_concurrent_replies:
        return (
            "no ceiling (SABLE_MAX_CONCURRENT_REPLIES=0) - every event that "
            "arrives starts a model call of its own"
        )
    return f"up to {config.max_concurrent_replies} replies at once, the rest queued"


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
        form = await request.form()
        upload = form.get("file")
        if upload is not None and not isinstance(upload, UploadFile):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "'file' must be an uploaded file"
            )
        payload = NotifyRequest(
            room=str(form.get("room") or ""),
            message=str(form.get("message") or ""),
            silent=_form_bool(form.get("silent")),
            reply_to=_form_int(form.get("replyTo") or form.get("reply_to") or 0),
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
    payload = NotifyRequest.model_validate(body)
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


def _check_bearer(header: str, expected: str) -> None:
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), expected):
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
    config = talk_bot.config
    if not config.uploads_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "file attachments are not configured: set SABLE_NEXTCLOUD_USER and "
            "SABLE_NEXTCLOUD_PASSWORD to enable them",
        )

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
