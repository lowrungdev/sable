"""Request body caps, enforced before any parser sees the body.

Starlette's ``request.form()`` spools a multipart upload to a temporary file and
``request.json()`` buffers the whole body, both before a handler gets to look at
how much arrived. A cap checked in the handler is therefore a cap checked after
the cost was paid, and a reverse proxy's limit is not something sable can count
on being there. So the app counts for itself, here, as a pure ASGI middleware
(not ``BaseHTTPMiddleware``, which would buffer the body it is meant to limit):

* a ``Content-Length`` over the cap is answered 413 without reading a byte;
* otherwise the bytes are counted as the app reads them, and the read that takes
  the total past the cap raises :class:`BodyTooLarge`, which is turned into the
  same 413. That covers chunked bodies, a missing ``Content-Length`` and one that
  lies - nothing past the cap reaches a JSON parser or a temporary file.

The 413 comes before authentication, on purpose: it reveals nothing, and refusing
early is the point.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)


class BodyTooLarge(Exception):
    """Raised from ``receive()`` when a streamed body passes its cap.

    Deliberately an ``Exception`` and not a ``ValueError`` or an ``HTTPException``:
    nothing between the handler and this middleware should catch it and carry on.
    """


class BodyLimitMiddleware:
    """Refuse request bodies over a per-route cap.

    ``cap_for(method, path)`` returns the cap in bytes for a request, or ``None``
    for no cap at all.
    """

    def __init__(self, app: ASGIApp, cap_for: Callable[[str, str], int | None]) -> None:
        self.app = app
        self.cap_for = cap_for

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cap = self.cap_for(scope["method"], scope["path"])
        if cap is None:
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > cap:
            log.warning(
                "refused %s %s: Content-Length %d is over the %d byte cap",
                scope["method"], scope["path"], declared, cap,
            )
            await _too_large(send, cap)
            return

        seen = 0
        started = False

        async def counted_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > cap:
                    raise BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracking_send)
        except BodyTooLarge:
            log.warning(
                "refused %s %s: the body streamed past the %d byte cap",
                scope["method"], scope["path"], cap,
            )
            if started:
                # Cannot happen for a handler that reads before it answers; if it
                # ever does, the response is already on the wire. Drop the
                # connection rather than pretend.
                raise
            await _too_large(send, cap)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                length = int(value)
            except ValueError:
                return None
            return length if length >= 0 else None
    return None


async def _too_large(send: Send, cap: int) -> None:
    body = json.dumps({"detail": f"the request body is larger than {cap} bytes"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # The body was not (all) read, so this connection is not reusable.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
