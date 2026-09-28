from __future__ import annotations

import hashlib
import hmac

from sable.signing import RANDOM_BYTES, digest, sign, verify, verify_any

SECRET = "s" * 40
#: A rotation in progress: the value Talk was given last, and the one before it.
CURRENT = "n" * 40
PREVIOUS = "o" * 40


def test_digest_matches_the_documented_construction() -> None:
    # Nextcloud: hash_hmac('sha256', $random . $body, $secret)
    random, body = "abc123", b'{"type":"Create"}'
    expected = hmac.new(
        SECRET.encode(), random.encode() + body, hashlib.sha256
    ).hexdigest()
    assert digest(random, body, SECRET) == expected


def test_verify_accepts_a_good_signature() -> None:
    body = b'{"type":"Create"}'
    random = "deadbeef"
    assert verify(random, digest(random, body, SECRET), body, SECRET)


def test_verify_is_case_insensitive_about_the_hex() -> None:
    body = b"payload"
    random = "abc"
    assert verify(random, digest(random, body, SECRET).upper(), body, SECRET)


def test_verify_rejects_a_tampered_body() -> None:
    random = "abc"
    signature = digest(random, b"original", SECRET)
    assert not verify(random, signature, b"tampered", SECRET)


def test_verify_rejects_a_wrong_secret_random_or_empty_header() -> None:
    body = b"payload"
    random = "abc"
    signature = digest(random, body, SECRET)
    assert not verify(random, signature, body, "x" * 40)
    assert not verify("different", signature, body, SECRET)
    assert not verify("", signature, body, SECRET)
    assert not verify(random, "", body, SECRET)


def test_sign_produces_enough_randomness_and_a_verifiable_digest() -> None:
    random, signature = sign("hello world", SECRET)
    assert len(random) == RANDOM_BYTES * 2
    assert len(signature) == 64
    assert verify(random, signature, b"hello world", SECRET)


def test_sign_is_not_deterministic() -> None:
    assert sign("x", SECRET)[0] != sign("x", SECRET)[0]


# --------------------------------------------------------------------------- #
# Verifying against more than one secret, for the rotation window
# --------------------------------------------------------------------------- #


def test_verify_any_accepts_a_signature_made_with_either_secret() -> None:
    body, random = b'{"type":"Create"}', "abc123"
    for secret in (CURRENT, PREVIOUS):
        signature = digest(random, body, secret)
        assert verify_any(random, signature, body, (CURRENT, PREVIOUS))


def test_verify_any_rejects_a_secret_that_is_not_in_the_list() -> None:
    body, random = b"payload", "abc"
    signature = digest(random, body, "z" * 40)
    assert not verify_any(random, signature, body, (CURRENT, PREVIOUS))


def test_verify_any_with_one_secret_is_verify() -> None:
    """The ordinary case, a rotation that is over: one entry, same answer."""
    body, random = b"payload", "abc"
    signature = digest(random, body, CURRENT)
    assert verify_any(random, signature, body, (CURRENT,))
    assert not verify_any(random, signature, body, (PREVIOUS,))


def test_verify_any_rejects_everything_when_there_are_no_secrets() -> None:
    body, random = b"payload", "abc"
    assert not verify_any(random, digest(random, body, CURRENT), body, ())


def test_verify_any_still_rejects_a_tampered_body_and_an_empty_header() -> None:
    random = "abc"
    secrets = (CURRENT, PREVIOUS)
    assert not verify_any(random, digest(random, b"original", PREVIOUS), b"tampered", secrets)
    assert not verify_any(random, "", b"original", secrets)
    assert not verify_any("", digest(random, b"original", CURRENT), b"original", secrets)
