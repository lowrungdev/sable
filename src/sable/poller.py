"""Reading chat: long-poll every conversation the account is in.

Talk has no single feed of new messages across conversations, so sable keeps one
long poll open per conversation - ``GET /chat/{token}?lookIntoFuture=1`` - and
rescans the conversation list every SABLE_ROOM_REFRESH seconds to start polling
the ones it was added to and stop polling the ones it left.

A poll that finds nothing comes back 304 after SABLE_POLL_TIMEOUT seconds and is
simply asked again; the cursor is the id of the last message seen. A conversation
is first followed from its newest message, so whatever was said before sable
started is never replayed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import httpx

from .bot import Bot
from .config import TOKEN_RE
from .events import EventError, parse_message
from .talk import TalkError

log = logging.getLogger(__name__)

#: How many conversations are followed at once. Each long poll occupies a
#: request slot on the Nextcloud server for up to SABLE_POLL_TIMEOUT seconds, so
#: this is a courtesy to it as much as a limit of ours. The most recently active
#: ones win.
MAX_POLLED_ROOMS = 50

#: Talk's conversation type for the "Talk updates" changelog, which is read-only
#: and nothing anybody addresses a bot in.
CHANGELOG_ROOM = 4

#: Spawns an event handler detached from the poll loop, under the reply ceiling.
Spawn = Callable[[Awaitable[None]], None]


class Poller:
    """Follows the account's conversations and hands each new event to the bot."""

    def __init__(
        self,
        bot: Bot,
        spawn: Spawn,
        *,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        idle_gap: float = 0.5,
    ) -> None:
        self.bot = bot
        self.config = bot.config
        self._spawn = spawn
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        #: The least time one empty poll may take. A server or proxy that answers
        #: 304 at once would otherwise be asked again in a tight loop.
        self._idle_gap = idle_gap
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cursors: dict[str, int] = {}
        self._names: dict[str, str] = {}
        self._scanner: asyncio.Task[None] | None = None
        self._capped = False

    # -- lifecycle --------------------------------------------------------- #

    @property
    def following(self) -> list[str]:
        """Tokens of the conversations being polled."""
        return sorted(token for token, task in self._tasks.items() if not task.done())

    def start(self) -> None:
        if self._scanner is None:
            self._scanner = asyncio.create_task(self._scan_forever(), name="sable-rooms")

    async def stop(self) -> None:
        tasks = [*self._tasks.values()]
        if self._scanner is not None:
            tasks.append(self._scanner)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._scanner = None

    def _delay(self, failures: int) -> float:
        return min(self._backoff_base * 2 ** max(failures - 1, 0), self._backoff_max)

    # -- which conversations ------------------------------------------------ #

    async def _scan_forever(self) -> None:
        failures = 0
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except (TalkError, httpx.HTTPError) as exc:
                failures += 1
                # TalkClient has logged the call itself; this says what it costs.
                log.debug("could not list conversations (%s); trying again", exc)
                await asyncio.sleep(min(self._delay(failures), self.config.room_refresh))
                continue
            except Exception:
                failures += 1
                log.exception("scanning for conversations crashed; trying again")
                await asyncio.sleep(min(self._delay(failures), self.config.room_refresh))
                continue
            failures = 0
            await asyncio.sleep(self.config.room_refresh)

    async def scan(self) -> None:
        """Compare the conversation list with what is being polled."""
        rooms = await self.bot.talk.rooms()
        usable: list[dict] = []
        for room in rooms:
            token = str(room.get("token", ""))
            if not TOKEN_RE.match(token):
                # Goes into a URL path below; anything odd is not worth following.
                log.warning("ignoring a conversation with an unusable token %r", token)
            elif room.get("type") != CHANGELOG_ROOM:
                usable.append(room)

        usable.sort(key=lambda room: _int(room.get("lastActivity")), reverse=True)
        if len(usable) > MAX_POLLED_ROOMS:
            if not self._capped:
                log.warning(
                    "the account is in %d conversations; following only the %d most "
                    "recently active (each long poll holds a request open on "
                    "Nextcloud). Leave the rest, or use an account that is in fewer.",
                    len(usable),
                    MAX_POLLED_ROOMS,
                )
            self._capped = True
            usable = usable[:MAX_POLLED_ROOMS]
        else:
            self._capped = False

        wanted = {str(room["token"]): room for room in usable}
        for token in [t for t in self._tasks if t not in wanted]:
            task = self._tasks.pop(token)
            task.cancel()
            self._cursors.pop(token, None)
            log.info(
                "no longer in conversation %s (%r) - no further messages from it",
                token,
                self._names.pop(token, ""),
            )
        for token, room in wanted.items():
            name = str(room.get("displayName") or room.get("name") or "")
            self._names[token] = name
            task = self._tasks.get(token)
            if task is not None and not task.done():
                continue
            newest = _newest(room)
            if token not in self._cursors and newest is not None:
                # Without one (Talk sent no lastMessage), _follow asks for it.
                self._cursors[token] = newest
            log.info("following conversation %s (%r)", token, name)
            self._tasks[token] = asyncio.create_task(
                self._follow(token), name=f"sable-poll-{token}"
            )

    # -- one conversation --------------------------------------------------- #

    async def _follow(self, token: str) -> None:
        failures = 0
        while True:
            started = time.monotonic()
            try:
                if token not in self._cursors:
                    self._cursors[token] = await self.bot.talk.latest_message_id(token)
                messages, cursor = await self.bot.talk.poll(
                    token, self._cursors[token], timeout=self.config.poll_timeout
                )
            except asyncio.CancelledError:
                raise
            except TalkError as exc:
                if exc.status in (403, 404):
                    # Removed from the conversation, or it was deleted, between two
                    # scans. The next scan settles whether it is still ours.
                    log.info(
                        "conversation %s is no longer readable (HTTP %s); "
                        "stopped following it",
                        token,
                        exc.status,
                    )
                    self._cursors.pop(token, None)
                    return
                failures += 1
                await asyncio.sleep(self._delay(failures))
                continue
            except httpx.HTTPError:
                failures += 1
                await asyncio.sleep(self._delay(failures))
                continue

            failures = 0
            self._cursors[token] = cursor
            for message in messages:
                self._dispatch(token, message)
            if not messages:
                spare = self._idle_gap - (time.monotonic() - started)
                if spare > 0:
                    await asyncio.sleep(spare)

    def _dispatch(self, token: str, message: dict) -> None:
        try:
            event = parse_message(
                message, room_token=token, room_name=self._names.get(token, "")
            )
        except EventError as exc:
            log.warning("unparseable message in %s: %s", token, exc)
            return
        if event is None:
            return
        log.debug(
            "received %s from %s in %s (message %s)",
            event.type,
            event.actor.id or "?",
            token,
            event.message_id or "-",
        )
        self._spawn(self.bot.handle(event))


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _newest(room: dict) -> int | None:
    """The id of the newest message the room list reports, None if it has none."""
    last = room.get("lastMessage")
    return _int(last.get("id")) if isinstance(last, dict) and last.get("id") else None
