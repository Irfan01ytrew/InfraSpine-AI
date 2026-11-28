"""Pure tests for the Step U slice 3 idempotency helper. No database: run with --noconftest."""

import uuid

import pytest

from app.core.errors import InvalidInputError
from app.platform.idempotency import (
    IdempotencyInProgressError,
    canonical_request_hash,
    key_digest,
    validate_idempotency_key,
)


def test_canonical_request_hash_ignores_key_order():
    a = canonical_request_hash({"endpoint": "rag_chat", "question": "hi", "n": 1})
    b = canonical_request_hash({"n": 1, "question": "hi", "endpoint": "rag_chat"})
    assert a == b
    assert len(a) == 64


def test_canonical_request_hash_changes_with_the_request():
    base = {"endpoint": "rag_chat", "question": "hi"}
    assert canonical_request_hash(base) != canonical_request_hash({**base, "question": "bye"})
    assert canonical_request_hash(base) != canonical_request_hash({**base, "endpoint": "other"})


def test_canonical_request_hash_handles_uuid_values():
    uid = uuid.uuid4()
    assert canonical_request_hash({"id": uid}) == canonical_request_hash({"id": str(uid)})


def test_key_digest_is_a_digest_not_the_key():
    key = "offline-replay-key-123"
    digest = key_digest(key)
    assert digest != key
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert key_digest(key) == digest


def test_validate_idempotency_key():
    assert validate_idempotency_key(None) is None
    assert validate_idempotency_key("  abc-123  ") == "abc-123"
    assert validate_idempotency_key("x" * 256) == "x" * 256
    with pytest.raises(InvalidInputError):
        validate_idempotency_key("")
    with pytest.raises(InvalidInputError):
        validate_idempotency_key("   ")
    with pytest.raises(InvalidInputError):
        validate_idempotency_key("x" * 257)


def test_in_progress_error_is_retryable_429():
    assert IdempotencyInProgressError.status_code == 429
