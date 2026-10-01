"""Uploading a file and sharing it into a conversation.

Attaching a file to a chat takes two steps, both as the same Nextcloud user
account sable chats as:

1. ``PUT`` the bytes into that user's own Files over WebDAV;
2. share the uploaded path into the conversation, ``shareType`` 10, which is what
   produces the chat message.

Used only on the /notify path, and only when a file is actually attached.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .state import ConnectionState

log = logging.getLogger(__name__)

#: Share type 10 is "share into a Talk conversation".
SHARE_TYPE_CONVERSATION = 10

SHARES_API = "/ocs/v2.php/apps/files_sharing/api/v1/shares"

#: What survives sanitising a caller-supplied filename.
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: Room for the timestamp and random prefix we add.
MAX_NAME_LENGTH = 96


class FilesError(RuntimeError):
    """Uploading or sharing failed.

    status carries Nextcloud's HTTP status when there was one, so the caller
    can tell "you asked for something impossible" from "the server is unwell".
    """

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def safe_filename(name: str) -> str:
    """Make a caller-supplied name safe to put in a URL path.

    Never trust the name on an upload: it arrives from whoever called /notify.
    Directory separators, traversal and control characters all have to go, and
    the result is prefixed to be unique so one alert cannot overwrite another.
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = SAFE_NAME.sub("-", base).strip("-.") or "attachment"
    if len(cleaned) > MAX_NAME_LENGTH:
        stem, dot, suffix = cleaned.rpartition(".")
        if dot and len(suffix) <= 12:
            cleaned = stem[: MAX_NAME_LENGTH - len(suffix) - 1] + "." + suffix
        else:
            cleaned = cleaned[:MAX_NAME_LENGTH]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}-{cleaned}"


@dataclass(frozen=True)
class SharedFile:
    """What a successful upload-and-share produced."""

    name: str
    path: str
    size: int
    share_id: int


class FilesClient:
    """WebDAV upload and conversation share, authenticated as a user."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        upload_path: str = "/sable",
        client: httpx.AsyncClient | None = None,
        state: ConnectionState | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.upload_path = "/" + upload_path.strip("/")
        self._auth = (user, password)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._state = state
        self._timeout = timeout
        self._checked_dir = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- plumbing ---------------------------------------------------------- #

    def _dav_url(self, path: str) -> str:
        quoted = quote(path.lstrip("/"), safe="/")
        return f"{self.base_url}/remote.php/dav/files/{quote(self.user)}/{quoted}"

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = await self._client.request(
                method, url, auth=self._auth, timeout=self._timeout, **kwargs
            )
        except httpx.HTTPError as exc:
            if self._state is not None:
                self._state.record_failure(exc)
            raise FilesError(f"could not reach Nextcloud: {exc}") from exc
        if self._state is not None:
            self._state.record_success()
        return response

    # -- steps ------------------------------------------------------------- #

    async def ensure_folder(self) -> None:
        """Create the upload folder, tolerating one that already exists."""
        if self._checked_dir:
            return
        response = await self._request("MKCOL", self._dav_url(self.upload_path))
        # 201 created, 405 already there. Anything else is a real problem.
        if response.status_code not in (201, 405):
            raise FilesError(
                f"could not create {self.upload_path} as {self.user}: "
                f"HTTP {response.status_code} {response.text[:200]}",
                response.status_code,
            )
        if response.status_code == 201:
            log.info("created %s in %s's files", self.upload_path, self.user)
        self._checked_dir = True

    async def upload(self, name: str, content: bytes) -> str:
        """PUT the bytes and return the path inside the user's root."""
        await self.ensure_folder()
        path = f"{self.upload_path}/{safe_filename(name)}"
        response = await self._request(
            "PUT",
            self._dav_url(path),
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        if response.status_code not in (200, 201, 204):
            raise FilesError(
                f"uploading {path} failed: HTTP {response.status_code} "
                f"{response.text[:200]}",
                response.status_code,
            )
        log.info("uploaded %s (%d bytes) as %s", path, len(content), self.user)
        return path

    async def delete(self, path: str) -> None:
        """Remove an uploaded file, used to clean up after a failed share."""
        try:
            response = await self._request("DELETE", self._dav_url(path))
        except FilesError as exc:
            log.warning("could not clean up %s: %s", path, exc)
            return
        if response.status_code not in (200, 204, 404):
            log.warning(
                "could not clean up %s: HTTP %s", path, response.status_code
            )
        else:
            log.info("cleaned up %s after a failed share", path)

    async def share(
        self,
        path: str,
        room_token: str,
        *,
        caption: str = "",
        silent: bool = False,
        reply_to: int = 0,
    ) -> int:
        """Share an uploaded path into a conversation. Returns the share id.

        The caption rides along in ``talkMetaData``, so the file and its message
        are one chat message rather than two.
        """
        meta: dict[str, object] = {"messageType": "comment"}
        if caption:
            meta["caption"] = caption
        if silent:
            meta["silent"] = True
        if reply_to:
            meta["replyTo"] = reply_to

        response = await self._request(
            "POST",
            f"{self.base_url}{SHARES_API}",
            data={
                "shareType": SHARE_TYPE_CONVERSATION,
                "shareWith": room_token,
                "path": path,
                "talkMetaData": json.dumps(meta),
            },
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise FilesError(
                f"sharing {path} into {room_token} failed: HTTP "
                f"{response.status_code} {response.text[:300]}",
                response.status_code,
            )
        try:
            data = response.json()["ocs"]["data"]
            share_id = int(data.get("id", 0))
        except (ValueError, KeyError, TypeError, AttributeError):
            share_id = 0
        log.info("shared %s into %s (share %s)", path, room_token, share_id or "?")
        return share_id

    # -- the whole job ----------------------------------------------------- #

    async def send_file(
        self,
        room_token: str,
        name: str,
        content: bytes,
        *,
        caption: str = "",
        silent: bool = False,
        reply_to: int = 0,
    ) -> SharedFile:
        """Upload and share, cleaning up the upload if the share fails."""
        path = await self.upload(name, content)
        try:
            share_id = await self.share(
                path, room_token, caption=caption, silent=silent, reply_to=reply_to
            )
        except FilesError:
            # Otherwise the file sits in the account's Files, shared with nobody.
            await self.delete(path)
            raise
        return SharedFile(
            name=path.rsplit("/", 1)[-1], path=path, size=len(content), share_id=share_id
        )
