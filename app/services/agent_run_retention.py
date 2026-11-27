"""Explicit, bounded redaction of expired agent-run task/result content."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentProfileRun
from app.services import audit
from app.services.audit import AuditContext

MIN_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650
MAX_PURGE_BATCH = 1000


def retention_cutoff(retention_days: int, *, now: datetime | None = None) -> datetime:
    """Calculate a UTC cutoff without allowing a deployment to choose a zero-day policy."""
    if not MIN_RETENTION_DAYS <= retention_days <= MAX_RETENTION_DAYS:
        raise ValueError(f"retention_days must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}.")
    point = now or datetime.now(UTC)
    if point.tzinfo is None:
        point = point.replace(tzinfo=UTC)
    else:
        point = point.astimezone(UTC)
    return point - timedelta(days=retention_days)


def eligible_run_content_ids(cutoff: datetime, *, limit: int):
    if not 1 <= limit <= MAX_PURGE_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_PURGE_BATCH}.")
    return (
        select(AgentProfileRun.id)
        .where(
            AgentProfileRun.requested_at < cutoff,
            AgentProfileRun.status.in_(("completed", "failed")),
            AgentProfileRun.content_purged_at.is_(None),
            AgentProfileRun.task_ciphertext.is_not(None),
        )
        .order_by(AgentProfileRun.requested_at, AgentProfileRun.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def count_eligible_run_content(session: AsyncSession, cutoff: datetime) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(AgentProfileRun)
        .where(
            AgentProfileRun.requested_at < cutoff,
            AgentProfileRun.status.in_(("completed", "failed")),
            AgentProfileRun.content_purged_at.is_(None),
            AgentProfileRun.task_ciphertext.is_not(None),
        )
    )
    return int(count or 0)


async def purge_expired_run_content(
    session: AsyncSession,
    context: AuditContext,
    *,
    retention_days: int,
    limit: int,
    now: datetime | None = None,
) -> tuple[int, datetime]:
    """Redact one confirmed batch; retain status, timing, profile and append-only audit metadata."""
    cutoff = retention_cutoff(retention_days, now=now)
    selected = await session.execute(eligible_run_content_ids(cutoff, limit=limit))
    run_ids = list(selected.scalars().all())
    purged_at = now or datetime.now(UTC)
    if purged_at.tzinfo is None:
        purged_at = purged_at.replace(tzinfo=UTC)
    else:
        purged_at = purged_at.astimezone(UTC)

    if run_ids:
        await session.execute(
            update(AgentProfileRun)
            .where(AgentProfileRun.id.in_(run_ids), AgentProfileRun.content_purged_at.is_(None))
            .values(
                task_ciphertext=None,
                task_nonce=None,
                task_key_id=None,
                result_ciphertext=None,
                result_nonce=None,
                result_key_id=None,
                content_purged_at=purged_at,
            )
        )
    audit.record(
        session,
        context,
        "agent.profile_run_content_purged",
        target="agent_profile_runs",
        details={
            "retention_days": retention_days,
            "purged_count": len(run_ids),
            "batch_limit": limit,
            "cutoff_at": cutoff.isoformat(),
        },
    )
    await session.commit()
    return len(run_ids), cutoff
