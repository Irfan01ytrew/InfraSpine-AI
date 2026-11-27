"""Transactional outbox and audit helpers. Events are written in the caller's transaction and dispatched later."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

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
    """Deliver due events to one handler (tests, one-off tools). Same claiming, savepoint, backoff and dead-letter
    rules as the background dispatcher in app.platform.outbox."""
    from app.platform.outbox import deliver_batch

    async def only(_db, row):
        await handler(row)

    types = await db.scalars(select(PlatformOutbox.event_type).where(PlatformOutbox.dispatched_at.is_(None)).distinct())
    counts = await deliver_batch(db, {event_type: [only] for event_type in types}, limit=limit)
    return counts["delivered"]
