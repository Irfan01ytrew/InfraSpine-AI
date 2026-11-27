"""Multi-step approval chains for purchase requests and expense claims.

An administrator configures ordered steps per category (the requester's manager, any holder of a position, or
one named person; optionally only at or above an amount). Consecutive steps can form a parallel stage, decided at
the same time by all of its approvers or by any one of them. When a request is submitted the applicable steps are
copied onto it, so later policy changes never move in-flight requests. Stages are decided in order; any rejection
rejects the request. Nobody approves their own request or two steps of the same request, and the final step's
approver also needs an approval limit covering the amount. Without a configured chain the single-step,
limit-based approval applies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.approval_models import FINANCIAL_CATEGORIES, ApprovalChainStep, RequestApprovalStep
from app.db.models import DepartmentManager, ServiceRequest, ServiceRequestEvent, ServiceRequestNotification, User
from app.db.org_models import Position
from app.services import audit
from app.services.approval_authority import check_approval
from app.services.budgets import check_budgets_for_approval
from app.services.approval_delegation import active_delegators, delegators_of, notify_step
from app.services import webhooks
from app.services.audit import AuditContext


# ----------------------------------------------------------------------------------------------- admin
class ApprovalChainService:
    def __init__(self, db: AsyncSession, admin: User, ctx: AuditContext) -> None:
        self.db = db
        self.admin = admin
        self.ctx = ctx

    async def list(self) -> list[tuple[ApprovalChainStep, str | None, str | None]]:
        rows = await self.db.execute(
            select(ApprovalChainStep, Position.title, User.email)
            .outerjoin(Position, Position.id == ApprovalChainStep.position_id)
            .outerjoin(User, User.id == ApprovalChainStep.user_id)
            .where(ApprovalChainStep.is_active.is_(True))
            .order_by(ApprovalChainStep.category, ApprovalChainStep.step_order)
        )
        return [(step, title, email) for step, title, email in rows.all()]

    async def _active(self, category: str) -> list[ApprovalChainStep]:
        return list((await self.db.scalars(
            select(ApprovalChainStep).where(ApprovalChainStep.category == category, ApprovalChainStep.is_active.is_(True))
            .order_by(ApprovalChainStep.step_order)
        )).all())

    async def _normalize(self, category: str) -> None:
        """The first step starts a stage; every step of a stage carries the stage's mode (its first step's)."""
        mode = "all"
        for index, step in enumerate(await self._active(category)):
            if index == 0:
                step.parallel_with_previous = False
            if not step.parallel_with_previous:
                mode = step.stage_mode
            step.stage_mode = mode
        await self.db.flush()

    async def set_stage(self, step_id: uuid.UUID, *, parallel_with_previous: bool | None, stage_mode: str | None) -> None:
        """Join a step to the previous step's stage (or split it off), or set its stage's all/any mode."""
        step = await self.db.get(ApprovalChainStep, step_id, with_for_update=True)
        if step is None or not step.is_active:
            raise NotFoundError("Approval step not found.")
        if parallel_with_previous is not None:
            if parallel_with_previous and step.step_order == 1:
                raise InvalidInputError("The first step has no previous step to run alongside.")
            step.parallel_with_previous = parallel_with_previous
        if stage_mode is not None:
            # Apply to the whole stage: back to its first step, forward while steps stay parallel.
            steps = await self._active(step.category)
            index = next(i for i, s in enumerate(steps) if s.id == step.id)
            start = index
            while start > 0 and steps[start].parallel_with_previous:
                start -= 1
            end = index + 1
            while end < len(steps) and steps[end].parallel_with_previous:
                end += 1
            for member in steps[start:end]:
                member.stage_mode = stage_mode
        await self.db.flush()
        await self._normalize(step.category)
        audit.record(self.db, self.ctx, "approval_chain.stage_changed", target=step.label,
                     details={"category": step.category, "parallel_with_previous": step.parallel_with_previous, "stage_mode": step.stage_mode})
        await self.db.commit()

    async def add(
        self, *, category: str, label: str, approver_kind: str, position_id: uuid.UUID | None,
        user_id: uuid.UUID | None, min_amount: Decimal | None, parallel_with_previous: bool = False, stage_mode: str = "all",
    ) -> ApprovalChainStep:
        if category not in FINANCIAL_CATEGORIES:
            raise InvalidInputError("Approval chains apply to purchase requests and expense claims.")
        label = " ".join(label.split())
        if not 2 <= len(label) <= 80:
            raise InvalidInputError("Give the step a name of 2 to 80 characters.")
        if approver_kind == "position":
            position = await self.db.get(Position, position_id) if position_id else None
            if position is None or not position.is_active:
                raise InvalidInputError("Choose an active position for this step.")
            user_id = None
        elif approver_kind == "person":
            person = await self.db.get(User, user_id) if user_id else None
            if person is None or not person.is_active:
                raise InvalidInputError("Choose an active person for this step.")
            position_id = None
        elif approver_kind == "manager":
            position_id = user_id = None
        else:
            raise InvalidInputError("Unknown approver type.")
        last = await self.db.scalar(
            select(ApprovalChainStep.step_order).where(ApprovalChainStep.category == category, ApprovalChainStep.is_active.is_(True))
            .order_by(ApprovalChainStep.step_order.desc()).limit(1)
        )
        order = (last or 0) + 1
        if order > 20:
            raise InvalidInputError("A chain can have at most 20 steps.")
        step = ApprovalChainStep(
            category=category, step_order=order, label=label, approver_kind=approver_kind,
            position_id=position_id, user_id=user_id, min_amount=min_amount, created_by=self.admin.id,
            parallel_with_previous=parallel_with_previous and order > 1, stage_mode=stage_mode,
        )
        self.db.add(step)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError("Another change to this chain happened at the same time. Try again.") from None
        if step.parallel_with_previous:
            # Joining a stage sets the stage's mode for all of its steps.
            for member in reversed((await self._active(category))[:-1]):
                member.stage_mode = stage_mode
                if not member.parallel_with_previous:
                    break
        await self._normalize(category)
        audit.record(self.db, self.ctx, "approval_chain.step_added", target=label,
                     details={"category": category, "order": order, "kind": approver_kind, "min_amount": str(min_amount) if min_amount is not None else None})
        await self.db.commit()
        return step

    async def remove(self, step_id: uuid.UUID) -> None:
        step = await self.db.get(ApprovalChainStep, step_id, with_for_update=True)
        if step is None or not step.is_active:
            raise NotFoundError("Approval step not found.")
        step.is_active = False
        await self.db.flush()
        # Close the gap so the remaining steps stay numbered 1..n.
        later = (await self.db.scalars(
            select(ApprovalChainStep).where(
                ApprovalChainStep.category == step.category, ApprovalChainStep.is_active.is_(True),
                ApprovalChainStep.step_order > step.step_order,
            ).order_by(ApprovalChainStep.step_order)
        )).all()
        for item in later:
            item.step_order -= 1
            await self.db.flush()
        if later and not step.parallel_with_previous and later[0].parallel_with_previous:
            later[0].parallel_with_previous = False  # the stage keeps its remaining steps
        await self._normalize(step.category)
        audit.record(self.db, self.ctx, "approval_chain.step_removed", target=step.label, details={"category": step.category})
        await self.db.commit()

    async def move(self, step_id: uuid.UUID, direction: int) -> None:
        step = await self.db.get(ApprovalChainStep, step_id, with_for_update=True)
        if step is None or not step.is_active:
            raise NotFoundError("Approval step not found.")
        neighbour = await self.db.scalar(select(ApprovalChainStep).where(
            ApprovalChainStep.category == step.category, ApprovalChainStep.is_active.is_(True),
            ApprovalChainStep.step_order == step.step_order + direction,
        ).with_for_update())
        if neighbour is None:
            return
        original = step.step_order
        # The unique order index covers active steps only: park this step while the two swap places.
        step.is_active = False
        await self.db.flush()
        neighbour.step_order = original
        await self.db.flush()
        step.step_order, step.is_active = original + direction, True
        await self.db.flush()
        await self._normalize(step.category)
        audit.record(self.db, self.ctx, "approval_chain.step_moved", target=step.label, details={"category": step.category, "order": step.step_order})
        await self.db.commit()


# ------------------------------------------------------------------------------------------- requests
async def attach_steps(db: AsyncSession, request: ServiceRequest, requester: User) -> int:
    """Copy the applicable chain steps onto a newly submitted financial request; returns how many."""
    if request.category not in FINANCIAL_CATEGORIES or request.amount is None:
        return 0
    chain = (await db.scalars(
        select(ApprovalChainStep).where(ApprovalChainStep.category == request.category, ApprovalChainStep.is_active.is_(True))
        .order_by(ApprovalChainStep.step_order)
    )).all()
    applicable = [s for s in chain if s.min_amount is None or request.amount >= s.min_amount]
    stages: list[list[ApprovalChainStep]] = []
    for step in applicable:  # a parallel step joins the stage of the previous applicable step
        if step.parallel_with_previous and stages:
            stages[-1].append(step)
        else:
            stages.append([step])
    now = datetime.now(UTC)
    order, first_stage = 0, []
    for number, members in enumerate(stages, start=1):
        mode = members[0].stage_mode if len(members) > 1 else "all"
        for step in members:
            order += 1
            approver_user = step.user_id if step.approver_kind == "person" else (requester.reports_to_id if step.approver_kind == "manager" else None)
            row = RequestApprovalStep(
                request_id=request.id, step_order=order, label=step.label, approver_kind=step.approver_kind,
                approver_user_id=approver_user, approver_position_id=step.position_id if step.approver_kind == "position" else None,
                stage=number, stage_mode=mode, final=number == len(stages),
                status="pending" if number == 1 else "waiting", pending_since=now if number == 1 else None,
            )
            db.add(row)
            if number == 1:
                first_stage.append(row)
    if first_stage:
        await db.flush()
        for row in first_stage:
            await notify_step(db, request, row, "approval_step_pending", now)
            await webhooks.request_event(db, request, "approval.step_pending", step=row.step_order, step_label=row.label)
    return len(applicable)


async def request_steps(db: AsyncSession, request_id: uuid.UUID) -> list[RequestApprovalStep]:
    return list((await db.scalars(
        select(RequestApprovalStep).where(RequestApprovalStep.request_id == request_id).order_by(RequestApprovalStep.step_order)
    )).all())


@dataclass(frozen=True)
class StepCheck:
    allowed: bool
    reason: str | None
    # The approver a delegate acts for; None when the user decides in their own right.
    on_behalf_of: User | None = None


def _involved(steps: list[RequestApprovalStep], step: RequestApprovalStep, user_id: uuid.UUID) -> bool:
    """Whether the person decided, or had a step decided for them, elsewhere on this request."""
    return any(user_id in (other.decided_by, other.on_behalf_of_user_id) for other in steps if other.id != step.id)


async def _assignment_reason(db: AsyncSession, person: User, request: ServiceRequest, step: RequestApprovalStep) -> str | None:
    if step.approver_kind in ("person", "manager") and step.approver_user_id is not None:
        if person.id != step.approver_user_id:
            return "This step is assigned to someone else."
    elif step.approver_kind == "manager":
        # The requester had no manager: the request department's managers decide this step.
        managed = request.department_group_id is not None and await db.get(DepartmentManager, (request.department_group_id, person.id)) is not None
        if not managed:
            return "This step is for the requester's manager or the department's managers."
    elif step.approver_kind == "position":
        if person.position_id != step.approver_position_id:
            return "This step is for holders of another position."
    return None


async def can_decide(db: AsyncSession, user: User, request: ServiceRequest, step: RequestApprovalStep, steps: list[RequestApprovalStep], *, approving: bool) -> StepCheck:
    """Whether the user may decide the step themselves or, while delegated, for an approver who is away."""
    if step.status != "pending":
        return StepCheck(False, "This step is not waiting for a decision.")
    if not user.is_active:
        return StepCheck(False, "Your account is inactive.")
    if request.requester_user_id == user.id:
        return StepCheck(False, "You cannot approve your own request.")
    if _involved(steps, step, user.id):
        return StepCheck(False, "You already decided an earlier step of this request; another person must decide this one.")
    reason = await _assignment_reason(db, user, request, step)
    candidates: list[User | None] = [None] if reason is None else []
    for principal in await active_delegators(db, user.id):
        if principal.id != request.requester_user_id and not _involved(steps, step, principal.id) \
                and await _assignment_reason(db, principal, request, step) is None:
            candidates.append(principal)
    if not candidates:
        return StepCheck(False, reason)
    if not (approving and step.final):
        return StepCheck(True, None, candidates[0])
    for principal in candidates:
        limit = await check_approval(db, principal or user, request)
        if limit.can_approve:
            return StepCheck(True, None, principal)
    limit = await check_approval(db, candidates[0] or user, request)
    return StepCheck(False, f"Final approval needs authority for the amount. {limit.reason}")


async def decidable_step(db: AsyncSession, user: User, request: ServiceRequest, steps: list[RequestApprovalStep], *, approving: bool) -> tuple[RequestApprovalStep | None, StepCheck]:
    """The first pending step (of the current stage) this user may decide, or the reason they may decide none."""
    first: StepCheck | None = None
    for step in (s for s in steps if s.status == "pending"):
        check = await can_decide(db, user, request, step, steps, approving=approving)
        if check.allowed:
            return step, check
        first = first or check
    return None, first or StepCheck(False, "This request has no approval step waiting for a decision.")


async def decide(db: AsyncSession, user: User, ctx: AuditContext, request: ServiceRequest, decision: str, comment: str | None) -> None:
    """Approve or reject a pending step; the caller has already loaded the request for update."""
    if request.status in {"resolved", "rejected", "cancelled"}:
        raise ConflictError("This request is already closed.")
    steps = await request_steps(db, request.id)
    if not any(s.status == "pending" for s in steps):
        raise ConflictError("This request has no approval step waiting for a decision.")
    clean = (comment or "").strip() or None
    if decision == "reject" and not clean:
        raise InvalidInputError("Say why the request is rejected.")
    pending, check = await decidable_step(db, user, request, steps, approving=decision == "approve")
    if pending is None:
        raise PermissionDeniedError(check.reason or "You cannot decide this step.")
    others_open = [s for s in steps if s.stage == pending.stage and s.id != pending.id and s.status == "pending"]
    completes_stage = decision == "approve" and (pending.stage_mode == "any" or not others_open)
    completes_request = completes_stage and pending.final
    budget_details = await check_budgets_for_approval(db, request) if completes_request else {}
    now = datetime.now(UTC)
    pending.status = "approved" if decision == "approve" else "rejected"
    pending.decided_by, pending.decided_by_email, pending.decided_at, pending.comment = user.id, user.email, now, clean
    principal = check.on_behalf_of
    if principal is not None:
        pending.on_behalf_of_user_id, pending.on_behalf_of_email = principal.id, principal.email
    old_status = request.status
    if decision == "reject":
        for other in steps:
            if other.id != pending.id and other.status in ("waiting", "pending"):
                other.status = "cancelled"
        request.status, request.closed_at = "rejected", now
    elif completes_request:
        for other in others_open:
            other.status = "cancelled"  # any-of stage: no longer needed
        request.status, request.closed_at = "resolved", now
    else:
        if completes_stage:
            for other in others_open:
                other.status = "cancelled"
            for following in (s for s in steps if s.stage == pending.stage + 1):
                following.status, following.pending_since = "pending", now
                await notify_step(db, request, following, "approval_step_pending", now)
                await webhooks.request_event(db, request, "approval.step_pending", step=following.step_order, step_label=following.label)
        if old_status == "submitted":
            request.status = "in_progress"
    request.updated_at = now
    if request.response_sla_met_at is None:
        request.response_sla_met_at = now
    behalf = f" on behalf of {principal.email}" if principal is not None else ""
    waiting = "" if decision == "reject" or completes_stage else f" Waiting for {len(others_open)} more approval(s) in this stage."
    note = f"Step {pending.step_order} ({pending.label}) {pending.status}{behalf}" + (f": {clean}" if clean else ".") + waiting
    db.add(ServiceRequestEvent(
        request_id=request.id, actor_user_id=user.id, actor_email=user.email,
        action="status_changed" if request.status != old_status else "commented",
        from_status=old_status, to_status=request.status, comment=note[:2000],
    ))
    audit.record(db, ctx, f"service_request.step_{pending.status}", target=str(request.id),
                 details={"step": pending.step_order, "label": pending.label, "stage": pending.stage, "final": pending.final, "amount": str(request.amount), "currency": request.currency,
                          **({"on_behalf_of": principal.email} if principal is not None else {})})
    if completes_request:
        limit = await check_approval(db, principal or user, request)
        audit.record(db, ctx, "service_request.approved", target=str(request.id),
                     details={"category": request.category, "amount": str(request.amount), "currency": request.currency,
                              "limit": str(limit.limit), "limit_source": limit.limit_source, "steps": len(steps), **budget_details})
    if request.status != old_status:
        await webhooks.request_event(db, request, "request.status_changed", from_status=old_status)
    if completes_request:
        await webhooks.request_event(db, request, "request.approved", decided_by=user.email, steps=len(steps))
    elif decision == "reject":
        await webhooks.request_event(db, request, "request.rejected", decided_by=user.email, step=pending.step_order)
    await db.commit()


async def cancel_steps(db: AsyncSession, request_id: uuid.UUID) -> None:
    await db.execute(update(RequestApprovalStep).where(
        RequestApprovalStep.request_id == request_id, RequestApprovalStep.status.in_(("pending", "waiting")),
    ).values(status="cancelled"))


def _step_conditions(user: User) -> list:
    """Steps this user is (or was) an approver on, decided, or can decide now for someone who delegated to them."""
    delegators = delegators_of(user.id)
    conditions = [
        RequestApprovalStep.approver_user_id == user.id, RequestApprovalStep.decided_by == user.id,
        RequestApprovalStep.on_behalf_of_user_id == user.id,
        RequestApprovalStep.approver_user_id.in_(delegators),
        RequestApprovalStep.approver_position_id.in_(select(User.position_id).where(User.id.in_(delegators), User.position_id.is_not(None))),
        # Manager steps that fell to the department's managers, when one of them delegated to this user.
        (RequestApprovalStep.approver_kind == "manager") & RequestApprovalStep.approver_user_id.is_(None) & RequestApprovalStep.request_id.in_(
            select(ServiceRequest.id).where(ServiceRequest.department_group_id.in_(
                select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id.in_(delegators))))),
        # People an overdue step was escalated to may follow it up.
        RequestApprovalStep.request_id.in_(select(ServiceRequestNotification.request_id).where(
            ServiceRequestNotification.recipient_user_id == user.id, ServiceRequestNotification.notification_type == "approval_step_escalated")),
    ]
    if user.position_id is not None:
        conditions.append(RequestApprovalStep.approver_position_id == user.position_id)
    return conditions


def step_reader_condition(user: User):
    """SQL condition: requests on which this user is (or was) a step approver, directly or by delegation."""
    return ServiceRequest.id.in_(select(RequestApprovalStep.request_id).where(or_(*_step_conditions(user))))


async def is_step_reader(db: AsyncSession, user: User, request_id: uuid.UUID) -> bool:
    return await db.scalar(select(RequestApprovalStep.id).where(RequestApprovalStep.request_id == request_id, or_(*_step_conditions(user))).limit(1)) is not None
