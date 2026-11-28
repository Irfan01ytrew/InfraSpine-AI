"""Finance approval reminders (Step R slice 3): nudge the approver when a payment approval is created or about to expire.

A finance payment approval is a platform approval (``action_type='funds.transfer'``,
``required_permission='payment.approve'``) requested by the invoice workflow when a payment group is ready.
Nothing in the app tells the approver it exists except the approvals inbox, so a payment can sit unnoticed
until its approval expires and the batch stalls. This worker polls for pending finance payment approvals and
writes an in-app notice (``wf_notices``, the same table and ``approval_required`` kind the workflow engine
uses for approval steps) to every person who may decide the approval right now.

Safety: a reminder never decides, expires, cancels or otherwise changes an approval, a payment or a run.
It only inserts notices and one audit event per reminded approval. The notice table's unique
``(step_id, recipient_id, kind)`` key makes each kind fire at most once per step and recipient, so a
re-requested approval for the same waiting step does not re-notify someone who was already nudged;
a newly eligible approver still gets their own notice. WhatsApp reminders are out of scope until Step F
lands (plan slice 3 note); the in-app notice is the channel today.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.finance_models import FinInvoiceBatch, FinPayment
from app.db.models import User
from app.db.workflow_engine_models import WorkflowNotice, WorkflowStep
from app.platform.access import SEPARATED_DUTIES
from app.platform.models import (
    PlatformActionExecution,
    PlatformApprovalRequest,
    PlatformMembership,
    PlatformRolePermission,
)
from app.services.audit import AuditContext, record

logger = logging.getLogger(__name__)
REMINDER_POLL_SECONDS = 300
# An approval expiring within this window gets the expiring-soon nudge instead of the created nudge.
EXPIRING_WITHIN = timedelta(hours=4)
FINANCE_APPROVAL_ACTION = "funds.transfer"
FINANCE_APPROVER_DUTY = "payment.approve"
_WORKER = AuditContext(None, "finance-approval-reminder-worker", None, None)


def _active_membership(now: datetime):
    """Same active-window rule as ``app.platform.access._active_membership``."""
    return and_(
        PlatformMembership.revoked_at.is_(None),
        or_(PlatformMembership.valid_from.is_(None), PlatformMembership.valid_from <= now),
        or_(PlatformMembership.valid_to.is_(None), PlatformMembership.valid_to > now),
    )


async def approval_recipients(db: AsyncSession, approval: PlatformApprovalRequest, *, now: datetime) -> list[uuid.UUID]:
    """Everyone who may decide this approval right now.

    Active users holding ``payment.approve`` in the approval's scope, excluding the requester (who may
    never decide their own request) and anyone who already performed another separated duty on the
    same subject (mirrors ``app.platform.approvals.assert_separation_of_duties``).
    """
    holders = (
        select(PlatformMembership.user_id)
        .join(PlatformRolePermission, PlatformRolePermission.role_id == PlatformMembership.role_id)
        .where(PlatformRolePermission.permission == FINANCE_APPROVER_DUTY, _active_membership(now))
        .distinct()
    )
    # Mirror app.platform.approvals.assert_separation_of_duties exactly: a holder is blocked only
    # when they already performed a DIFFERENT separated duty on this subject. Deciding another
    # payment approval on the same batch is the SAME duty, so it never blocks (the platform
    # rule subtracts the duty itself: `... & SEPARATED_DUTIES - {duty}`).
    blocked = set(await db.scalars(select(PlatformApprovalRequest.decided_by).where(
        PlatformApprovalRequest.subject == approval.subject,
        PlatformApprovalRequest.decided_by.is_not(None),
        PlatformApprovalRequest.status.in_(("approved", "consumed")),
        PlatformApprovalRequest.required_permission.in_(SEPARATED_DUTIES - {FINANCE_APPROVER_DUTY}),
    )))
    blocked |= set(await db.scalars(select(PlatformActionExecution.executor_user_id).where(
        PlatformActionExecution.subject == approval.subject,
        PlatformActionExecution.status != "failed",
        PlatformActionExecution.duty.in_(SEPARATED_DUTIES - {FINANCE_APPROVER_DUTY}),
    )))
    blocked.add(approval.requested_by)
    rows = await db.scalars(
        select(User.id)
        .where(User.id.in_(holders), User.is_active.is_(True))
        .order_by(User.created_at, User.id)
    )
    return [user_id for user_id in rows if user_id not in blocked]


async def _waiting_step(db: AsyncSession, payment: FinPayment) -> WorkflowStep | None:
    """The step of the batch's workflow run that is waiting on this approval, if any.

    The notice table keys on a ``wf_steps`` row, so without a waiting step there is nothing to
    attach the nudge to and the approval is left alone (the run is not waiting on it).
    """
    batch = await db.get(FinInvoiceBatch, payment.batch_id)
    if batch is None or batch.wf_run_id is None:
        return None
    return await db.scalar(
        select(WorkflowStep)
        .where(WorkflowStep.run_id == batch.wf_run_id, WorkflowStep.status == "waiting")
        .order_by(WorkflowStep.available_at, WorkflowStep.id)
        .limit(1)
    )


async def process_finance_approval_reminders(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime | None = None,
    expiring_within: timedelta = EXPIRING_WITHIN,
) -> int:
    """Send due finance approval reminders; returns how many in-app notices were newly sent.

    Each pending finance payment approval gets at most one ``approval_required`` notice (when first
    seen) and at most one ``approval_expiring`` notice (once it is inside the expiry window), per
    recipient. Two approvals waiting on the same workflow step share one notice for that step, so
    the count is notices sent, not approvals processed. Approvals that are already decided, expired,
    or have no waiting step or no eligible approver are left untouched. No approval, payment or
    run is ever modified.
    """
    current = now or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("The finance approval reminder worker requires a timezone-aware timestamp.")
    sent = 0
    async with sessionmaker() as db:
        pending = (await db.execute(
            select(PlatformApprovalRequest, FinPayment)
            .join(FinPayment, FinPayment.approval_id == PlatformApprovalRequest.id)
            .where(
                PlatformApprovalRequest.status == "pending",
                PlatformApprovalRequest.action_type == FINANCE_APPROVAL_ACTION,
                PlatformApprovalRequest.required_permission == FINANCE_APPROVER_DUTY,
                PlatformApprovalRequest.expires_at > current,
            )
            .order_by(PlatformApprovalRequest.created_at, PlatformApprovalRequest.id)
        )).all()
        for approval, payment in pending:
            recipients = await approval_recipients(db, approval, now=current)
            if not recipients:
                continue
            step = await _waiting_step(db, payment)
            if step is None:
                continue
            kind = "approval_expiring" if approval.expires_at - current <= expiring_within else "approval_required"
            inserted = (await db.execute(
                pg_insert(WorkflowNotice)
                .values([{"id": uuid.uuid4(), "scope_id": step.scope_id, "step_id": step.id,
                          "recipient_id": recipient, "kind": kind} for recipient in recipients])
                .on_conflict_do_nothing(index_elements=["step_id", "recipient_id", "kind"])
                .returning(WorkflowNotice.id)
            )).all()
            if not inserted:
                continue
            record(db, _WORKER, "finance.approval_reminder", target=str(approval.id), details={
                "payment_id": str(payment.id), "batch_id": str(payment.batch_id), "kind": kind,
                "recipient_count": len(inserted), "expires_at": approval.expires_at.isoformat()})
            logger.info("Finance approval reminder: %s notice for approval %s to %s recipient(s)",
                        kind, approval.id, len(inserted))
            sent += len(inserted)
        await db.commit()
    return sent


async def run_finance_approval_reminders(
    sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event, *,
    interval_seconds: float = REMINDER_POLL_SECONDS,
) -> None:
    """Background scheduler; retries on the next tick and logs only exception types."""
    while not stop.is_set():
        try:
            await process_finance_approval_reminders(sessionmaker)
        except Exception as exc:
            logger.warning("Finance approval reminder scheduler failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
