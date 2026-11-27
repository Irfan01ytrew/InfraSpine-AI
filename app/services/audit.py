"""Audit trail for security-relevant actions. Callers must never pass secrets in `details`."""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


@dataclass(frozen=True)
class AuditContext:
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    ip_address: str | None
    request_id: str | None


def record(
    db: AsyncSession,
    ctx: AuditContext,
    action: str,
    *,
    outcome: str = "success",
    target: str | None = None,
    details: dict | None = None,
) -> None:
    """Add an audit entry to the current transaction; it commits (or rolls back) with it."""
    db.add(
        AuditLog(
            actor_user_id=ctx.actor_user_id,
            actor_email=ctx.actor_email,
            action=action,
            target=target,
            outcome=outcome,
            details=details or {},
            ip_address=ctx.ip_address,
            request_id=ctx.request_id,
        )
    )


async def record_failure_now(
    db: AsyncSession, ctx: AuditContext, action: str, *, target: str | None, details: dict
) -> None:
    """Persist a failure immediately, independent of the request's (failed) business change."""
    await db.rollback()
    record(db, ctx, action, outcome="failure", target=target, details=details)
    await db.commit()
