"""Deciding what to do with an event, and doing it."""

from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .commands import (
    Command,
    CommandError,
    Context,
    Registry,
    parse_argv,
    registry,
    split_command,
)
from .config import Config, LLMConfig
from .events import TalkEvent, clean_name, mention_keys, render_message
from .files import FilesClient
from .history import History, MessageCache
from .llm import LLMClient, LLMError
from .openwebui import OpenWebUIClient
from .state import ConnectionState
from .talk import TalkClient, TalkError

log = logging.getLogger(__name__)


def llm_client(config: LLMConfig, http: httpx.AsyncClient) -> LLMClient | OpenWebUIClient:
    """Whichever backend the configuration asks for.

    Both answer ``complete(messages) -> str`` and raise ``LLMError``, so nothing
    downstream needs to know which one it is holding.
    """
    if config.agentic:
        return OpenWebUIClient(config, client=http)
    return LLMClient(config, client=http)


def now(timezone: str = "") -> str:
    """The current time, written for a model rather than for a log.

    A model has no clock. Asked what something costs *now*, it answers from
    whenever its training data stopped - confidently, and with a figure that can
    be years stale. One line of prompt is the whole fix.
    """
    moment = datetime.now(ZoneInfo(timezone)) if timezone else datetime.now().astimezone()
    label = timezone or moment.tzname() or "local time"
    return f"{moment:%A %d %B %Y, %H:%M} ({label})"

#: How many recently handled events to remember, so a message delivered twice
#: does not produce a second reply.
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
        self._llm = llm if llm is not None else llm_client(config.llm, self._http)
        self._seen: deque[SeenKey] = deque(maxlen=SEEN_CACHE)
        self._seen_set: set[SeenKey] = set()
        #: The one account sable is. Everything posted under its id is ours.
        self.talk = TalkClient(
            config.nextcloud_url,
            config.nextcloud_user,
            config.nextcloud_password,
            client=self._http,
            max_message_chars=config.max_message_chars,
            state=self.nextcloud,
        )
        self._files: FilesClient | None = None
        self.user_id = config.nextcloud_user
        self.display_name = ""
        self._set_identity(config.nextcloud_user, "")

    def _set_identity(self, user_id: str, display_name: str) -> None:
        """Learn who we are, and rebuild what recognises a mention of us."""
        self.user_id = user_id
        self.display_name = clean_name(display_name)
        names = sorted(
            {n for n in (self.user_id, self.display_name) if n}, key=len, reverse=True
        )
        alternatives = "|".join(re.escape(n) for n in names)
        self._mention_re = re.compile(
            rf"^@?(?:{alternatives})\b[,:;]?\s*", re.IGNORECASE
        )
        self._mention_anywhere_re = re.compile(
            rf"(?<!\w)@(?:{alternatives})\b", re.IGNORECASE
        )

    async def aclose(self) -> None:
        await self._llm.aclose()
        if self._owns_http:
            await self._http.aclose()

    # -- wiring ------------------------------------------------------------ #

    @property
    def llm_enabled(self) -> bool:
        return self.config.llm.enabled

    def admin_only(self, command: Command) -> bool:
        """Is this command restricted to SABLE_ADMIN_USERS?"""
        return self.config.admin_only(command.name, *command.aliases)

    def is_admin_actor(self, event: TalkEvent) -> bool:
        """May whoever caused this event use the restricted paths?

        The bot check belongs in the decision rather than in front of it. An
        administrator's id is a plain string, and what keeps another bot from
        passing for one is the is_bot early return in handle happening to run
        first - true, and only true while nobody moves a line. Refusing here
        holds wherever the question is asked from.
        """
        return not event.actor.is_bot and self.config.is_admin_user(event.actor.user_id)

    @property
    def ask_enabled(self) -> bool:
        """Is the react-to-ask feature on? It needs both an emoji and a model."""
        return bool(self._ask_key) and self.llm_enabled

    def remembers_messages(self, event: TalkEvent) -> bool:
        """Are this conversation's messages kept, so a reaction can name one?

        Scoped per conversation because the cache is the whole data-at-rest cost
        of the feature (accepted risk 6): every room the bot sits in otherwise
        holds its last SABLE_MESSAGE_CACHE messages in memory to serve a reaction
        that, in most of them, nobody will ever send.
        """
        return self.ask_enabled and self.config.ask_room_allowed(
            event.room_token, event.room_name
        )

    def is_ask_reaction(self, reaction: str) -> bool:
        return bool(self._ask_key) and emoji_key(reaction) == self._ask_key

    async def check_nextcloud(self) -> bool:
        """Sign in and log who we are, or why we could not.

        Not fatal when it fails: Nextcloud may simply not be up yet, and the
        receive loop keeps retrying with backoff either way. A rejected password
        is different - nothing will work until someone changes it - and says so
        in words that point at the setting.
        """
        try:
            user_id, display_name = await self.talk.whoami()
        except httpx.HTTPError as exc:
            log.warning(
                "could not reach Nextcloud at %s: %s. Nothing will be received or "
                "sent until this works - check the URL, DNS, and whether the "
                "certificate is trusted (see docs/deployment.md on internal CAs)",
                self.config.nextcloud_url,
                exc,
            )
            return False
        except TalkError as exc:
            if exc.status in (401, 403):
                log.error(
                    "Nextcloud at %s refused the credentials for %r (HTTP %s). Check "
                    "SABLE_NEXTCLOUD_USER and SABLE_NEXTCLOUD_PASSWORD - the password "
                    "should be an app password, and it stops working when revoked.",
                    self.config.nextcloud_url,
                    self.config.nextcloud_user,
                    exc.status,
                )
            else:
                log.warning(
                    "Nextcloud at %s answered HTTP %s when asked who %r is; it is "
                    "reachable but may not be healthy: %s",
                    self.config.nextcloud_url,
                    exc.status,
                    self.config.nextcloud_user,
                    exc.body[:200],
                )
            return False

        self._set_identity(user_id, display_name)
        log.info(
            "signed in to %s as %s%s",
            self.config.nextcloud_url,
            user_id,
            f" ({self.display_name})" if self.display_name else "",
        )
        return True

    def files(self) -> FilesClient:
        """The upload client, as the same account. Built once: it remembers that
        the upload folder exists."""
        if self._files is None:
            self._files = FilesClient(
                self.config.nextcloud_url,
                self.config.nextcloud_user,
                self.config.nextcloud_password,
                upload_path=self.config.upload_path,
                client=self._http,
                state=self.nextcloud,
            )
        return self._files

    def is_self(self, event: TalkEvent) -> bool:
        """Did we write this ourselves? Acting on it would mean answering ourselves."""
        mine = self.user_id.casefold()
        return (
            bool(mine)
            and event.actor.type == "users"
            and event.actor.user_id.casefold() == mine
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

    def strip_mention(self, event: TalkEvent) -> tuple[bool, str]:
        """Detect a mention of this account and return the text without it.

        A real Talk mention arrives as a ``user`` parameter carrying our id, and
        that is what counts. Failing that, the account's id or display name typed
        out - at the start of the message, or as ``@name`` anywhere in it.
        """
        if self.user_id in event.mentions:
            ours = mention_keys(event.parameters, self.user_id)
            raw, end = event.raw_message, 0
            lead_re = re.compile(r"\s*\{([a-zA-Z0-9_-]+)\}[,:;]?\s*")
            while True:
                lead = lead_re.match(raw, end)
                if lead is None or lead.group(1) not in ours:
                    break
                end = lead.end()
            if end:
                return True, render_message(raw[end:], event.parameters).strip()
            return True, event.message
        text = event.message
        match = self._mention_re.match(text)
        if match:
            return True, text[match.end() :].strip()
        if self._mention_anywhere_re.search(text):
            return True, text
        return False, text

    async def handle(self, event: TalkEvent) -> None:
        """Entry point for an event read from a conversation."""
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
        #
        # Which conversation it is, is the one further thing this may turn on: it
        # is a fact about the room rather than about the sender or the event, so
        # asking it here cannot quietly reintroduce the checks below.
        if event.is_message and self.remembers_messages(event):
            self.messages.add(
                event.room_token,
                event.message_id,
                event.actor.name or event.actor.id,
                event.message.strip(),
            )

        if self.is_self(event):
            # Our own replies and reactions come back down the same poll.
            log.debug("ignoring %s from myself", event.type)
            return
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

        if not event.is_message:
            log.debug("no handler for %s events", event.type)
            return

        text = event.message.strip()
        if not text:
            return

        mentioned, remainder = self.strip_mention(event)
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

        if self.admin_only(command) and not self.is_admin_actor(event):
            log.warning(
                "refused %s%s for %s - not in SABLE_ADMIN_USERS",
                self.config.command_prefix,
                command.name,
                self._who(event),
            )
            await self._safe_reply(
                event,
                f"`{self.config.command_prefix}{command.name}` is for "
                "administrators only.",
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
        if self.config.ask_admins_only and not self.is_admin_actor(event):
            # Refused in the log and nowhere else. A reaction is not a command: it
            # asks nobody anything, so there is no question left hanging by
            # silence, while a refusal would be threaded under a third person's
            # message for everyone in the room to read - and the room has done
            # nothing wrong. Nothing of ours is showing either, the thinking
            # reaction never having gone on, so there is nothing to take back.
            log.warning(
                "refused the %s reaction on message %s in %s for %s - not in "
                "SABLE_ADMIN_USERS",
                self.config.ask_reaction,
                event.message_id,
                event.room_token,
                self._who(event),
            )
            return

        cached = self.messages.get(event.room_token, event.message_id)
        asker = event.actor.name or event.actor.id
        if cached is None:
            # Same miss, two reasons, and they send the reader different places: in
            # a conversation outside SABLE_ASK_ROOMS nothing was ever kept, so
            # blaming the age of the message would have somebody scrolling for one
            # that could not have been there however recent it was.
            unlisted = not self.remembers_messages(event)
            log.info(
                "%s asked about message %s in %s, which is not in the cache%s",
                event.actor.id,
                event.message_id,
                event.room_token,
                " - the conversation is not in SABLE_ASK_ROOMS" if unlisted else "",
            )
            await self._safe_reply(
                event,
                (
                    "I do not keep this conversation's messages, so I cannot see "
                    "the one you reacted to. Quote it or mention me instead."
                )
                if unlisted
                else (
                    "I do not have that message — I only remember ones posted while "
                    "I was in the conversation. Quote it or mention me instead."
                ),
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
        client = self.talk
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
        prompt += f"\nThe current date and time is {now(self.config.timezone)}."
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
        client = self.talk
        if reply_to is None:
            reply_to = event.message_id if self.config.reply_as_reply else 0
        return await client.send_message(
            event.room_token, message, reply_to=reply_to, silent=silent
        )

    async def send(
        self, room_token: str, message: str, *, silent: bool = False, reply_to: int = 0
    ) -> int:
        """Post into a conversation without an incoming event (alerting path)."""
        return await self.talk.send_message(
            room_token, message, silent=silent, reply_to=reply_to
        )

    async def _safe_reply(
        self, event: TalkEvent, message: str, *, reply_to: int | None = None
    ) -> None:
        """Reply, treating a failed post as a log line rather than a crash.

        Nextcloud being unreachable is not this handler's problem to solve, and
        an exception here would only surface as a stray traceback.
        """
        try:
            await self.reply(event, message, reply_to=reply_to)
        except (TalkError, httpx.HTTPError, ValueError) as exc:
            log.warning("could not post to %s: %s", event.room_token, exc)

    async def _report(self, event: TalkEvent, detail: str) -> None:
        if not self.config.report_errors:
            return
        await self._safe_reply(event, f"⚠️ Sorry - {detail}")
