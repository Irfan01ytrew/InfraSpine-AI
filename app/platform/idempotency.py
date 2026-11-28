"""Idempotency-Key support for lightweight HTTP creates (Step U slice 3).

The offline queue replays a POST with the same ``Idempotency-Key`` when the device
comes back online. Without server-side dedupe the replay would run the request a
second time: a second LLM call for a knowledge question, a second saved message in a
conversation, a second workflow draft. This module is the generic ledger those
endpoints share.

How it works
------------
- The raw key is never stored, only its SHA-256 digest (same pattern as Agent
  Studio runs, which store a digest on the run row). Keys are scoped per user:
  one user's key can never return another user's result.
- The key is bound to the exact request via a SHA-256 of the canonical request
  JSON. Same key + different request is a 409 (the key was reused for a different
  action — a client bug, and the offline queue drops it with a visible notice).
- Same key + same request while the first request is still running returns 429
  (the queue treats 429 as retryable, so the entry is retried on the next online
  event instead of being dropped). A pending row older than ``PENDING_STALE`` is
  reclaimed — the first attempt must have crashed before recording its result.
- A completed row stores the original response (status + body). A replay returns
  that body without executing again. ``GET /api/offline/replay/{key}`` exposes the
  same record so a client that never saw the response (lost in transit) can fetch
  the original result without re-posting.

This is deliberately separate from ``execute_once`` / ``PlatformActionExecution``:
that ledger is for privileged, approval-gated dispatches with read-back
verification against external systems. These creates are synchronous, user-scoped
and need no duty or approval — a lighter ledger is the right tool.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, InvalidInputError
from app.platform.models import IdempotencyRecord

#: Longest key the ledger accepts (matches the ``Idempotency-Key`` header limit
#: used by Agent Studio runs).
MAX_KEY_LENGTH = 256
#: A pending row older than this is assumed to be a crashed first attempt and may
#: be reclaimed by a replay.
PENDING_STALE = timedelta(minutes=10)
#: Completed/failed rows older than this may be pruned by ``prune_records``.
RETENTION_DAYS = 90


class IdempotencyInProgressError(AppError):
    """The same key + request is already executing. Retry later (429, retryable)."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "idempotency_in_progress"
    message = "This request is already running. Try again in a moment."


def key_digest(key: str) -> str:
    """SHA-256 hex of the raw key. The raw key is never persisted."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def canonical_request_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonical request JSON (sorted keys, compact separators)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_idempotency_key(key: str | None) -> str | None:
    """Return the stripped key, or None when the client sent no key.

    A key is optional on every endpoint: requests without one execute exactly as
    before (no dedupe). An empty or over-long key is a 422, never silently ignored.
    """
    if key is None:
        return None
    cleaned = key.strip()
    if not cleaned or len(cleaned) > MAX_KEY_LENGTH:
        raise InvalidInputError("Idempotency-Key must be 1 to 256 characters.")
    return cleaned


@dataclass(frozen=True)
class ClaimOutcome:
    """Result of :func:`claim_request`."""

    record: IdempotencyRecord
    replayed: bool
    response_status: int | None = None
    response_body: dict[str, Any] | None = None


def _is_fresh(row: IdempotencyRecord, *, now: datetime) -> bool:
    return row.created_at is not None and now - row.created_at < PENDING_STALE


async def claim_request(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    key: str,
    endpoint: str,
    request_hash: str,
) -> ClaimOutcome:
    """Claim a key for this request, or return the stored result on a replay.

    The claim is a single insert committed immediately, so two concurrent callers
    cannot both execute: the loser reads the winner's row (locked) and either gets
    the stored result, a 409 (different request), a 429 (still running) or a
    reclaim (stale pending / failed first attempt).
    """
    digest = key_digest(key)
    now = datetime.now(UTC)
    row = IdempotencyRecord(
        user_id=user_id,
        key_digest=digest,
        endpoint=endpoint,
        request_hash=request_hash,
        status="pending",
    )
    # The claim insert runs in a SAVEPOINT, not the outer transaction: on a
    # key conflict only the savepoint rolls back. A plain rollback here would
    # expire every object the caller already loaded on this session (notably
    # the authenticated user), and the next attribute read on one of them
    # raises MissingGreenlet. The outer transaction stays usable, and the
    # pending row is committed immediately so a concurrent caller sees it.
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        pass  # savepoint rolled back; fall through to the conflict path
    else:
        await db.commit()
        return ClaimOutcome(record=row, replayed=False)

    existing = await db.scalar(
        select(IdempotencyRecord)
        .where(IdempotencyRecord.user_id == user_id, IdempotencyRecord.key_digest == digest)
        .with_for_update()
    )
    if existing is None:  # deleted concurrently; claim once more
        return await claim_request(db, user_id=user_id, key=key, endpoint=endpoint, request_hash=request_hash)
    if existing.request_hash != request_hash:
        await db.rollback()
        raise ConflictError("This idempotency key was already used for a different request.")
    if existing.status == "completed":
        # Read the stored result BEFORE the rollback: rollback() expires the
        # row, and reading an expired attribute afterwards raises
        # MissingGreenlet — the sync attribute loader has no greenlet in this
        # async context. (This broke every replay of a completed request:
        # rag/chat, conversations, workflow creates/versions, draft-from-text
        # all 500'd on replay until this read moved above the rollback.)
        response_status = existing.response_status
        response_body = existing.response_body
        # No rollback here: the caller returns the stored result immediately,
        # and the row lock releases when the request's transaction ends.
        # Rolling back would only expire the caller's loaded objects.
        return ClaimOutcome(
            record=existing,
            replayed=True,
            response_status=response_status,
            response_body=response_body,
        )
    if existing.status == "pending" and _is_fresh(existing, now=now):
        await db.rollback()
        raise IdempotencyInProgressError()
    # Stale pending (crashed first attempt) or failed: reclaim the row.
    existing.endpoint = endpoint
    existing.request_hash = request_hash
    existing.status = "pending"
    existing.response_status = None
    existing.response_body = None
    existing.created_at = now
    existing.completed_at = None
    await db.commit()
    return ClaimOutcome(record=existing, replayed=False)


async def complete_request(
    db: AsyncSession,
    record: IdempotencyRecord,
    *,
    response_status: int,
    response_body: dict[str, Any],
) -> None:
    """Store the original response so a replay returns it without executing."""
    # Normalize to plain JSON first, with FastAPI's own encoder: callers may
    # pass row dicts carrying UUIDs and datetimes (workflow creates/versions),
    # which the JSONB column cannot store ("Object of type UUID is not JSON
    # serializable"). jsonable_encoder formats them exactly as the client
    # originally received them (UUIDs as strings, datetimes as ISO 8601), so
    # a replay returns a body identical to the first response — a plain
    # str() fallback would not (datetimes come back space-separated).
    stored_body = jsonable_encoder(response_body)
    record.status = "completed"
    record.response_status = response_status
    record.response_body = stored_body
    record.completed_at = datetime.now(UTC)
    await db.commit()


async def fail_request(db: AsyncSession, record: IdempotencyRecord) -> None:
    """Mark a claimed row failed so a later replay may reclaim it.

    The failed attempt produced no stored result: the client already received the
    error, and a replay re-executes instead of returning a stale failure.
    """
    record.status = "failed"
    record.response_status = None
    record.response_body = None
    await db.commit()


async def lookup_record(
    db: AsyncSession, *, user_id: uuid.UUID, key: str
) -> IdempotencyRecord | None:
    """The caller's own record for a key, or None (unknown key — or another user's)."""
    return await db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id, IdempotencyRecord.key_digest == key_digest(key)
        )
    )


async def prune_records(db: AsyncSession, *, older_than_days: int = RETENTION_DAYS) -> int:
    """Delete completed/failed rows older than the retention window.

    Pending rows are never pruned: a pending row is the only proof a request is in
    flight, and deleting it could let a late replay execute twice. No scheduler
    calls this yet; it exists for a future maintenance job.
    """
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    result = await db.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.status.in_(("completed", "failed")),
            IdempotencyRecord.created_at < cutoff,
        )
    )
    await db.commit()
    return result.rowcount or 0
