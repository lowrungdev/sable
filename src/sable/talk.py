"""Client for the Nextcloud Talk bot API.

Base path is ``/ocs/v2.php/apps/spreed/api/v1``. Each call is signed with the
shared secret over a single endpoint-specific value - see :mod:`sable.signing`.
"""

from __future__ import annotations

import logging

import httpx

from .signing import HEADER_BOT_RANDOM, HEADER_BOT_SIGNATURE, sign

log = logging.getLogger(__name__)

API_BASE = "/ocs/v2.php/apps/spreed/api/v1"

#: Talk rejects anything longer than this (HTTP 413).
MESSAGE_LIMIT = 32000


class TalkError(RuntimeError):
    """A bot API call failed."""

    def __init__(self, status: int, body: str, endpoint: str) -> None:
        super().__init__(f"{endpoint} returned HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body
        self.endpoint = endpoint


class TalkClient:
    """Posts messages and reactions back into a conversation."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_message_chars: int = 30000,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._secret = secret
        self._max_chars = min(max_message_chars, MESSAGE_LIMIT)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self, signed_value: str) -> dict[str, str]:
        random, signature = sign(signed_value, self._secret)
        return {
            HEADER_BOT_RANDOM: random,
            HEADER_BOT_SIGNATURE: signature,
            "OCS-APIRequest": "true",
            "Accept": "application/json",
        }

    async def _request(
        self, method: str, path: str, signed_value: str, payload: dict
    ) -> httpx.Response:
        url = f"{self.base_url}{API_BASE}{path}"
        response = await self._client.request(
            method, url, json=payload, headers=self._headers(signed_value)
        )
        if response.status_code >= 400:
            raise TalkError(response.status_code, response.text, f"{method} {path}")
        return response

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
        # Signed over the message text only, not the serialised body.
        response = await self._request(
            "POST", f"/bot/{room_token}/message", message, payload
        )
        try:
            data = response.json()["ocs"]["data"]
        except (ValueError, KeyError, TypeError):
            return 0
        return int(data.get("id", 0)) if isinstance(data, dict) else 0

    async def react(self, room_token: str, message_id: int, reaction: str) -> None:
        """Add a single-emoji reaction to a message."""
        await self._request(
            "POST",
            f"/bot/{room_token}/reaction/{message_id}",
            reaction,
            {"reaction": reaction},
        )

    async def unreact(self, room_token: str, message_id: int, reaction: str) -> None:
        """Remove a reaction we previously added."""
        await self._request(
            "DELETE",
            f"/bot/{room_token}/reaction/{message_id}",
            reaction,
            {"reaction": reaction},
        )

    async def features(self, room_token: str) -> int:
        """The feature bitmask this bot has in the conversation."""
        response = await self._request(
            "POST", "/bot/ask-features", room_token, {"token": room_token}
        )
        try:
            return int(response.json()["ocs"]["data"]["features"])
        except (ValueError, KeyError, TypeError):
            return 0

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
