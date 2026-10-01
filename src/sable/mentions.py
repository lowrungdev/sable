"""Keeping text that came from outside from pinging a whole room.

sable posts words it did not write: a model's answer to whatever somebody typed,
an error that quotes a server's reply, a webhook's payload. Talk turns ``@all``
into a notification for every participant, and ``@"group/..."`` or
``@"team/..."`` into one for every member of that group or team, so anyone who
can get a sentence into one of those paths could page the room through the
account. :func:`defang_mentions` breaks the mass forms and leaves ordinary
mentions of one person alone.

Talk's docs (chat page, mention autocomplete) say an id with a space or slash is
written ``@"space user"`` / ``@"guest/random-string"``, and list the sources
``users``, ``federated_users``, ``group``, ``guests`` and ``calls`` (the whole
conversation). They do not spell out ``@all`` or the team form: those are what
Talk's own clients send, so they are covered on that basis.
"""

from __future__ import annotations

import re

#: Zero-width space. Between ``@`` and the name, the text no longer parses as a
#: mention but looks the same.
ZWSP = "\u200b"

#: The mass forms: ``@all`` (also quoted), and a quoted ``group/`` or ``team/``
#: id, singular or plural, which is how Talk writes a group or team mention.
#: The ``@`` must not follow a word character, so an email address stays intact,
#: and ``@all`` must end there: ``@all-hands`` is a user called that.
_MASS_RE = re.compile(
    r"""(?<![\w@])@(?=
        "?all"?(?![\w@-]|\.\w)
      | "(?:groups?|teams?)/
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def defang_mentions(text: str) -> str:
    """Neutralise ``@all`` and group or team mentions in ``text``."""
    return _MASS_RE.sub("@" + ZWSP, text)
