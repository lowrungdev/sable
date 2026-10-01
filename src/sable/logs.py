"""Keeping the log worth reading.

The container healthcheck asks ``GET /healthz`` every thirty seconds, and
uvicorn logs each one. That is about 2,900 identical lines a day, which is
enough to hide anything that matters between them.
"""

from __future__ import annotations

import logging

#: uvicorn's access records carry the request as arguments rather than as
#: formatted text: (client, method, full path, http version, status).
_PATH, _STATUS = 2, 4


class HealthCheckFilter(logging.Filter):
    """Drop access lines for a probe that answered normally.

    Only the successful ones. A probe that starts failing - 401 once a health
    token is set - is exactly the line you want,
    and it is the reason this filters rather than turning the access log off.
    """

    def __init__(self, path: str = "/healthz") -> None:
        super().__init__()
        self.path = path

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) <= _STATUS:
            # Not an access record, or uvicorn changed its shape. Either way,
            # letting a line through is the safe direction to be wrong in.
            return True
        try:
            status = int(args[_STATUS])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return True
        path = str(args[_PATH]).split("?", 1)[0]
        return not (path == self.path and 200 <= status < 300)


def quiet_health_checks(path: str = "/healthz") -> None:
    """Stop uvicorn logging a line for every successful probe."""
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter(path))
