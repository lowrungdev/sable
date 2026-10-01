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
from .talk import POLL_SLACK, TalkError

log = logging.getLogger(__name__)

#: How many conversations are followed at once. Each long poll occupies a
#: request slot on the Nextcloud server for up to SABLE_POLL_TIMEOUT seconds, so
#: this is a courtesy to it as much as a limit of ours. The most recently active
#: ones win.
MAX_POLLED_ROOMS = 50

#: Conversation types nobody addresses a bot in, each of which would cost a held
#: request for nothing: the read-only "Talk updates" changelog (4), a former
#: one-to-one whose other person is gone (5), and the account's own note to self (6).
SKIPPED_ROOM_TYPES = frozenset({4, 5, 6})

#: The only conversation types sable will leave: group (2) and public (3). A
#: one-to-one is a person, not a room, and the rest are not ours to leave.
LEAVABLE_ROOM_TYPES = frozenset({2, 3})

#: Most conversations left in one scan, so a long list of unwanted rooms is
#: worked through over several scans rather than all at once.
MAX_LEAVES_PER_SCAN = 5

#: The object type of Talk's "Let's get started!" sample conversation.
SAMPLE_OBJECT_TYPE = "sample"

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
        #: Conversations Talk would not let us leave (we moderate them alone), so
        #: they are said once and not asked about every scan.
        self._stuck: set[str] = set()
        #: Allowed tokens the last scan did not find, so the warning is said once.
        self._absent: list[str] = []
        #: Whether leaving is currently suspended, so the warning is said once.
        self._leave_suspended = False

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
        unlisted: list[dict] = []
        for room in rooms:
            token = str(room.get("token", ""))
            if not TOKEN_RE.match(token):
                # Goes into a URL path below; anything odd is not worth following.
                log.warning("ignoring a conversation with an unusable token %r", token)
            elif _skipped(room):
                log.debug("not following %s: nobody talks to a bot there", token)
            elif not self.config.room_allowed(token):
                log.debug("not following %s: not in SABLE_ALLOWED_ROOMS", token)
                unlisted.append(room)
            else:
                usable.append(room)

        absent = sorted(
            set(self.config.allowed_rooms) - {str(room.get("token", "")) for room in rooms}
        )
        if absent != self._absent:
            self._absent = absent
            if absent:
                # A mistyped token is silent otherwise - and with
                # SABLE_LEAVE_UNLISTED_ROOMS on it means every other group is left.
                log.warning(
                    "SABLE_ALLOWED_ROOMS lists %s, which the account is not in "
                    "(or that is not a conversation token); nothing is followed there",
                    ", ".join(absent),
                )

        await self._leave_unlisted(unlisted, rooms)
        self._stuck &= {str(room["token"]) for room in unlisted}

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

    async def _leave_unlisted(self, unlisted: list[dict], rooms: list[dict]) -> None:
        """Leave the group and public conversations nobody listed, if asked to.

        ``rooms`` is the whole fetched list; nothing is left unless at least one
        allowed conversation is in it.

        Never raises: a conversation that will not let us go is said once and
        left alone, and a failure of any other kind waits for the next scan.
        """
        config = self.config
        if not (config.leave_unlisted_rooms and config.allowed_rooms):
            return
        if not unlisted:
            return
        if not any(config.room_allowed(str(room.get("token", ""))) for room in rooms):
            # A mistyped SABLE_ALLOWED_ROOMS (or an account removed from the room
            # it was meant to keep) would otherwise make every other group look
            # unwanted. With no allowed room in sight, whatever is left is not
            # known to be unwanted.
            if not self._leave_suspended:
                log.warning(
                    "leaving unlisted conversations is suspended: none of the "
                    "conversations in SABLE_ALLOWED_ROOMS is in the account's "
                    "conversation list, so nothing can be told apart as unwanted "
                    "(check the tokens). Nothing is left until one is visible."
                )
            self._leave_suspended = True
            return
        self._leave_suspended = False
        keep = config.destination_rooms
        left = 0
        for room in unlisted:
            token = str(room["token"])
            if room.get("type") not in LEAVABLE_ROOM_TYPES or token in keep:
                continue
            if token in self._stuck:
                continue
            if left >= MAX_LEAVES_PER_SCAN:
                log.info("more conversations to leave; the rest wait for the next scan")
                break
            name = str(room.get("displayName") or room.get("name") or "")
            try:
                await self.bot.talk.leave(token)
            except TalkError as exc:
                if exc.status in (400, 403):
                    self._stuck.add(token)
                    log.warning(
                        "cannot leave conversation %s (%r): HTTP %s - probably the "
                        "account is its only moderator or owner. Remove it from "
                        "there, or list the token in SABLE_ALLOWED_ROOMS.",
                        token,
                        name,
                        exc.status,
                    )
                else:
                    log.warning("could not leave conversation %s: %s", token, exc)
                continue
            except httpx.HTTPError as exc:
                log.warning("could not leave conversation %s: %s", token, exc)
                continue
            left += 1
            log.info(
                "left conversation %s (%r): not in SABLE_ALLOWED_ROOMS and not a "
                "/notify or /hook destination",
                token,
                name,
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
                if exc.status in (403, 404, 412):
                    # Removed from the conversation, it was deleted, or a lobby shut us out (412), between two
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
            except httpx.ReadTimeout:
                failures += 1
                # Said once per streak: a server that is short of workers does it on
                # every poll, and the first line is the one that helps.
                log.log(
                    logging.WARNING if failures == 1 else logging.DEBUG,
                    "Nextcloud held the poll of conversation %s (%r) past %ds without "
                    "answering; asking again. If this repeats, its PHP-FPM pool is "
                    "probably too small for this many conversations "
                    "(see docs/deployment.md).",
                    token,
                    self._names.get(token, ""),
                    self.config.poll_timeout + POLL_SLACK,
                )
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
        if not self.bot.would_handle(event):
            # Chatter, our own replies, other bots: nothing for handle to do, so
            # it must not take a reply slot or push a real trigger out of the queue.
            return
        self._spawn(self.bot.handle(event))


def _skipped(room: dict) -> bool:
    """True for a conversation not worth holding a request open for."""
    return room.get("type") in SKIPPED_ROOM_TYPES or room.get("objectType") == SAMPLE_OBJECT_TYPE


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _newest(room: dict) -> int | None:
    """The id of the newest message the room list reports, None if it has none."""
    last = room.get("lastMessage")
    return _int(last.get("id")) if isinstance(last, dict) and last.get("id") else None
