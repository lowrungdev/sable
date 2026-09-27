"""Reachability tracking for the two services sable depends on.

Every failed call already logs itself where it happened. What an operator needs
on top of that is the *moment* reachability changed - one line when a service
goes away and one when it comes back - rather than a line per attempt for as
long as the outage lasts.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class ConnectionState:
    """Logs the transitions between reachable and unreachable.

    Only transport failures count as unreachable. An HTTP error response means
    the service answered, so it is reachable even when it is unhappy - those are
    logged by the caller with their status code.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        #: None until the first call: nothing is known before then.
        self._up: bool | None = None

    @property
    def up(self) -> bool | None:
        return self._up

    def record_success(self) -> None:
        if self._up is False:
            log.info("%s is reachable again", self.name)
        else:
            log.debug("%s answered", self.name)
        self._up = True

    def record_failure(self, exc: BaseException) -> None:
        if self._up is not False:
            log.error("lost connection to %s: %s: %s", self.name, type(exc).__name__, exc)
        else:
            log.debug("%s still unreachable: %s", self.name, exc)
        self._up = False
