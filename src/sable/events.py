"""Turning Talk chat messages into the events the bot acts on.

Talk's chat API hands back JSON message objects: an ``id``, who wrote it
(``actorType`` and ``actorId``), the text with rich-object placeholders like
``{mention-user1}`` and the ``messageParameters`` that fill them in. Three kinds
matter here:

* ``Create`` - a chat message was posted (``messageType`` ``comment``)
* ``Like`` / ``Undo`` - a reaction was added / revoked by a moderator. Talk reports
  these as *system* messages (``reaction`` / ``reaction_revoked``) whose ``parent``
  is the message reacted to. A reaction its author takes back arrives as
  ``reaction_deleted``, which is not parsed, since nothing acts on a removal.

Everything else - joins, renames, deleted messages - is of no interest, and
:func:`parse_message` says so by returning None. :func:`render_message` flattens
the placeholders back into something a human (or a model) can read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_-]+)\}")

#: Parameter types rendered with an ``@`` prefix.
MENTION_TYPES = {"user", "call", "guest", "user-group", "group", "federated_user", "email"}

#: Control characters, stripped from every name before anything sees it. A
#: display name and a conversation name both get spliced into the model's
#: prompt, and a newline there is the difference between sitting inside the
#: prompt and writing a line of it. The line separators Unicode adds on top of
#: these - U+0085, U+2028, U+2029 - are whitespace to str.split, which is what
#: collapses them.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: Longer than any name a person has, short enough that nobody can crowd out the
#: prompt their name sits in.
NAME_LIMIT = 100

#: The longest text taken for a reaction. An emoji is a handful of code points.
REACTION_LIMIT = 32


class EventError(ValueError):
    """Raised when a payload is not a shape we understand."""


@dataclass(frozen=True)
class Actor:
    """Whoever wrote the message.

    ``id`` is ``<actorType>/<actorId>`` - ``users/alice``, ``guests/7f3c...``,
    ``bots/relay`` - which is how the log prints people and what an entry in
    SABLE_IGNORE_USERS may spell out in full.
    """

    type: str = ""
    id: str = ""
    name: str = ""
    participant_type: str = ""

    @property
    def is_bot(self) -> bool:
        """True for other bots - never react to these, or you loop."""
        return self.type in ("bot", "bots") or self.id.startswith("bots/")

    @property
    def is_guest(self) -> bool:
        return self.id.startswith("guests/")

    @property
    def user_id(self) -> str:
        """The Nextcloud user id, or '' for guests, bots and federated users."""
        return self.id.split("/", 1)[1] if self.id.startswith("users/") else ""


@dataclass(frozen=True)
class TalkEvent:
    """A normalised Talk event."""

    type: str
    actor: Actor
    room_token: str
    room_name: str = ""
    message_id: int = 0
    message: str = ""
    raw_message: str = ""
    parameters: dict = field(default_factory=dict)
    reply_to_id: int = 0
    reaction: str = ""
    #: User ids mentioned with a real Talk mention, in order of appearance.
    mentions: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    @property
    def is_message(self) -> bool:
        return self.type == "Create"


def render_message(message: str, parameters: dict) -> str:
    """Substitute rich-object placeholders with their display names."""
    if not parameters:
        return message

    def replace(match: re.Match[str]) -> str:
        param = parameters.get(match.group(1))
        if not isinstance(param, dict):
            return match.group(0)
        name = str(param.get("name") or param.get("id") or "")
        if not name:
            return match.group(0)
        if param.get("type") in MENTION_TYPES:
            return f"@{name}"
        return name

    return PLACEHOLDER_RE.sub(replace, message)


def clean_name(value: str) -> str:
    """A display name or conversation name, flattened onto a single line."""
    return " ".join(CONTROL_RE.sub(" ", value).split())[:NAME_LIMIT].strip()


def mention_keys(parameters: dict, user_id: str) -> set[str]:
    """The placeholder names in ``parameters`` that are a mention of ``user_id``."""
    return {
        key
        for key, param in parameters.items()
        if isinstance(param, dict)
        and param.get("type") == "user"
        and str(param.get("id", "")) == user_id
    }


def _actor(payload: dict) -> Actor:
    kind = str(payload.get("actorType", ""))
    ident = str(payload.get("actorId", ""))
    return Actor(
        type=kind,
        id=f"{kind}/{ident}" if kind and ident else ident,
        name=clean_name(str(payload.get("actorDisplayName", ""))),
    )


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _reaction(payload: dict) -> str:
    """The emoji a reaction system message carries.

    Talk puts it in ``message``. A revoked reaction may instead carry a sentence
    with placeholders, so the parameters are looked at too, and anything too long
    to be an emoji is refused rather than taken for one.
    """
    text = str(payload.get("message", "")).strip()
    if (
        text
        and "{" not in text
        and len(text) <= REACTION_LIMIT
        and " " not in text
        # A bare word such as "reaction_revoked" is a system keyword, not an emoji.
        and not (text.isascii() and text.replace("_", "").isalnum())
    ):
        return text
    params = payload.get("messageParameters")
    if isinstance(params, dict):
        for key in ("reaction", "emoji"):
            value = params.get(key)
            if isinstance(value, dict):
                value = value.get("name") or value.get("id")
            if isinstance(value, str) and value.strip() and len(value) <= REACTION_LIMIT:
                return value.strip()
    return ""


def parse_message(payload: dict, *, room_token: str = "", room_name: str = "") -> TalkEvent | None:
    """Turn one chat message into a :class:`TalkEvent`, or None if it is not one
    the bot has any use for.

    Raises EventError for something that is not a message object at all.
    """
    if not isinstance(payload, dict):
        raise EventError("a chat message must be a JSON object")

    token = room_token or str(payload.get("token", ""))
    if not token:
        raise EventError("a chat message names no conversation token")

    kind = str(payload.get("messageType", ""))
    system = str(payload.get("systemMessage", ""))
    actor = _actor(payload)
    parent = payload.get("parent")
    parent_id = _int(parent.get("id")) if isinstance(parent, dict) else 0
    common = {
        "actor": actor,
        "room_token": token,
        "room_name": clean_name(room_name),
        "raw": payload,
    }

    if kind == "system" and system in ("reaction", "reaction_revoked"):
        reaction = _reaction(payload)
        # A revoked reaction need not say which emoji: nothing acts on one.
        if not parent_id or (system == "reaction" and not reaction):
            return None
        return TalkEvent(
            type="Like" if system == "reaction" else "Undo",
            message_id=parent_id,
            reaction=reaction,
            **common,
        )

    if kind != "comment":
        return None

    raw_message = str(payload.get("message", ""))
    params = payload.get("messageParameters")
    parameters = params if isinstance(params, dict) else {}
    mentions = tuple(
        str(param.get("id", ""))
        for param in (parameters.get(key) for key in PLACEHOLDER_RE.findall(raw_message))
        if isinstance(param, dict) and param.get("type") == "user" and param.get("id")
    )
    return TalkEvent(
        type="Create",
        message_id=_int(payload.get("id")),
        message=render_message(raw_message, parameters),
        raw_message=raw_message,
        parameters=parameters,
        reply_to_id=parent_id,
        mentions=mentions,
        **common,
    )
