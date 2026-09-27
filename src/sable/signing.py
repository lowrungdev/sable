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


def sign(data: str, secret: str) -> tuple[str, str]:
    """Sign one outgoing value, returning ``(random, signature)``."""
    random = secrets.token_hex(RANDOM_BYTES)
    return random, digest(random, data.encode("utf-8"), secret)
