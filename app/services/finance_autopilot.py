"""Finance autopilot worker (Step R slice 2): run the sweep on the worker schedule.

The sweep is NOT a second execution path: it only starts the existing invoice
workflow for waiting batches (and wakes runs stalled on an expired approval) using
the same ``FinanceService.start`` / unified engine every human caller uses. It runs
in the document worker's ``--once`` pass (so a cron can call it) and in the
long-running worker loop, both gated on the workflow-engine kill switch
(``WORKFLOW_ENGINE_WORKER_ENABLED``) and on the org-settings policy
(``finance_autopilot_enabled``).

The sweep acts as a real user: the earliest-created active user who holds
``invoice.ingest`` through an active membership and holds NEITHER
``payment.approve`` NOR ``funds.transfer`` anywhere. If no such user exists the
pass records ``finance.autopilot_no_actor`` in the audit log and does nothing, so
an administrator can see the sweep is configured but has nobody to run as. The
sweep itself never creates a payment, requests or decides an approval, or calls a
bank connector (see ``app.finance.autopilot``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import AuditLog, User
from app.finance.autopilot import load_policy, sweep
from app.platform.models import PlatformMembership, PlatformRolePermission
from app.services.audit import AuditContext, record

logger = logging.getLogger(__name__)
AUTOPILOT_POLL_SECONDS = 300
_WORKER = AuditContext(None, "finance-autopilot-worker", None, None)
# Duties the sweep actor must never hold: even a bug in the sweep could then
# approve or pay. The sweep never calls those paths, and this is the backstop.
FORBIDDEN_DUTIES = ("payment.approve", "funds.transfer")


def _active_membership(now: datetime):
    """Same active-window rule as ``app.platform.access._active_membership``."""
    return and_(
        PlatformMembership.revoked_at.is_(None),
        or_(PlatformMembership.valid_from.is_(None), PlatformMembership.valid_from <= now),
        or_(PlatformMembership.valid_to.is_(None), PlatformMembership.valid_to > now),
    )


async def sweep_actor(db: AsyncSession, *, now: datetime | None = None) -> User | None:
    """Earliest-created active user who may ingest invoices and holds no forbidden duty."""
    current = now or datetime.now(UTC)
    holders = (
        select(PlatformMembership.user_id)
        .join(PlatformRolePermission, PlatformRolePermission.role_id == PlatformMembership.role_id)
        .where(PlatformRolePermission.permission == "invoice.ingest", _active_membership(current))
        .distinct()
    )
    forbidden = (
        select(PlatformMembership.user_id)
        .join(PlatformRolePermission, PlatformRolePermission.role_id == PlatformMembership.role_id)
        .where(PlatformRolePermission.permission.in_(FORBIDDEN_DUTIES), _active_membership(current))
        .distinct()
    )
    return await db.scalar(
        select(User)
        .where(User.id.in_(holders), ~User.id.in_(forbidden), User.is_active.is_(True))
        .order_by(User.created_at, User.id)
        .limit(1)
    )


async def last_sweep(db: AsyncSession) -> AuditLog | None:
    """Most recent autopilot sweep audit event, for the admin status page."""
    return await db.scalar(
        select(AuditLog)
        .where(AuditLog.action == "finance.autopilot_sweep")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )


async def process_finance_autopilot(
    sessionmaker: async_sessionmaker[AsyncSession], *, settings: Settings, cipher=None,
) -> int:
    """One scheduled autopilot pass. Returns the number of sweep decisions made
    (0 when the policy is disabled, there is no actor, or nothing was waiting)."""
    async with sessionmaker() as db:
        policy = await load_policy(db)
        if not policy.enabled:
            return 0
        actor = await sweep_actor(db)
        if actor is None:
            record(db, _WORKER, "finance.autopilot_no_actor", target="autopilot", details={
                "reason": "no active user holds invoice.ingest without payment.approve/funds.transfer; "
                          "grant finance.clerk to a user to let the sweep run"})
            await db.commit()
            logger.warning("Finance autopilot enabled but no sweep actor exists; nothing started")
            return 0
        decisions = await sweep(db, actor, settings, policy, cipher=cipher)
        if decisions:
            logger.info("Finance autopilot: %s decisions as %s", len(decisions), actor.email)
        return len(decisions)


async def run_finance_autopilot(
    sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event, *, settings: Settings,
    cipher=None, interval_seconds: float = AUTOPILOT_POLL_SECONDS,
) -> None:
    """Background scheduler; retries on the next tick and logs only exception types."""
    while not stop.is_set():
        try:
            await process_finance_autopilot(sessionmaker, settings=settings, cipher=cipher)
        except Exception as exc:
            logger.warning("Finance autopilot scheduler failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
