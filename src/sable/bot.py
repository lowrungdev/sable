"""Deciding what to do with an event, and doing it."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

import httpx

from .commands import (
    Access,
    Command,
    CommandError,
    Context,
    Registry,
    parse_argv,
    registry,
    split_command,
)
from .config import Config, LLMConfig
from .events import TalkEvent, clean_name, mention_keys, parse_message, render_message
from .files import FilesClient
from .history import History
from .llm import LLMClient, LLMError
from .mentions import defang_mentions
from .openwebui import OpenWebUIClient
from .plugins import PhraseHit, PluginFailure, PluginManager
from .ratelimit import ALLOWED, FIRST_REFUSAL, RateLimiter
from .state import ConnectionState
from .talk import TalkClient, TalkError

log = logging.getLogger(__name__)


def llm_client(config: LLMConfig, http: httpx.AsyncClient) -> LLMClient | OpenWebUIClient:
    """Whichever backend the configuration asks for.

    Both answer ``complete(messages) -> str`` and raise ``LLMError``. Only the
    Open WebUI one takes ``tools=``, which the bot passes only to that one.
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


#: Said to somebody outside SABLE_LLM_USERS who addressed the model directly.
NOT_ALLOWED = "You are not allowed to use the assistant."


@dataclass(frozen=True)
class _Route:
    """What an event is a trigger for. See Bot._route."""

    kind: str  # "reaction", "command", "llm" or "phrase"
    mentioned: bool = False
    remainder: str = ""
    command: tuple[str, str] | None = None
    #: The plugin phrase handlers this message would fire. Only ever set for a plain
    #: message: never for a command, never for one addressed to the bot.
    phrases: tuple[PhraseHit, ...] = ()


class ModelNotAllowed(CommandError):  # noqa: N818 - public name, read as a refusal rather than an error
    """The sender may not make the model answer. A CommandError so that a command
    which reaches the model says so in the room, once, like any refusal."""


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
        command_registry: Registry | None = None,
    ) -> None:
        self.config = config
        #: This bot's own commands: a copy, so that plugins registered here (or a
        #: command a test adds) never reach the module-level built-ins.
        self.registry = (command_registry or registry).copy()
        #: The plugin manager, when SABLE_PLUGINS_DIR is set. See attach_plugins.
        self.plugins: PluginManager | None = None
        self.history = history or History(config.history_turns, config.history_ttl)
        self._limiter = RateLimiter(config.rate_limit)
        self._ask_key = emoji_key(config.ask_reaction)
        self.nextcloud = ConnectionState("Nextcloud")
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._llm: LLMClient | OpenWebUIClient = (
            llm if llm is not None else llm_client(config.llm, self._http)
        )
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
        names = sorted({n for n in (self.user_id, self.display_name) if n}, key=len, reverse=True)
        alternatives = "|".join(re.escape(n) for n in names)
        self._mention_re = re.compile(rf"^@?(?:{alternatives})\b[,:;]?\s*", re.IGNORECASE)
        self._mention_anywhere_re = re.compile(rf"(?<!\w)@(?:{alternatives})\b", re.IGNORECASE)

    async def aclose(self) -> None:
        if self.plugins is not None:
            await self.plugins.aclose()
        await self._llm.aclose()
        if self._owns_http:
            await self._http.aclose()

    # -- wiring ------------------------------------------------------------ #

    @property
    def llm_enabled(self) -> bool:
        return self.config.llm.enabled

    def admin_only(self, command: Command) -> bool:
        """Is this command restricted to SABLE_ADMIN_USERS?

        ``!plugins`` always is, whatever SABLE_ADMIN_COMMANDS says. So is a plugin
        command whose plugin is ``admins_only``: it is the same question asked in
        the plugin's own settings.
        """
        if command.plugin:
            if self.plugins is not None and self.plugins.admins_only(command.plugin):
                return True
        elif command.name == "plugins":
            return True
        return self.config.admin_only(command.name, *command.aliases)

    def attach_plugins(self, manager: PluginManager) -> None:
        """Take a loaded plugin manager and register its commands on this bot."""
        self.plugins = manager
        manager.is_admin = self.is_admin_actor
        # Schedules have no triggering event to post through: the manager posts
        # their dispatches straight through us, the same ChatPort a command or a
        # phrase handler's actions already go through.
        manager.port = self
        for command in manager.commands():
            self.registry.add(command)

    def plugin_access(self, command: Command, event: TalkEvent) -> Access:
        """May this person run this command here, as far as its plugin says?

        Built-ins are always OK: who may run them is settled by the rest of the
        access layers. A plugin command whose plugin is not loaded (no manager)
        is not here at all.
        """
        if not command.plugin:
            return Access.OK
        if self.plugins is None:
            return Access.NOT_HERE
        return self.plugins.allows(command.plugin, event)

    def is_admin_actor(self, event: TalkEvent) -> bool:
        """May whoever caused this event use the restricted paths?

        The bot check belongs in the decision rather than in front of it. An
        administrator's id is a plain string, and what keeps another bot from
        passing for one is the bot check in _screen happening to run
        first - true, and only true while nobody moves a line. Refusing here
        holds wherever the question is asked from.
        """
        return not event.actor.is_bot and self.config.is_admin_user(event.actor.user_id)

    def can_use_model(self, event: TalkEvent) -> bool:
        """May whoever caused this event make the model answer?

        Same shape as is_admin_actor, and for the same reason: a bot is refused
        here, in the decision, whatever id it carries.
        """
        return not event.actor.is_bot and self.config.is_llm_user(event.actor.user_id)

    @property
    def ask_enabled(self) -> bool:
        """Is the react-to-ask feature on? It needs both an emoji and a model."""
        return bool(self._ask_key) and self.llm_enabled

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
        return bool(mine) and event.actor.type == "users" and event.actor.user_id.casefold() == mine

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

    def _screen(self, event: TalkEvent) -> bool:
        """The cheap early exits: is this event from somebody we may listen to?

        Pure apart from a debug line: no dedupe, no rate limit. False means drop.
        """
        if not self.config.room_allowed(event.room_token):
            # The poller never follows these; this holds wherever else an event
            # might come from.
            log.debug(
                "ignoring %s in %s - not in SABLE_ALLOWED_ROOMS", event.type, event.room_token
            )
            return False
        if self.config.is_ignored(event.actor.id, event.actor.name):
            # Their words never reach the model, not even by somebody else
            # reacting to them: the reaction path reads the message back only
            # after this check, and the author check is made on what it reads.
            log.debug(
                "ignoring %s from %s - listed in SABLE_IGNORE_USERS",
                event.type,
                self._who(event),
            )
            return False
        if self.is_self(event):
            # Our own replies and reactions come back down the same poll.
            log.debug("ignoring %s from myself", event.type)
            return False
        if event.actor.is_bot:
            log.debug("ignoring %s from bot %s", event.type, event.actor.id)
            return False
        return True

    def _route(self, event: TalkEvent) -> _Route | None:
        """Is this event a trigger, and of what kind? None means it is not.

        The single classification behind both ``handle`` and ``would_handle``,
        so the poller's pre-filter cannot drift from what handle acts on. Pure
        apart from debug lines.
        """
        # Gated on ask_enabled, not just the emoji: with no model configured there
        # is nothing to ask.
        if self.ask_enabled and event.type == "reaction" and self.is_ask_reaction(event.reaction):
            return _Route("reaction")

        if not event.is_message:
            log.debug("no handler for %s events", event.type)
            return None

        text = event.message.strip()
        if not text:
            return None

        mentioned, remainder = self.strip_mention(event)
        command = split_command(remainder, self.config.command_prefix)
        in_ai_room = self.config.ai_room_allowed(event.room_token)

        # A plain message, which is what plugin phrases listen to. Not a command and
        # not addressed to us: those have their own paths, and a phrase firing on top
        # of "!ping" or "@sable ..." would answer a question nobody asked twice. Only
        # peeked at here (nothing is consumed), so this is safe for would_handle too.
        # In an AI room the model answers every message, and a phrase handler may
        # fire as well: both are for the same message, each its own decision.
        phrases: tuple[PhraseHit, ...] = ()
        if command is None and not mentioned and self.plugins is not None:
            phrases = self.plugins.phrase_hits(event)

        if command is None and not mentioned and not in_ai_room and not phrases:
            log.debug(
                "message in %s (%r) was not for me - no prefix, no mention, and not an AI room",
                event.room_token,
                event.room_name,
            )
            return None
        if command is not None:
            return _Route("command", mentioned, remainder, command)
        if mentioned or in_ai_room:
            return _Route("llm", mentioned, remainder, None, phrases)
        return _Route("phrase", phrases=phrases)

    def would_handle(self, event: TalkEvent) -> bool:
        """Would ``handle`` do any work for this event? Synchronous and free of
        side effects: it consumes no rate-limit token. The poller asks before
        spawning, so that chatter, our own replies and other bots never occupy a
        reply slot."""
        return self._screen(event) and self._route(event) is not None

    async def handle(self, event: TalkEvent) -> None:
        """Entry point for an event read from a conversation."""
        # Re-checked here although the poller asks would_handle first: this is
        # also the entry point for anything that does not come through it.
        if not self._screen(event):
            return
        route = self._route(event)
        if route is None:
            return
        if route.kind == "phrase":
            # An ambient phrase match is never a trigger the per-person limit should
            # see: it costs the sender nothing, so a broad, cooldown-0 phrase can
            # never exhaust somebody's budget for a real command. Cooldowns still
            # start now, with nothing awaited since the route was decided, so that
            # two handlers cannot both be sent the same message in the same room.
            firing = self._claim_phrases(route.phrases)
            if firing:
                await self._run_phrases(event, firing)
            return
        # Everything else - a command, a mention, a reaction, a plain message in an
        # AI room - costs exactly one token, whatever it triggers alongside it.
        if self._rate_limited(event):
            return
        firing = self._claim_phrases(route.phrases)
        if route.kind == "reaction":
            await self._run_reaction_query(event)
        elif route.command is not None:
            await self._run_command(event, *route.command)
        elif firing:
            # An AI-room message with no mention: the model answers and any phrase
            # handler fires alongside it, concurrently, for the one token above.
            outcomes = await asyncio.gather(
                self._run_llm_reply(event, route.remainder, explicit=route.mentioned),
                self._run_phrases(event, firing),
                return_exceptions=True,
            )
            failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
            for extra in failures[1:]:
                # Only the first failure can be raised; the rest must not vanish.
                log.error("a concurrent phrase handler or model reply also failed", exc_info=extra)
            if failures:
                raise failures[0]
        else:
            await self._run_llm_reply(event, route.remainder, explicit=route.mentioned)

    def _claim_phrases(self, hits: tuple[PhraseHit, ...]) -> list[PhraseHit]:
        """Start the cooldown of each phrase handler about to be called."""
        manager = self.plugins
        if manager is None:
            return []
        return [hit for hit in hits if manager.claim_phrase(hit)]

    async def _run_phrases(self, event: TalkEvent, hits: list[PhraseHit]) -> None:
        """Call the phrase handlers, together; one failing leaves the others alone."""
        await asyncio.gather(*(self._run_phrase(event, hit) for hit in hits))

    async def _run_phrase(self, event: TalkEvent, hit: PhraseHit) -> None:
        """One phrase handler. Nobody asked it anything, so whatever goes wrong is for
        the log and never for the room: no crash notice, no plugin error, no reply.
        Never raises: this runs inside a plain ``asyncio.gather`` alongside other
        handlers (and, in an AI room, the model reply), with none of them cancelled
        if one raises - so nothing here may be let to.
        """
        manager = self.plugins
        if manager is None:
            return
        log.info(
            "%s triggered phrase handler %s/%s in %s",
            self._who(event),
            hit.plugin,
            hit.handler,
            event.room_token,
        )
        try:
            reply = await manager.run_phrase(hit, event, self)
        except CommandError as exc:
            # The handler's own PluginError. For a command this is an answer shown
            # as the plugin wrote it, unredacted; here it only ever reaches the log,
            # so it gets the same treatment as any other worker-authored text: one
            # line, and the plugin's own settings values blanked. Without that, a
            # phrase handler could inject a fake log line with an embedded newline,
            # or quote a secret verbatim into the log, neither of which chat ever
            # sees but a search of the log would.
            log.info(
                "plugin %s: phrase handler %s said: %s",
                hit.plugin,
                hit.handler,
                manager.log_safe(hit.plugin, str(exc)),
            )
            return
        except PluginFailure as exc:
            log.warning("plugin %s: phrase handler %s failed: %s", hit.plugin, hit.handler, exc)
            return
        except Exception:
            log.exception("plugin %s: phrase handler %s crashed", hit.plugin, hit.handler)
            return
        if reply:
            try:
                await self._safe_reply(event, reply)
            except Exception:
                log.exception(
                    "plugin %s: phrase handler %s's reply could not be posted",
                    hit.plugin,
                    hit.handler,
                )

    def _rate_limited(self, event: TalkEvent) -> bool:
        """Count this trigger against its sender; True means drop it, silently.

        Keyed on the actor id (``users/alice``), so a guest and a user with the
        same name do not share a bucket. Administrators are counted like anybody.
        """
        verdict = self._limiter.hit(event.actor.id)
        if verdict == ALLOWED:
            return False
        if verdict == FIRST_REFUSAL:
            log.warning(
                "rate limit: ignoring %s from %s - more than %d triggers a minute "
                "(SABLE_RATE_LIMIT); said once per minute",
                event.type,
                self._who(event),
                self.config.rate_limit,
            )
        return True

    async def _run_command(self, event: TalkEvent, name: str, args: str) -> None:
        command = self.registry.get(name)
        access = Access.OK if command is None else self.plugin_access(command, event)
        if command is None or access is Access.NOT_HERE:
            # A plugin command in a room, or for a person, its plugin does not
            # serve is answered exactly like one that does not exist, hint
            # included: nothing here says the plugin is installed.
            log.debug("unknown command %r in %s", name, event.room_token)
            if self.config.unknown_command_hint:
                await self._safe_reply(
                    event,
                    f"I have no `{name}` command. Try `{self.config.command_prefix}help`.",
                )
            return

        if access is Access.NOT_YOU:
            log.info(
                "refused %s%s for %s - not allowed by its plugin",
                self.config.command_prefix,
                command.name,
                self._who(event),
            )
            await self._safe_reply(
                event, f"`{self.config.command_prefix}{command.name}` is not available to you."
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
                f"`{self.config.command_prefix}{command.name}` is for administrators only.",
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
        except (LLMError, TalkError, httpx.HTTPError, PluginFailure) as exc:
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

        The event names the message by id only, so it is read back from Talk. That
        also means it is read only after the checks on who is asking, and the
        author's own ignore-list entry is honoured on what comes back.
        """
        if self.config.ask_admins_only and not self.is_admin_actor(event):
            # Refused in the log and nowhere else. A reaction is not a command: it
            # asks nobody anything, so there is no question left hanging by
            # silence, while a refusal would be threaded under a third person's
            # message for everyone in the room to read - and the room has done
            # nothing wrong. Nothing of ours is showing either, the thinking
            # reaction never having gone on, so there is nothing to take back.
            log.warning(
                "refused the %s reaction on message %s in %s for %s - not in SABLE_ADMIN_USERS",
                self.config.ask_reaction,
                event.message_id,
                event.room_token,
                self._who(event),
            )
            return
        if not self.can_use_model(event):
            log.info(
                "ignoring the %s reaction on message %s in %s from %s - not in SABLE_LLM_USERS",
                self.config.ask_reaction,
                event.message_id,
                event.room_token,
                self._who(event),
            )
            return

        try:
            raw = await self.talk.message(event.room_token, event.message_id)
        except (TalkError, httpx.HTTPError) as exc:
            log.warning(
                "could not read message %s in %s: %s", event.message_id, event.room_token, exc
            )
            await self._report(event, f"I could not read the message you reacted to ({exc})")
            return
        if raw is None:
            log.info(
                "%s asked about message %s in %s, which Talk does not have",
                event.actor.id,
                event.message_id,
                event.room_token,
            )
            await self._safe_reply(
                event,
                "I cannot find that message - it may have been deleted.",
                reply_to=event.message_id,
            )
            return
        kind = str(raw.get("messageType", ""))
        if kind == "comment_deleted":
            await self._safe_reply(
                event, "That message has been deleted.", reply_to=event.message_id
            )
            return
        target = parse_message(raw, room_token=event.room_token) if kind == "comment" else None
        if target is None:
            # A system message, a join or a rename: nothing anybody wrote to ask about.
            log.debug(
                "the %s reaction on %s message %s: nothing to answer",
                self.config.ask_reaction,
                kind or "unknown",
                event.message_id,
            )
            return
        if self.config.is_ignored(target.actor.id, target.actor.name):
            log.debug(
                "not answering about message %s - its author is listed in SABLE_IGNORE_USERS",
                event.message_id,
            )
            return
        text = target.message.strip()
        if not text:
            await self._safe_reply(
                event,
                "That message has no text for me to read.",
                reply_to=event.message_id,
            )
            return

        # Our own answer is fair game: reacting to it is how somebody asks a
        # follow-up. The author is named, so the model can see whose words they are.
        author = "me" if self.is_self(target) else target.actor.name or target.actor.id
        asker = event.actor.name or event.actor.id
        log.info(
            "%s asked the model about message %s in %s, written by %s",
            self._who(event),
            event.message_id,
            event.room_token,
            author,
        )
        log.debug("the message asked about: %r", text)
        prompt = (
            f"{asker} flagged the message below for you with {self.config.ask_reaction}. "
            f"Answer it, or explain it if it is not a question.\n\n"
            f"{author}: {text}"
        )
        try:
            answer = await self.answer_with_llm(event, prompt, attribute=False)
        except ModelNotAllowed:
            return
        except LLMError as exc:
            log.warning("completion failed: %s", exc)
            await self._report(event, str(exc))
            return
        if answer:
            await self._safe_reply(event, answer, reply_to=event.message_id)

    async def _run_llm_reply(
        self, event: TalkEvent, prompt: str, *, explicit: bool = False
    ) -> None:
        """Answer a mention (explicit) or a plain message in an AI room.

        Somebody outside SABLE_LLM_USERS who mentioned us is told once; a plain
        message in an AI room is not addressed to us, so refusing it would only
        make every line they type in that room a second line from us.
        """
        if not self.llm_enabled:
            log.debug("LLM disabled; ignoring message %s", event.message_id)
            return
        if not self.can_use_model(event):
            log.info(
                "not answering %s in %s - not in SABLE_LLM_USERS",
                self._who(event),
                event.room_token,
            )
            if explicit:
                await self._safe_reply(event, NOT_ALLOWED)
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
        except ModelNotAllowed:
            return
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
        if not self.can_use_model(event):
            # The one gate every path to the model passes, custom commands too.
            log.info(
                "refused the model to %s in %s - not in SABLE_LLM_USERS",
                self._who(event),
                event.room_token,
            )
            raise ModelNotAllowed(NOT_ALLOWED)
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
            if self.config.llm.agentic:
                # Tools only where SABLE_LLM_TOOL_ROOMS says so.
                # llm_client() returns an OpenWebUIClient whenever llm.agentic is set.
                llm = cast(OpenWebUIClient, self._llm)
                answer = await llm.complete(messages, tools=self.config.llm.tools_in(room))
            else:
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
        # Everything posted here was triggered by chat input - a model's answer to
        # it, an error quoting it - so it must not be able to ping the whole room.
        return await client.send_message(
            event.room_token, defang_mentions(message), reply_to=reply_to, silent=silent
        )

    async def send(
        self, room_token: str, message: str, *, silent: bool = False, reply_to: int = 0
    ) -> int:
        """Post into a conversation without an incoming event (alerting path)."""
        return await self.talk.send_message(room_token, message, silent=silent, reply_to=reply_to)

    async def _safe_reply(
        self,
        event: TalkEvent,
        message: str,
        *,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> bool:
        """Reply, treating a failed post as a log line rather than a crash.

        Nextcloud being unreachable is not this handler's problem to solve, and
        an exception here would only surface as a stray traceback. True if the
        message was posted.
        """
        try:
            await self.reply(event, message, reply_to=reply_to, silent=silent)
        except (TalkError, httpx.HTTPError, ValueError) as exc:
            log.warning("could not post to %s: %s", event.room_token, exc)
            return False
        return True

    # -- on behalf of plugins ----------------------------------------------- #
    # The plugin manager has already limited what a plugin may ask for; these
    # only carry it out, and never raise for a Talk failure.

    async def plugin_reply(self, event: TalkEvent, text: str, *, silent: bool = False) -> bool:
        return await self._safe_reply(event, text, silent=silent)

    async def plugin_send(self, room: str, text: str, *, silent: bool = False) -> bool:
        try:
            await self.send(room, defang_mentions(text), silent=silent)
        except (TalkError, httpx.HTTPError, ValueError) as exc:
            log.warning("could not post to %s: %s", room, exc)
            return False
        return True

    async def plugin_react(self, event: TalkEvent, emoji: str) -> bool:
        return await self.talk.try_react(event.room_token, event.message_id, emoji)

    async def _report(self, event: TalkEvent, detail: str) -> None:
        if not self.config.report_errors:
            return
        await self._safe_reply(event, f"⚠️ Sorry - {detail}")
