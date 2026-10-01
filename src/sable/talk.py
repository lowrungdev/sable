"""Client for the Nextcloud Talk chat API, as an ordinary user account.

sable logs in like any other Nextcloud user: HTTP Basic auth with an app
password, and the ``OCS-APIRequest`` header OCS endpoints insist on. Everything
goes through the regular chat API under ``/ocs/v2.php/apps/spreed/api/v1`` (conversations
are listed under ``api/v4``) -
listing conversations, long-polling them for new messages, posting, reacting.
"""

from __future__ import annotations

import logging

import httpx

from .config import MAX_POLL_TIMEOUT
from .state import ConnectionState

log = logging.getLogger(__name__)

API_BASE = "/ocs/v2.php/apps/spreed/api/v1"

#: Conversations are listed by v4; v1 answers /room with a 404 (OCS status 998).
ROOMS_API_BASE = "/ocs/v2.php/apps/spreed/api/v4"

#: Not under the Talk base: who the credentials belong to.
USER_ENDPOINT = "/ocs/v2.php/cloud/user"

#: Talk rejects anything longer than this (HTTP 413).
MESSAGE_LIMIT = 32000


#: Extra time the HTTP client waits beyond the poll timeout the server was given.
POLL_SLACK = 15.0

#: Most messages one poll may return.
POLL_LIMIT = 100


class TalkError(RuntimeError):
    """A Talk API call failed."""

    def __init__(self, status: int, body: str, endpoint: str) -> None:
        super().__init__(f"{endpoint} returned HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body
        self.endpoint = endpoint


def _ocs_data(response: httpx.Response) -> object:
    """The ``ocs.data`` member of an OCS response, or None if there is none."""
    try:
        return response.json()["ocs"]["data"]
    except (ValueError, KeyError, TypeError):
        return None


class TalkClient:
    """Reads and posts chat as one Nextcloud user."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_message_chars: int = 30000,
        timeout: float = 30.0,
        state: ConnectionState | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self._auth = httpx.BasicAuth(user, password)
        #: Shared across clients so the up/down transitions are logged once.
        self._state = state
        self._timeout = timeout
        self._max_chars = min(max_message_chars, MESSAGE_LIMIT)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- plumbing ---------------------------------------------------------- #

    async def _send(
        self,
        method: str,
        url: str,
        label: str,
        *,
        params: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
        timeout: float | None = None,
        ok: frozenset[int] = frozenset(),
        long_poll: bool = False,
    ) -> httpx.Response:
        """One call. Raises TalkError on a status of 400 or more, except any in ``ok``."""
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=payload,
                auth=self._auth,
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                timeout=timeout if timeout is not None else self._timeout,
            )
        except httpx.HTTPError as exc:
            if long_poll and isinstance(exc, httpx.ReadTimeout):
                # Connected, then held past the time we allowed: Nextcloud is slow
                # (usually too few PHP workers), not unreachable. The caller says so.
                raise
            # Transport level: Nextcloud could not be reached at all.
            if self._state is not None:
                self._state.record_failure(exc)
            raise
        # It answered, so it is reachable - even if the answer is an error.
        if self._state is not None:
            self._state.record_success()
        if response.status_code >= 400 and response.status_code not in ok:
            log.warning(
                "Nextcloud rejected %s %s with HTTP %s: %s",
                method,
                label,
                response.status_code,
                response.text[:200],
            )
            raise TalkError(response.status_code, response.text, f"{method} {label}")
        return response

    async def _talk(
        self, method: str, path: str, **kwargs: object
    ) -> httpx.Response:
        return await self._send(
            method, f"{self.base_url}{API_BASE}{path}", path, **kwargs  # type: ignore[arg-type]
        )

    # -- who we are -------------------------------------------------------- #

    async def whoami(self) -> tuple[str, str]:
        """The user id and display name these credentials belong to.

        Doubles as the credentials check: a wrong password is a 401 here.
        """
        response = await self._send(
            "GET", f"{self.base_url}{USER_ENDPOINT}", USER_ENDPOINT
        )
        data = _ocs_data(response)
        if not isinstance(data, dict) or not data.get("id"):
            raise TalkError(
                response.status_code,
                "the answer names no user - is this really a Nextcloud?",
                f"GET {USER_ENDPOINT}",
            )
        return str(data["id"]), str(data.get("displayname") or data.get("display-name") or "")

    # -- receiving --------------------------------------------------------- #

    async def rooms(self) -> list[dict]:
        """Every conversation this account is in."""
        # noStatusUpdate: listing conversations must not flip the account online.
        response = await self._send(
            "GET",
            f"{self.base_url}{ROOMS_API_BASE}/room",
            "/room",
            params={"noStatusUpdate": 1},
        )
        data = _ocs_data(response)
        return [room for room in data if isinstance(room, dict)] if isinstance(data, list) else []

    async def latest_message_id(self, room_token: str) -> int:
        """The id of the newest message in a conversation, 0 if it has none."""
        response = await self._talk(
            "GET",
            f"/chat/{room_token}",
            params={
                "lookIntoFuture": 0,
                "limit": 1,
                "setReadMarker": 0,
                "noStatusUpdate": 1,
            },
            ok=frozenset({304}),
        )
        if response.status_code == 304:
            return 0
        messages = _ocs_data(response)
        if not isinstance(messages, list):
            return 0
        return max((_int(m.get("id")) for m in messages if isinstance(m, dict)), default=0)

    async def poll(
        self, room_token: str, after: int, *, timeout: int = 30
    ) -> tuple[list[dict], int]:
        """Wait for messages newer than ``after``; return them and the new cursor.

        Talk answers 304 when the timeout passes with nothing to say, which is
        the normal case and returns ``([], after)``.
        """
        timeout = max(1, min(int(timeout), MAX_POLL_TIMEOUT))
        response = await self._talk(
            "GET",
            f"/chat/{room_token}",
            params={
                "lookIntoFuture": 1,
                "lastKnownMessageId": after,
                "timeout": timeout,
                "limit": POLL_LIMIT,
                "setReadMarker": 0,
                "includeLastKnown": 0,
                "noStatusUpdate": 1,
            },
            timeout=timeout + POLL_SLACK,
            ok=frozenset({304}),
            long_poll=True,
        )
        if response.status_code == 304:
            return [], after
        data = _ocs_data(response)
        messages = [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []
        messages.sort(key=lambda m: _int(m.get("id")))
        cursor = max(
            [after, _int(response.headers.get("X-Chat-Last-Given"))]
            + [_int(m.get("id")) for m in messages]
        )
        return messages, cursor

    # -- sending ----------------------------------------------------------- #

    def truncate(self, message: str) -> str:
        """Clip a message to the configured limit, flagging that we did."""
        if len(message) <= self._max_chars:
            return message
        suffix = "\n\n_[truncated]_"
        return message[: self._max_chars - len(suffix)] + suffix

    async def send_message(
        self,
        room_token: str,
        message: str,
        *,
        reply_to: int = 0,
        silent: bool = False,
        reference_id: str = "",
    ) -> int:
        """Post a Markdown message. Returns the new message id (0 if unknown)."""
        message = self.truncate(message)
        if not message.strip():
            raise ValueError("refusing to send an empty message")
        payload: dict[str, object] = {"message": message, "silent": silent}
        if reply_to:
            payload["replyTo"] = reply_to
        if reference_id:
            payload["referenceId"] = reference_id
        response = await self._talk("POST", f"/chat/{room_token}", payload=payload)
        data = _ocs_data(response)
        new_id = _int(data.get("id")) if isinstance(data, dict) else 0
        log.debug(
            "posted %d chars to %s as message %s%s",
            len(message),
            room_token,
            new_id or "?",
            f" (reply to {reply_to})" if reply_to else "",
        )
        return new_id

    async def react(self, room_token: str, message_id: int, reaction: str) -> None:
        """Add a single-emoji reaction to a message."""
        await self._talk(
            "POST",
            f"/reaction/{room_token}/{message_id}",
            # Talk answers 200, not an error, when the reaction is already there.
            payload={"reaction": reaction},
        )

    async def unreact(self, room_token: str, message_id: int, reaction: str) -> None:
        """Remove a reaction we previously added."""
        await self._talk(
            "DELETE",
            f"/reaction/{room_token}/{message_id}",
            # The docs put the emoji in the body; the query copy covers a server
            # that reads DELETE parameters only from the URL.
            payload={"reaction": reaction},
            params={"reaction": reaction},
            ok=frozenset({404}),
        )

    async def try_react(self, room_token: str, message_id: int, reaction: str) -> bool:
        """React, swallowing failures - reactions are never load-bearing."""
        if not reaction or not message_id:
            return False
        try:
            await self.react(room_token, message_id, reaction)
            return True
        except (TalkError, httpx.HTTPError) as exc:
            log.debug("could not add reaction %s: %s", reaction, exc)
            return False

    async def try_unreact(self, room_token: str, message_id: int, reaction: str) -> None:
        if not reaction or not message_id:
            return
        try:
            await self.unreact(room_token, message_id, reaction)
        except (TalkError, httpx.HTTPError) as exc:
            log.debug("could not remove reaction %s: %s", reaction, exc)


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
