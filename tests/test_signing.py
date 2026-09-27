from __future__ import annotations

import hashlib
import hmac

from sable.signing import RANDOM_BYTES, digest, sign, verify

SECRET = "s" * 40


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
