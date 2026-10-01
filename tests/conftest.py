from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, AsyncIterator

import httpx
import pytest

from sable.bot import Bot
from sable.config import Config, LLMConfig
from sable.events import parse_message
from sable.llm import Message
from sable.talk import API_BASE

BACKEND = "https://cloud.example.org"
USER = "sable"
PASSWORD = "app-password-1234"
ROOM = "abcd1234"
ROOM_NAME = "Team chat"

#: The base every Talk call is made under, for building mock routes.
TALK = f"{BACKEND}{API_BASE}"


@pytest.fixture
def config() -> Config:
    return make_config()


def make_config(**overrides: Any) -> Config:
    defaults: dict[str, Any] = {
        "nextcloud_url": BACKEND,
        "nextcloud_user": USER,
        "nextcloud_password": PASSWORD,
        # Off by default: no test should reach the network implicitly. The
        # credentials check has its own tests.
        "startup_check": False,
        "llm": LLMConfig(model="some-model", api_key="sk-test"),
    }
    defaults.update(overrides)
    return Config(**defaults)


class FakeLLM:
    """Stands in for :class:`sable.llm.LLMClient`."""

    def __init__(self, reply: str = "mock answer", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[list[Message]] = []
        #: The ``tools`` argument of each call; None where it was not passed, which
        #: is how the plain OpenAI-compatible client is called.
        self.tools: list[bool | None] = []

    async def complete(
        self, messages: list[Message], *, model: str | None = None, tools: bool | None = None
    ) -> str:
        self.calls.append(messages)
        self.tools.append(tools)
        if self.error is not None:
            raise self.error
        return self.reply

    async def aclose(self) -> None:
        return None

    @property
    def last_prompt(self) -> str:
        return self.calls[-1][-1]["content"]


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        yield client


@pytest.fixture
def bot(config: Config, llm: FakeLLM, http_client: httpx.AsyncClient) -> Bot:
    return Bot(config, http_client=http_client, llm=llm)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #


def split_actor(actor_id: str) -> tuple[str, str]:
    """``users/alice`` as Talk sends it: actorType ``users``, actorId ``alice``."""
    kind, slash, ident = actor_id.partition("/")
    return (kind, ident) if slash else ("", actor_id)


def message_payload(
    text: str = "hello",
    *,
    message_id: int = 100,
    room: str = ROOM,
    actor_id: str = "users/alice",
    actor_name: str = "Alice",
    parameters: dict | None = None,
    in_reply_to: int = 0,
) -> dict:
    """A chat message as the chat API returns it."""
    kind, ident = split_actor(actor_id)
    payload: dict[str, Any] = {
        "id": message_id,
        "token": room,
        "actorType": kind,
        "actorId": ident,
        "actorDisplayName": actor_name,
        "timestamp": 1700000000,
        "message": text,
        "messageParameters": parameters or {},
        "messageType": "comment",
        "systemMessage": "",
        "reactions": {},
    }
    if in_reply_to:
        payload["parent"] = {"id": in_reply_to, "messageType": "comment", "message": "earlier"}
    return payload


def mention(key: str = "mention-user1", user: str = USER, name: str = "sable") -> dict:
    """One ``messageParameters`` entry for a Talk mention of a user."""
    return {key: {"type": "user", "id": user, "name": name}}


def as_bot(parsed):
    """The same event with its actor typed ``bots`` but keeping the id it had.

    Talk never sends that - a bot's id carries its own ``bots/`` prefix - but the
    decisions that refuse a bot are written not to lean on the id, and this is how
    a test reaches them: a bot actor whose id still reads as an administrator's.
    """
    return replace(parsed, actor=replace(parsed.actor, type="bots"))


def event(
    text: str = "hello",
    *,
    room_name: str = ROOM_NAME,
    actor_type: str = "",
    **kwargs: Any,
):
    parsed = parse_message(message_payload(text, **kwargs), room_name=room_name)
    return as_bot(parsed) if actor_type == "bots" else parsed


def reaction_payload(
    reaction: str = "👍",
    *,
    message_id: int = 100,
    room: str = ROOM,
    actor_id: str = "users/alice",
    actor_name: str = "Alice",
    undo: bool = False,
    system_id: int = 5000,
) -> dict:
    """The system message Talk posts when somebody reacts, or takes one back."""
    kind, ident = split_actor(actor_id)
    return {
        "id": system_id,
        "token": room,
        "actorType": kind,
        "actorId": ident,
        "actorDisplayName": actor_name,
        "message": "" if undo else reaction,
        "messageParameters": {},
        "messageType": "system",
        "systemMessage": "reaction_revoked" if undo else "reaction",
        "parent": {"id": message_id, "messageType": "comment", "message": "the original"},
    }


def reaction_event(reaction: str = "👍", *, actor_type: str = "", **kwargs: Any):
    parsed = parse_message(reaction_payload(reaction, **kwargs), room_name=ROOM_NAME)
    return as_bot(parsed) if actor_type == "bots" else parsed


# --------------------------------------------------------------------------- #
# Actor shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ActorShape:
    """One kind of actor id Talk sends, and what :class:`sable.events.Actor`
    should make of it.

    Those three derived properties are all any later decision reads, and an
    empty ``user_id`` carries the most weight of the three: it is what stops
    ``Config.is_admin_user`` promoting somebody who has simply renamed
    themselves after an administrator.
    """

    label: str
    actor_id: str
    actor_name: str
    user_id: str = ""
    is_guest: bool = False
    is_bot: bool = False

    @property
    def payload_kwargs(self) -> dict[str, str]:
        """Keyword arguments for :func:`message_payload` / :func:`reaction_payload`."""
        return {"actor_id": self.actor_id, "actor_name": self.actor_name}

    @property
    def bare_id(self) -> str:
        """Whatever follows the prefix - one of the three things an entry in
        SABLE_IGNORE_USERS may match, and the form an operator reading a
        Nextcloud user list has in front of them."""
        return self.actor_id.split("/", 1)[1] if "/" in self.actor_id else self.actor_id


#: Every actor shape the bot has to cope with, each with the answers
#: :class:`sable.events.Actor` owes for it. Shared between the parsing tests and
#: the end-to-end ones so both are arguing about the same list.
ACTOR_SHAPES: tuple[ActorShape, ...] = (
    ActorShape("a_user", "users/alice", "Alice", user_id="alice"),
    # A guest picks their own display name every time they join, which is why no
    # decision may rest on it.
    ActorShape("a_guest", "guests/7f3c9a2b", "Guest", is_guest=True),
    # A real person, on somebody else's server: not a guest and not a bot, yet
    # with no user id here either.
    ActorShape("a_federated_user", "federated_users/karl@cloud.example.net", "Karl"),
    ActorShape("a_bot", "bots/relay", "Relay", is_bot=True),
)


#: Ids that are not a user's, though four of them look close enough to be worth
#: writing down: ``Actor.user_id`` is exact about the prefix, and everything
#: downstream reads an empty user id as "nobody in particular".
NON_USER_IDS: tuple[tuple[str, str], ...] = (
    ("a_capitalised_prefix", "Users/alice"),
    ("no_prefix_at_all", "alice"),
    ("a_prefix_without_its_slash", "usersalice"),
    ("a_prefix_and_nothing_else", "users/"),
    ("a_federated_cloud_id", "federated_users/alice@cloud.example.net"),
    ("an_empty_id", ""),
)


# --------------------------------------------------------------------------- #
# Conversation tokens
# --------------------------------------------------------------------------- #

#: Tokens ``TOKEN_RE`` accepts. Talk itself mints eight lowercase alphanumerics,
#: but the regex spans 4-64 characters, and the edges of that range are where a
#: token gets truncated or rejected by mistake.
VALID_TOKENS: tuple[tuple[str, str], ...] = (
    ("four_chars_the_minimum", "abcd"),
    ("eight_chars_as_talk_mints_them", "s7xk29qp"),
    ("all_digits", "1234567890"),
    ("all_letters", "conversation"),
    ("sixty_four_chars_the_maximum", "z" * 64),
)

#: Strings that can never be a conversation token. Talk's own routes only match
#: lowercase alphanumerics, so each of these names a conversation that cannot
#: exist - and the last few are what somebody pastes when they mean the room's
#: name, or when they are trying their luck with the path.
REJECTED_TOKENS: tuple[tuple[str, str], ...] = (
    ("uppercase", "ABCD1234"),
    ("mixed_case", "Abcd1234"),
    ("three_chars", "abc"),
    ("sixty_five_chars", "a" * 65),
    ("empty", ""),
    ("a_hyphen", "abcd-1234"),
    ("an_underscore", "abcd_1234"),
    ("a_slash", "abcd/1234"),
    ("a_leading_path_traversal", "../abcd1234"),
    ("an_embedded_dot_dot", "abcd..1234"),
    ("a_leading_space", " abcd1234"),
    ("a_trailing_newline", "abcd1234" + chr(10)),
    ("a_room_name", "Team chat"),
)


def actor_event(shape: ActorShape, text: str = "hello", **kwargs: Any):
    """A chat event from this actor shape. Keyword arguments override the shape,
    so a test can keep the id and vary the display name."""
    return event(text, **{**shape.payload_kwargs, **kwargs})


def actor_reaction_event(shape: ActorShape, reaction: str = "👍", **kwargs: Any):
    """A reaction event from this actor shape - the other way into Bot.handle."""
    return reaction_event(reaction, **{**shape.payload_kwargs, **kwargs})
