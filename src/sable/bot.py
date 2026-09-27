"""Deciding what to do with an event, and doing it."""

from __future__ import annotations

import logging
import re
from collections import deque

import httpx

from .commands import CommandError, Context, Registry, parse_argv, registry, split_command
from .config import Config
from .events import TalkEvent
from .history import History
from .llm import LLMClient, LLMError
from .talk import TalkClient, TalkError

log = logging.getLogger(__name__)

#: How many recently handled events to remember, so a redelivered webhook does
#: not produce a second reply.
SEEN_CACHE = 512


class Bot:
    def __init__(
        self,
        config: Config,
        *,
        http_client: httpx.AsyncClient | None = None,
        llm: LLMClient | None = None,
        history: History | None = None,
        command_registry: Registry | None = None,
    ) -> None:
        self.config = config
        self.registry = command_registry or registry
        self.history = history or History(config.history_turns, config.history_ttl)
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._llm = llm if llm is not None else LLMClient(config.llm, client=self._http)
        self._seen: deque[tuple[str, str, int]] = deque(maxlen=SEEN_CACHE)
        self._seen_set: set[tuple[str, str, int]] = set()
        self._mention_re = re.compile(
            rf"^@?{re.escape(config.bot_name)}\b[,:;]?\s*", re.IGNORECASE
        )
        self._mention_anywhere_re = re.compile(
            rf"(?<!\w)@{re.escape(config.bot_name)}\b", re.IGNORECASE
        )

    async def aclose(self) -> None:
        await self._llm.aclose()
        if self._owns_http:
            await self._http.aclose()

    # -- wiring ------------------------------------------------------------ #

    @property
    def llm_enabled(self) -> bool:
        return self.config.llm.enabled

    def talk(self, backend: str) -> TalkClient:
        """A client for the server that sent us this event."""
        base = self.config.nextcloud_url or backend
        if not base:
            raise ValueError("no Nextcloud URL: set SABLE_NEXTCLOUD_URL")
        return TalkClient(
            base,
            self.config.bot_secret,
            client=self._http,
            max_message_chars=self.config.max_message_chars,
        )

    def seen(self, event: TalkEvent) -> bool:
        """Record an event and report whether we already handled it."""
        key = (event.room_token, event.type, event.message_id)
        if event.message_id and key in self._seen_set:
            return True
        self._seen.append(key)
        self._seen_set.add(key)
        if len(self._seen_set) > len(self._seen):
            # A deque eviction dropped an entry; rebuild the membership set.
            self._seen_set = set(self._seen)
        return False

    # -- routing ----------------------------------------------------------- #

    def strip_mention(self, text: str) -> tuple[bool, str]:
        """Detect a mention of this bot and return the text without it."""
        match = self._mention_re.match(text)
        if match:
            return True, text[match.end() :].strip()
        if self._mention_anywhere_re.search(text):
            return True, text
        return False, text

    async def handle(self, event: TalkEvent) -> None:
        """Entry point for a verified webhook event."""
        if event.actor.is_bot:
            log.debug("ignoring %s from bot %s", event.type, event.actor.id)
            return
        if self.seen(event):
            log.info("ignoring redelivered %s #%s", event.type, event.message_id)
            return

        if event.type in {"Join", "Leave"}:
            log.info("bot %s conversation %s", event.type.lower(), event.room_token)
            return
        if not event.is_message:
            log.debug("no handler for %s events", event.type)
            return

        text = event.message.strip()
        if not text:
            return

        mentioned, remainder = self.strip_mention(text)
        command = split_command(remainder, self.config.command_prefix)

        if command is not None:
            await self._run_command(event, *command)
        elif mentioned or self.config.ai_room_allowed(event.room_token):
            await self._run_llm_reply(event, remainder)
        else:
            log.debug("message in %s was not for me", event.room_token)

    async def _run_command(self, event: TalkEvent, name: str, args: str) -> None:
        command = self.registry.get(name)
        if command is None:
            log.debug("unknown command %r in %s", name, event.room_token)
            if self.config.unknown_command_hint:
                await self._safe_reply(
                    event,
                    f"I have no `{name}` command. "
                    f"Try `{self.config.command_prefix}help`.",
                )
            return

        ctx = Context(self, event, command.name, args, parse_argv(args))
        log.info("running %s for %s in %s", command.name, event.actor.id, event.room_token)
        try:
            reply = await command.handler(ctx)
        except CommandError as exc:
            await self._safe_reply(event, str(exc))
            return
        except (LLMError, TalkError, httpx.HTTPError) as exc:
            log.warning("command %s failed: %s", command.name, exc)
            await self._report(event, str(exc))
            return
        except Exception:
            log.exception("command %s crashed", command.name)
            await self._report(event, f"the `{command.name}` command crashed")
            return
        if reply:
            await self._safe_reply(event, reply)

    async def _run_llm_reply(self, event: TalkEvent, prompt: str) -> None:
        if not self.llm_enabled:
            log.debug("LLM disabled; ignoring message %s", event.message_id)
            return
        try:
            reply = await self.answer_with_llm(event, prompt)
        except LLMError as exc:
            log.warning("completion failed: %s", exc)
            await self._report(event, str(exc))
            return
        if reply:
            await self._safe_reply(event, reply)

    # -- actions ----------------------------------------------------------- #

    async def answer_with_llm(self, event: TalkEvent, prompt: str) -> str:
        """Run a completion in the conversation's context and remember it."""
        if not self.llm_enabled:
            raise LLMError("no model is configured (set SABLE_LLM_MODEL)")
        prompt = prompt.strip()
        if not prompt:
            raise LLMError("nothing to answer")

        room = event.room_token
        turn = self._attributed(event, prompt)
        messages = [{"role": "system", "content": self._system_prompt(event)}]
        messages.extend(self.history.get(room))
        messages.append({"role": "user", "content": turn})

        reaction = self.config.thinking_reaction
        client = self.talk(event.backend)
        reacted = await client.try_react(room, event.message_id, reaction)
        try:
            answer = await self._llm.complete(messages)
        finally:
            if reacted:
                await client.try_unreact(room, event.message_id, reaction)

        self.history.add(room, "user", turn)
        self.history.add(room, "assistant", answer)
        return answer

    def _system_prompt(self, event: TalkEvent) -> str:
        prompt = self.config.llm.system_prompt
        if event.room_name:
            prompt += f'\nThe conversation is called "{event.room_name}".'
        return prompt

    def _attributed(self, event: TalkEvent, prompt: str) -> str:
        """Prefix the speaker's name, since rooms have more than one human."""
        name = event.actor.name or event.actor.id
        return f"{name}: {prompt}" if name else prompt

    async def reply(self, event: TalkEvent, message: str, *, silent: bool = False) -> int:
        """Post a message into the conversation the event came from."""
        client = self.talk(event.backend)
        reply_to = event.message_id if self.config.reply_as_reply else 0
        return await client.send_message(
            event.room_token, message, reply_to=reply_to, silent=silent
        )

    async def send(
        self, room_token: str, message: str, *, silent: bool = False, reply_to: int = 0
    ) -> int:
        """Post into a conversation without an incoming event (alerting path)."""
        client = self.talk("")
        return await client.send_message(
            room_token, message, silent=silent, reply_to=reply_to
        )

    async def _safe_reply(self, event: TalkEvent, message: str) -> None:
        """Reply, treating a failed post as a log line rather than a crash.

        Nextcloud being unreachable is not this handler's problem to solve, and
        an exception here would only surface as a stray traceback: the webhook
        request has long since been answered.
        """
        try:
            await self.reply(event, message)
        except (TalkError, httpx.HTTPError, ValueError) as exc:
            log.warning("could not post to %s: %s", event.room_token, exc)

    async def _report(self, event: TalkEvent, detail: str) -> None:
        if not self.config.report_errors:
            return
        await self._safe_reply(event, f"⚠️ Sorry - {detail}")
