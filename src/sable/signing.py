"""HMAC-SHA256 request signing, both directions.

Nextcloud Talk signs the webhooks it sends us over
``X-Nextcloud-Talk-Random`` + the raw request body, and expects the requests we
send back to be signed over ``X-Nextcloud-Talk-Bot-Random`` + a single
endpoint-specific value (the message text, the reaction emoji, or the
conversation token). Both use the bot's shared secret.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Iterable

#: Headers Nextcloud sets on an incoming webhook.
HEADER_SIGNATURE = "X-Nextcloud-Talk-Signature"
HEADER_RANDOM = "X-Nextcloud-Talk-Random"
HEADER_BACKEND = "X-Nextcloud-Talk-Backend"

#: Headers we set when calling the bot API.
HEADER_BOT_SIGNATURE = "X-Nextcloud-Talk-Bot-Signature"
HEADER_BOT_RANDOM = "X-Nextcloud-Talk-Bot-Random"

#: Nextcloud requires at least 32 bytes of randomness per request.
RANDOM_BYTES = 32


def digest(random: str, data: bytes, secret: str) -> str:
    """HMAC-SHA256 hex digest over ``random`` + ``data``."""
    return hmac.new(
        secret.encode("utf-8"),
        random.encode("utf-8") + data,
        hashlib.sha256,
    ).hexdigest()


def verify(random: str, signature: str, body: bytes, secret: str) -> bool:
    """Constant-time check of an incoming webhook signature."""
    if not random or not signature:
        return False
    return hmac.compare_digest(digest(random, body, secret), signature.strip().lower())


def verify_any(random: str, signature: str, body: bytes, candidates: Iterable[str]) -> bool:
    """Verify an incoming signature against several secrets, stopping at the first.

    For the rotation window: Talk holds one secret per bot install, so replacing
    it means a reinstall, and events signed with the old value keep arriving for
    as long as it takes. ``Config.inbound_secrets`` is that list, current secret
    first.

    Incoming only. :func:`sign` still takes one secret, and everything sable
    sends is signed with the current one - Talk has been given the new value by
    then, so a call signed with the old one would be refused.

    Every comparison goes through :func:`verify`, so each is constant-time in the
    digest. Trying a second secret costs a second HMAC of the body, which is why
    the list is short and ordered: a rotation that is over leaves one entry.
    """
    return any(verify(random, signature, body, secret) for secret in candidates)


def sign(data: str, secret: str) -> tuple[str, str]:
    """Sign one outgoing value, returning ``(random, signature)``."""
    random = secrets.token_hex(RANDOM_BYTES)
    return random, digest(random, data.encode("utf-8"), secret)
