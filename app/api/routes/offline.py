"""Offline replay support (Step U slice 3).

``GET /api/offline/replay/{key}`` returns the original result recorded for an
``Idempotency-Key`` — the response the server produced the first time that key was
used. It exists for the case the offline queue cares about most: the client sent
the request, the server executed it, and the response was lost in transit when
the connection dropped. Instead of re-posting blindly, the client can ask here
first and take the stored result.

Records are scoped per user: another user's key is a 404, exactly like an unknown
key, so keys can never leak results across users. The raw key is never stored
(only its SHA-256 digest), and the endpoint only reads — it never executes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession, get_current_user
from app.core.errors import NotFoundError
from app.platform.idempotency import lookup_record, validate_idempotency_key

router = APIRouter(prefix="/api/offline", tags=["offline"], dependencies=[Depends(get_current_user)])


class ReplayRecordOut(BaseModel):
    idempotency_key: str
    endpoint: str
    request_hash: str
    status: str
    response_status: int | None
    response_body: dict | None
    created_at: datetime
    completed_at: datetime | None


@router.get("/replay/{key}", response_model=ReplayRecordOut)
async def replay_record(
    key: Annotated[str, Path(min_length=1, max_length=256)],
    db: DbSession,
    user: CurrentUser,
) -> ReplayRecordOut:
    """The recorded result for one of the caller's own idempotency keys."""
    cleaned = validate_idempotency_key(key)
    assert cleaned is not None  # a path parameter is always present
    record = await lookup_record(db, user_id=user.id, key=cleaned)
    if record is None:
        raise NotFoundError("No recorded result for this idempotency key.")
    return ReplayRecordOut(
        idempotency_key=cleaned,
        endpoint=record.endpoint,
        request_hash=record.request_hash,
        status=record.status,
        response_status=record.response_status,
        response_body=record.response_body,
        created_at=record.created_at,
        completed_at=record.completed_at,
    )
