"""The HTTP surface: the Talk webhook, the alerting endpoint, and a health check.

Routes
------
``POST /webhook``  Nextcloud Talk posts events here. Signature-verified, then
                   handled in the background so we answer well inside Talk's
                   request timeout.
``POST /notify``   Inbound alerting: other systems post JSON here with a bearer
                   token and we relay it into a conversation.
``GET  /healthz``  Liveness probe.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
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
from .signing import HEADER_BACKEND, HEADER_RANDOM, HEADER_SIGNATURE, verify
from .talk import TalkError

log = logging.getLogger(__name__)

#: How long shutdown waits for in-flight replies to finish.
DRAIN_TIMEOUT = 30.0


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.bot = bot or Bot(config)

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
        log.info(
            "  ask reaction:   %s",
            config.ask_reaction or "disabled",
        )
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
        log.info("  backend pin:    %s", "on" if config.pin_backend else "off")
        log.info("  log level:      %s", config.log_level)

        if config.startup_check:
            await app.state.bot.check_nextcloud()

        log.info("sable %s ready", __version__)
        try:
            yield
        finally:
            log.info("sable %s stopping", __version__)
            if tasks:
                log.info("waiting for %d in-flight reply/replies", len(tasks))
                await asyncio.wait(tasks, timeout=DRAIN_TIMEOUT)
            if bot is None:
                await app.state.bot.aclose()
            log.info("sable %s stopped", __version__)

    app = FastAPI(
        title="sable",
        version=__version__,
        description="A Nextcloud Talk bot.",
        lifespan=lifespan,
    )

    def spawn(coro) -> None:
        """Run a handler detached from the request, keeping a strong reference."""
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(_log_task_failure)

    def current_bot(request: Request) -> Bot:
        """The Bot built during startup. Read from app.state rather than through
        Depends: a locally defined alias is not resolvable under postponed
        annotations, and FastAPI would take it for a query parameter."""
        return request.app.state.bot

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "bot": config.bot_name,
            "llm": config.llm.model or None,
            "notify": config.notify_enabled,
        }

    @app.post("/webhook", status_code=status.HTTP_200_OK, tags=["talk"])
    async def webhook(
        request: Request,
        signature: Annotated[str, Header(alias=HEADER_SIGNATURE)] = "",
        random: Annotated[str, Header(alias=HEADER_RANDOM)] = "",
        backend: Annotated[str, Header(alias=HEADER_BACKEND)] = "",
    ) -> dict[str, str]:
        body = await request.body()

        if not verify(random, signature, body, config.bot_secret):
            log.warning("rejected a webhook with a bad signature from %s", backend or "?")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

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
            # Same answer whether the hook is unknown or the feature is unused,
            # so probing cannot enumerate which hooks exist.
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
