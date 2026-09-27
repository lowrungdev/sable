"""Deciding what to do with an event, and doing it."""

from __future__ import annotations

import logging
import re
from collections import deque

import httpx

from .commands import CommandError, Context, Registry, parse_argv, registry, split_command
from .config import Config
from .events import TalkEvent
from .files import FilesClient
from .history import History, MessageCache
from .llm import LLMClient, LLMError
from .state import ConnectionState
from .talk import TalkClient, TalkError

log = logging.getLogger(__name__)

#: How many recently handled events to remember, so a redelivered webhook does
#: not produce a second reply.
SEEN_CACHE = 512

#: (conversation, event type, message id, actor, reaction) - see Bot.seen.
SeenKey = tuple[str, str, int, str, str]


def emoji_key(emoji: str) -> str:
    """Normalise an emoji for comparison.

    Clients differ over the variation selector, so the same reaction can arrive
    as U+2049 or U+2049 U+FE0F. Compare without it.
    """
    return emoji.strip().replace("️", "").replace("︎", "")


class Bot:
    def __init__(
        self,
        config: Config,
        *,
        http_client: httpx.AsyncClient | None = None,
        llm: LLMClient | None = None,
        history: History | None = None,
        messages: MessageCache | None = None,
        command_registry: Registry | None = None,
    ) -> None:
        self.config = config
        self.registry = command_registry or registry
        self.history = history or History(config.history_turns, config.history_ttl)
        self.messages = messages or MessageCache(config.message_cache, config.history_ttl)
        self._ask_key = emoji_key(config.ask_reaction)
        self.nextcloud = ConnectionState("Nextcloud")
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._llm = llm if llm is not None else LLMClient(config.llm, client=self._http)
        self._seen: deque[SeenKey] = deque(maxlen=SEEN_CACHE)
        self._seen_set: set[SeenKey] = set()
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

    @property
    def ask_enabled(self) -> bool:
        """Is the react-to-ask feature on? It needs both an emoji and a model."""
        return bool(self._ask_key) and self.llm_enabled

    def is_ask_reaction(self, reaction: str) -> bool:
        return bool(self._ask_key) and emoji_key(reaction) == self._ask_key

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
            state=self.nextcloud,
        )

    async def check_nextcloud(self) -> bool:
        """Probe Nextcloud's public status endpoint and log what came back.

        Only meaningful when SABLE_NEXTCLOUD_URL is configured; the webhook path
        otherwise learns the URL from each signed event. Failure is not fatal:
        Nextcloud may simply not be up yet, and the bot has nothing to do until a
        webhook arrives anyway.
        """
        if not self.config.nextcloud_url:
            log.info(
                "no SABLE_NEXTCLOUD_URL configured; the server URL will be taken "
                "from each signed webhook"
            )
            return False

        url = f"{self.config.nextcloud_url}/status.php"
        try:
            response = await self._http.get(url, timeout=10.0)
        except httpx.HTTPError as exc:
            self.nextcloud.record_failure(exc)
            log.warning(
                "could not reach Nextcloud at %s: %s. Replies will fail until this "
                "works - check the URL, DNS, and whether the certificate is trusted "
                "(see docs/deployment.md on internal CAs)",
                self.config.nextcloud_url,
                exc,
            )
            return False

        self.nextcloud.record_success()
        if response.status_code >= 400:
            log.warning(
                "Nextcloud at %s answered HTTP %s on status.php; it is reachable but "
                "may not be healthy",
                self.config.nextcloud_url,
                response.status_code,
            )
            return False
        try:
            status = response.json()
        except ValueError:
            log.warning(
                "%s answered, but not with JSON - is %s really a Nextcloud?",
                url,
                self.config.nextcloud_url,
            )
            return False

        log.info(
            "connected to %s %s at %s%s",
            status.get("productname") or "Nextcloud",
            status.get("versionstring") or "(unknown version)",
            self.config.nextcloud_url,
            " [MAINTENANCE MODE]" if status.get("maintenance") else "",
        )
        return True

    def files(self) -> FilesClient:
        """A client for the user account that uploads and shares attachments.

        Separate from :meth:`talk` on purpose: this one carries a credential
        that can read and write that user's files, so it is built only when an
        attachment is actually being sent.
        """
        if not self.config.uploads_enabled:
            raise ValueError(
                "file attachments are not configured (set SABLE_NEXTCLOUD_USER "
                "and SABLE_NEXTCLOUD_PASSWORD)"
            )
        return FilesClient(
            self.config.nextcloud_url,
            self.config.nextcloud_user,
            self.config.nextcloud_password,
            upload_path=self.config.upload_path,
            client=self._http,
            state=self.nextcloud,
        )

    def seen(self, event: TalkEvent) -> bool:
        """Record an event and report whether we already handled it.

        The actor and the reaction are part of the key, not just the message id:
        for a reaction event the id is the message being reacted *to*, so two
        people reacting to one message - or one person reacting twice with
        different emoji - would otherwise look like a redelivery of the first.
        For a chat message both are constant, so the id still decides.
        """
        key = (
            event.room_token,
            event.type,
            event.message_id,
            event.actor.id,
            event.reaction,
        )
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
        if self.config.is_ignored(event.actor.id, event.actor.name):
            # Before the message cache too: their words never reach the model,
            # not even by somebody else reacting to them.
            log.debug(
                "ignoring %s from %s - listed in SABLE_IGNORE_USERS",
                event.type,
                self._who(event),
            )
            return

        # Remember messages before anything else, the bot's own included, so a
        # reaction can name one later. Remembering is not acting on it, and a
        # reaction to one of our own answers is a reasonable follow-up.
        if self.ask_enabled and event.is_message:
            self.messages.add(
                event.room_token,
                event.message_id,
                event.actor.name or event.actor.id,
                event.message.strip(),
            )

        if event.actor.is_bot:
            log.debug("ignoring %s from bot %s", event.type, event.actor.id)
            return
        if self.seen(event):
            log.info("ignoring redelivered %s #%s", event.type, event.message_id)
            return

        # Gated on ask_enabled, not just the emoji: with no model configured the
        # cache is empty too, and "I do not have that message" would be a lie.
        if self.ask_enabled and event.type == "Like" and self.is_ask_reaction(event.reaction):
            await self._run_reaction_query(event)
            return

        if event.type == "Join":
            log.info(
                "added to conversation %s (%r) - now receiving its messages",
                event.room_token,
                event.room_name,
            )
            return
        if event.type == "Leave":
            log.info(
                "removed from conversation %s (%r) - no further messages from it",
                event.room_token,
                event.room_name,
            )
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
        elif mentioned or self.config.ai_room_allowed(event.room_token, event.room_name):
            await self._run_llm_reply(event, remainder)
        else:
            # Naming the conversation both ways: whichever you put in
            # SABLE_AI_ROOMS, this line shows you the value to use.
            log.debug(
                "message in %s (%r) was not for me - no prefix, no mention, and "
                "not an AI room",
                event.room_token,
                event.room_name,
            )

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
        log.info(
            "%s ran %s%s in %s",
            self._who(event),
            self.config.command_prefix,
            command.name,
            event.room_token,
        )
        log.debug("arguments: %r", args)
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

    async def _run_reaction_query(self, event: TalkEvent) -> None:
        """Answer the message somebody reacted to with the ask emoji.

        The event names the message by id only, so this depends on having seen it
        go past: a reaction to something older than the cache is a miss, and
        saying so is better than answering the wrong thing.
        """
        cached = self.messages.get(event.room_token, event.message_id)
        asker = event.actor.name or event.actor.id
        if cached is None:
            log.info(
                "%s asked about message %s in %s, which is not in the cache",
                event.actor.id,
                event.message_id,
                event.room_token,
            )
            await self._safe_reply(
                event,
                "I do not have that message — I only remember ones posted while I "
                "was in the conversation. Quote it or mention me instead.",
                reply_to=event.message_id,
            )
            return

        log.info(
            "%s asked the model about message %s in %s, written by %s",
            self._who(event),
            event.message_id,
            event.room_token,
            cached.author,
        )
        log.debug("the message asked about: %r", cached.text)
        prompt = (
            f"{asker} flagged the message below for you with {self.config.ask_reaction}. "
            f"Answer it, or explain it if it is not a question.\n\n"
            f"{cached.author}: {cached.text}"
        )
        try:
            answer = await self.answer_with_llm(event, prompt, attribute=False)
        except LLMError as exc:
            log.warning("completion failed: %s", exc)
            await self._report(event, str(exc))
            return
        if answer:
            await self._safe_reply(event, answer, reply_to=event.message_id)

    async def _run_llm_reply(self, event: TalkEvent, prompt: str) -> None:
        if not self.llm_enabled:
            log.debug("LLM disabled; ignoring message %s", event.message_id)
            return
        log.info(
            "%s asked the model in %s (%d chars)",
            self._who(event),
            event.room_token,
            len(prompt),
        )
        log.debug("prompt: %r", prompt)
        try:
            reply = await self.answer_with_llm(event, prompt)
        except LLMError as exc:
            log.warning("completion failed: %s", exc)
            await self._report(event, str(exc))
            return
        if reply:
            await self._safe_reply(event, reply)

    # -- actions ----------------------------------------------------------- #

    async def answer_with_llm(
        self, event: TalkEvent, prompt: str, *, attribute: bool = True
    ) -> str:
        """Run a completion in the conversation's context and remember it.

        attribute prefixes the speaker's name. Turn it off when the caller
        has already built a prompt naming the people involved, as the
        react-to-ask path does - there the actor is the person asking, not the
        author of the message being asked about.
        """
        if not self.llm_enabled:
            raise LLMError("no model is configured (set SABLE_LLM_MODEL)")
        prompt = prompt.strip()
        if not prompt:
            raise LLMError("nothing to answer")

        room = event.room_token
        turn = self._attributed(event, prompt) if attribute else prompt
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

    @staticmethod
    def _who(event: TalkEvent) -> str:
        """How a user appears in the log: display name and id, or just the id."""
        name = event.actor.name
        return f"{name} ({event.actor.id})" if name else event.actor.id

    def _attributed(self, event: TalkEvent, prompt: str) -> str:
        """Prefix the speaker's name, since rooms have more than one human."""
        name = event.actor.name or event.actor.id
        return f"{name}: {prompt}" if name else prompt

    async def reply(
        self,
        event: TalkEvent,
        message: str,
        *,
        silent: bool = False,
        reply_to: int | None = None,
    ) -> int:
        """Post a message into the conversation the event came from.

        reply_to overrides the SABLE_REPLY_AS_REPLY default, for answers that
        make no sense floating free of the message they are about.
        """
        client = self.talk(event.backend)
        if reply_to is None:
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

    async def _safe_reply(
        self, event: TalkEvent, message: str, *, reply_to: int | None = None
    ) -> None:
        """Reply, treating a failed post as a log line rather than a crash.

        Nextcloud being unreachable is not this handler's problem to solve, and
        an exception here would only surface as a stray traceback: the webhook
        request has long since been answered.
        """
        try:
            await self.reply(event, message, reply_to=reply_to)
        except (TalkError, httpx.HTTPError, ValueError) as exc:
            log.warning("could not post to %s: %s", event.room_token, exc)

    async def _report(self, event: TalkEvent, detail: str) -> None:
        if not self.config.report_errors:
            return
        await self._safe_reply(event, f"⚠️ Sorry - {detail}")
