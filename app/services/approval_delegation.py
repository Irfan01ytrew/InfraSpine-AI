"""Approval delegation while an approver is away, and in-app notifications for approval steps.

A person hands their approval-step decisions to a colleague for a date range (an administrator may set it up
for anyone). During that range the delegate decides the steps the delegator could decide, with the delegator's
approval authority; the decision records both people. Delegation is not passed on: a delegate's own delegations
do not cover what was delegated to them. The delegator keeps their own access and can still decide.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.config import get_settings
from app.db.approval_models import ApprovalDelegation, ApprovalEmail, RequestApprovalStep
from app.db.models import ROLE_ADMIN, DepartmentManager, ServiceRequest, ServiceRequestNotification, User
from app.platform.timezone import org_today, org_today_sql
from app.services import audit
from app.services.audit import AuditContext

MAX_DELEGATION_DAYS = 366


def _today():
    """Today in the organisation's time zone (default Asia/Karachi), as SQL for query builders without a session.
    Python-side checks use `await org_today(db)`; both follow org_settings.time_zone, not UTC days."""
    return org_today_sql()


def active_condition(on: date):
    return (ApprovalDelegation.is_active.is_(True), ApprovalDelegation.starts_on <= on, ApprovalDelegation.ends_on >= on)


def delegators_of(user_id: uuid.UUID, on: date | None = None):
    """SQL subquery: the people who have delegated their approvals to this user today."""
    return select(ApprovalDelegation.delegator_user_id).where(ApprovalDelegation.delegate_user_id == user_id, *active_condition(on or _today()))


async def active_delegators(db: AsyncSession, user_id: uuid.UUID, on: date | None = None) -> list[User]:
    on = on or await org_today(db)
    return list((await db.scalars(
        select(User).where(User.id.in_(delegators_of(user_id, on)), User.is_active.is_(True)).order_by(User.email)
    )).all())


async def step_recipients(db: AsyncSession, request: ServiceRequest, step: RequestApprovalStep, on: date | None = None) -> set[uuid.UUID]:
    """Everyone who can decide this step now — the approver(s) and their active delegates — except the requester."""
    if step.approver_kind == "position":
        approvers = set(await db.scalars(select(User.id).where(User.position_id == step.approver_position_id, User.is_active.is_(True))))
    elif step.approver_user_id is not None:
        approvers = {step.approver_user_id}
    elif request.department_group_id is not None:
        approvers = set(await db.scalars(
            select(User.id).join(DepartmentManager, DepartmentManager.manager_user_id == User.id)
            .where(DepartmentManager.group_id == request.department_group_id, User.is_active.is_(True))
        ))
    else:
        approvers = set()
    if approvers:
        approvers.update(await db.scalars(
            select(ApprovalDelegation.delegate_user_id).join(User, User.id == ApprovalDelegation.delegate_user_id)
            .where(ApprovalDelegation.delegator_user_id.in_(approvers), User.is_active.is_(True),
                   *active_condition(on or await org_today(db)))
        ))
    approvers.discard(request.requester_user_id)
    return approvers


_EMAIL_KIND = {"approval_step_pending": "pending", "approval_step_reminder": "reminder", "approval_step_escalated": "escalation"}


async def notify(db: AsyncSession, request: ServiceRequest, recipients: set[uuid.UUID], kind: str, now: datetime) -> int:
    """Create (or bring back to unread) in-app notifications, and queue emails when approval email is on."""
    recipients = set(recipients) - {request.requester_user_id}
    if not recipients:
        return 0
    if get_settings().approval_email_enabled:
        db.add_all([ApprovalEmail(request_id=request.id, recipient_user_id=r, kind=_EMAIL_KIND[kind], created_at=now) for r in recipients])
    statement = insert(ServiceRequestNotification).values([
        {"id": uuid.uuid4(), "request_id": request.id, "recipient_user_id": r, "notification_type": kind, "created_at": now}
        for r in sorted(recipients, key=str)
    ])
    await db.execute(statement.on_conflict_do_update(
        constraint="uq_service_request_notifications_request_recipient_type",
        set_={"created_at": statement.excluded.created_at, "read_at": None},
    ))
    return len(recipients)


async def notify_step(db: AsyncSession, request: ServiceRequest, step: RequestApprovalStep, kind: str, now: datetime) -> int:
    """Notify the people who can decide the step now (approvers and their active delegates)."""
    return await notify(db, request, await step_recipients(db, request, step, await org_today(db, now)), kind, now)


async def escalation_recipients(db: AsyncSession, request: ServiceRequest, step: RequestApprovalStep) -> set[uuid.UUID]:
    """The approvers' own managers; administrators when there are none (or the step had no named approver)."""
    if step.approver_kind == "position":
        approvers = select(User.id).where(User.position_id == step.approver_position_id, User.is_active.is_(True))
    elif step.approver_user_id is not None:
        approvers = select(User.id).where(User.id == step.approver_user_id)
    else:
        approvers = None
    managers: set[uuid.UUID] = set()
    if approvers is not None:
        managers = set(await db.scalars(
            select(User.id).where(User.id.in_(select(User.reports_to_id).where(User.id.in_(approvers))), User.is_active.is_(True))
        ))
    managers.discard(request.requester_user_id)
    if not managers:
        managers = set(await db.scalars(select(User.id).where(User.role == ROLE_ADMIN, User.is_active.is_(True))))
    return managers


class DelegationService:
    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx

    async def list(self) -> list[tuple[ApprovalDelegation, str, str]]:
        """Current and upcoming delegations: an administrator sees all; others those they gave or received."""
        delegator, delegate = User.__table__.alias("delegator"), User.__table__.alias("delegate")
        query = (
            select(ApprovalDelegation, delegator.c.email, delegate.c.email)
            .join(delegator, delegator.c.id == ApprovalDelegation.delegator_user_id)
            .join(delegate, delegate.c.id == ApprovalDelegation.delegate_user_id)
            .where(ApprovalDelegation.is_active.is_(True), ApprovalDelegation.ends_on >= _today())
        )
        if self.user.role != ROLE_ADMIN:
            query = query.where(or_(ApprovalDelegation.delegator_user_id == self.user.id, ApprovalDelegation.delegate_user_id == self.user.id))
        rows = await self.db.execute(query.order_by(ApprovalDelegation.starts_on, delegator.c.email))
        return [(row, a, b) for row, a, b in rows.all()]

    async def create(self, *, delegate_user_id: uuid.UUID, starts_on: date, ends_on: date, reason: str | None,
                     delegator_user_id: uuid.UUID | None = None) -> ApprovalDelegation:
        delegator_id = delegator_user_id or self.user.id
        if delegator_id != self.user.id and self.user.role != ROLE_ADMIN:
            raise PermissionDeniedError("Only administrators set up delegation for someone else.")
        if ends_on < starts_on:
            raise InvalidInputError("The delegation must end on or after it starts.")
        if ends_on < await org_today(self.db):
            raise InvalidInputError("The delegation period has already ended.")
        if (ends_on - starts_on).days >= MAX_DELEGATION_DAYS:
            raise InvalidInputError("A delegation can last at most a year.")
        if delegate_user_id == delegator_id:
            raise InvalidInputError("Choose someone else to approve for you.")
        delegator = await self.db.get(User, delegator_id)
        delegate = await self.db.get(User, delegate_user_id)
        if delegator is None or not delegator.is_active:
            raise NotFoundError("Person not found.")
        if delegate is None or not delegate.is_active:
            raise InvalidInputError("Choose an active person to approve for you.")
        overlap = await self.db.scalar(select(ApprovalDelegation.id).where(
            ApprovalDelegation.delegator_user_id == delegator_id, ApprovalDelegation.is_active.is_(True),
            ApprovalDelegation.starts_on <= ends_on, ApprovalDelegation.ends_on >= starts_on,
        ).limit(1))
        if overlap is not None:
            raise InvalidInputError("A delegation for these dates already exists; end it first.")
        clean = " ".join((reason or "").split())[:200] or None
        row = ApprovalDelegation(delegator_user_id=delegator_id, delegate_user_id=delegate_user_id, starts_on=starts_on,
                                 ends_on=ends_on, reason=clean, created_by=self.user.id)
        self.db.add(row)
        audit.record(self.db, self.ctx, "approval_delegation.created", target=delegator.email,
                     details={"delegate": delegate.email, "from": str(starts_on), "to": str(ends_on)})
        await self.db.commit()
        return row

    async def end(self, delegation_id: uuid.UUID) -> None:
        row = await self.db.get(ApprovalDelegation, delegation_id, with_for_update=True)
        if row is None or not row.is_active:
            raise NotFoundError("Delegation not found.")
        if self.user.role != ROLE_ADMIN and self.user.id not in (row.delegator_user_id, row.delegate_user_id):
            raise NotFoundError("Delegation not found.")
        # Ends at once; the row stays (inactive, with ended_at) as the record of who could approve when.
        row.is_active, row.ended_at = False, datetime.now(UTC)
        delegator = await self.db.get(User, row.delegator_user_id)
        audit.record(self.db, self.ctx, "approval_delegation.ended", target=delegator.email if delegator else str(row.delegator_user_id),
                     details={"delegation": str(row.id)})
        await self.db.commit()
