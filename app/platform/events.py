"""Transactional outbox and audit helpers. Events are written in the caller's transaction and dispatched later."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.models import PlatformOutbox
from app.services import audit as audit_log
from app.services.audit import AuditContext


async def publish(db: AsyncSession, event_type: str, scope_id: uuid.UUID | None, payload: dict,
                  trace_id: uuid.UUID) -> None:
    db.add(PlatformOutbox(event_type=event_type, scope_id=scope_id, payload=payload, trace_id=trace_id))


def audit(db: AsyncSession, principal, event: str, *, target: str | None = None, details: dict | None = None,
          outcome: str = "success") -> None:
    ctx = AuditContext(principal.user_id, principal.email, None, str(principal.trace_id))
    audit_log.record(db, ctx, event, outcome=outcome, target=target, details={
        "trace_id": str(principal.trace_id), "channel": principal.channel, "principal_kind": principal.kind,
        **({"agent_id": str(principal.agent_id)} if principal.agent_id else {}), **(details or {}),
    })


async def drain_outbox(db: AsyncSession, handler: Callable[[PlatformOutbox], Awaitable[None]], *, limit: int = 100) -> int:
    """Deliver pending events in order. Rows are locked with SKIP LOCKED so several workers can drain safely;
    a failing handler leaves its event pending with the error recorded."""
    rows = list(await db.scalars(
        select(PlatformOutbox).where(PlatformOutbox.dispatched_at.is_(None))
        .order_by(PlatformOutbox.id).limit(limit).with_for_update(skip_locked=True)
    ))
    delivered = 0
    for row in rows:
        row.attempts += 1
        try:
            await handler(row)
        except Exception as exc:  # handler failures must not lose the event
            row.last_error = type(exc).__name__[:200]
            continue
        row.dispatched_at = datetime.now(UTC)
        row.last_error = None
        delivered += 1
    await db.commit()
    return delivered
