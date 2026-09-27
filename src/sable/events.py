"""Parsing the Activity Streams payloads Nextcloud Talk posts to the webhook.

Talk speaks a small subset of ActivityStreams 2.0:

* ``Create``  - a chat message was posted
* ``Like`` / ``Undo`` - a reaction was added / removed
* ``Join`` / ``Leave`` - the bot was enabled / disabled in a conversation

The chat text itself arrives twice-encoded: ``object.content`` is a JSON string
holding ``{"message": ..., "parameters": ...}``, where the message carries
rich-object placeholders like ``{mention-user1}``. :func:`render_message`
flattens that back into something a human (or a model) can read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_-]+)\}")

#: Parameter types rendered with an ``@`` prefix.
MENTION_TYPES = {"user", "call", "guest", "user-group", "group", "federated_user", "email"}


class EventError(ValueError):
    """Raised when a payload is not a shape we understand."""


@dataclass(frozen=True)
class Actor:
    """Whoever caused the event."""

    type: str = ""
    id: str = ""
    name: str = ""
    participant_type: str = ""

    @property
    def is_bot(self) -> bool:
        """True for bots (including us) - never react to these, or you loop."""
        return self.type == "Application" or self.id.startswith("bots/")

    @property
    def is_guest(self) -> bool:
        return self.id.startswith("guests/")

    @property
    def user_id(self) -> str:
        """The Nextcloud user id, or '' for guests and bots."""
        return self.id.split("/", 1)[1] if self.id.startswith("users/") else ""


@dataclass(frozen=True)
class TalkEvent:
    """A normalised Talk event."""

    type: str
    actor: Actor
    room_token: str
    room_name: str = ""
    backend: str = ""
    message_id: int = 0
    message: str = ""
    raw_message: str = ""
    parameters: dict = field(default_factory=dict)
    reply_to_id: int = 0
    reaction: str = ""
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


def _actor(payload: dict) -> Actor:
    raw = payload.get("actor") or {}
    return Actor(
        type=str(raw.get("type", "")),
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        participant_type=str(raw.get("talkParticipantType", "")),
    )


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def parse_event(payload: dict, backend: str = "") -> TalkEvent:
    """Turn a webhook body into a :class:`TalkEvent`."""
    if not isinstance(payload, dict):
        raise EventError("payload must be a JSON object")

    event_type = str(payload.get("type", ""))
    if not event_type:
        raise EventError("payload has no 'type'")

    actor = _actor(payload)
    obj = payload.get("object") or {}
    target = payload.get("target") or {}
    if not isinstance(obj, dict) or not isinstance(target, dict):
        raise EventError("'object' and 'target' must be objects")

    # Join/Leave carry the conversation in 'object'; everything else in 'target'.
    room = target if target else obj
    room_token = str(room.get("id", ""))
    room_name = str(room.get("name", ""))
    if not room_token:
        raise EventError(f"no conversation token in a {event_type} event")

    message = raw_message = ""
    parameters: dict = {}
    message_id = 0
    reply_to_id = 0
    reaction = ""

    if event_type == "Create":
        message_id = _int(obj.get("id"))
        content = obj.get("content")
        if isinstance(content, str) and content:
            try:
                decoded = json.loads(content)
            except json.JSONDecodeError as exc:
                raise EventError(f"object.content is not valid JSON: {exc}") from exc
            if not isinstance(decoded, dict):
                raise EventError("object.content must decode to an object")
            raw_message = str(decoded.get("message", ""))
            params = decoded.get("parameters")
            parameters = params if isinstance(params, dict) else {}
            message = render_message(raw_message, parameters)
        in_reply_to = obj.get("inReplyTo")
        if isinstance(in_reply_to, dict):
            reply_to_id = _int(in_reply_to.get("id"))
    elif event_type == "Like":
        message_id = _int(obj.get("id"))
        reaction = str(payload.get("content", ""))
    elif event_type == "Undo":
        # object is the Like being undone; the message sits one level deeper.
        like = obj.get("object")
        if isinstance(like, dict):
            message_id = _int(like.get("id"))
        reaction = str(obj.get("content", ""))

    return TalkEvent(
        type=event_type,
        actor=actor,
        room_token=room_token,
        room_name=room_name,
        backend=backend,
        message_id=message_id,
        message=message,
        raw_message=raw_message,
        parameters=parameters,
        reply_to_id=reply_to_id,
        reaction=reaction,
        raw=payload,
    )
