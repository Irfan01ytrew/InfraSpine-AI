"""Who may approve how much: approval limits per position or per person, with separation of duties.

A person's own limit overrides their position's limit. With no limit nobody can approve a purchase request
or expense claim, administrators included, and nobody can approve their own request. When the reviewer's
authority is too low, the first person up the requester's reporting line with enough authority is suggested.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.approval_models import FINANCIAL_CATEGORIES, ApprovalLimit
from app.db.models import ServiceRequest, User
from app.db.org_models import Position
from app.services import audit
from app.services.audit import AuditContext

MAX_CHAIN = 64


@dataclass(frozen=True)
class ApprovalCheck:
    can_approve: bool
    reason: str | None
    limit: Decimal | None
    limit_currency: str | None
    limit_source: str | None  # "personal" or the position title


async def effective_limit(db: AsyncSession, user: User, category: str) -> tuple[ApprovalLimit | None, str | None]:
    personal = await db.scalar(select(ApprovalLimit).where(
        ApprovalLimit.is_active.is_(True), ApprovalLimit.category == category, ApprovalLimit.user_id == user.id,
    ))
    if personal is not None:
        return personal, "personal"
    if user.position_id is None:
        return None, None
    row = (await db.execute(
        select(ApprovalLimit, Position.title)
        .join(Position, Position.id == ApprovalLimit.position_id)
        .where(ApprovalLimit.is_active.is_(True), ApprovalLimit.category == category, ApprovalLimit.position_id == user.position_id)
    )).first()
    return (row[0], row[1]) if row else (None, None)


async def check_approval(db: AsyncSession, approver: User, request: ServiceRequest) -> ApprovalCheck:
    if request.category not in FINANCIAL_CATEGORIES or request.amount is None:
        return ApprovalCheck(True, None, None, None, None)
    if request.requester_user_id == approver.id:
        return ApprovalCheck(False, "You cannot approve your own request.", None, None, None)
    limit, source = await effective_limit(db, approver, request.category)
    if limit is None:
        return ApprovalCheck(False, f"You have no approval limit for {request.category} requests.", None, None, None)
    if limit.currency != request.currency:
        return ApprovalCheck(False, f"Your approval limit is in {limit.currency}; this request is in {request.currency}.", limit.max_amount, limit.currency, source)
    if limit.max_amount < request.amount:
        return ApprovalCheck(False, f"This needs {request.currency} {request.amount:,.2f}; your limit is {limit.currency} {limit.max_amount:,.2f}.", limit.max_amount, limit.currency, source)
    return ApprovalCheck(True, None, limit.max_amount, limit.currency, source)


async def suggest_approver(db: AsyncSession, request: ServiceRequest) -> User | None:
    """The nearest manager up the requester's reporting line whose authority covers the request."""
    if request.category not in FINANCIAL_CATEGORIES or request.amount is None:
        return None
    requester = await db.get(User, request.requester_user_id)
    current_id = requester.reports_to_id if requester else None
    seen: set[uuid.UUID] = set()
    for _ in range(MAX_CHAIN):
        if current_id is None or current_id in seen:
            return None
        seen.add(current_id)
        person = await db.get(User, current_id)
        if person is None:
            return None
        if person.is_active and person.id != request.requester_user_id and (await check_approval(db, person, request)).can_approve:
            return person
        current_id = person.reports_to_id
    return None


class ApprovalLimitService:
    def __init__(self, db: AsyncSession, admin: User, ctx: AuditContext) -> None:
        self.db = db
        self.admin = admin
        self.ctx = ctx

    async def list(self) -> list[tuple[ApprovalLimit, str | None, str | None]]:
        rows = await self.db.execute(
            select(ApprovalLimit, Position.title, User.email)
            .outerjoin(Position, Position.id == ApprovalLimit.position_id)
            .outerjoin(User, User.id == ApprovalLimit.user_id)
            .where(ApprovalLimit.is_active.is_(True))
            .order_by(ApprovalLimit.category, ApprovalLimit.max_amount.desc())
        )
        return [(limit, title, email) for limit, title, email in rows.all()]

    async def create(self, *, category: str, position_id: uuid.UUID | None, user_id: uuid.UUID | None, max_amount: Decimal, currency: str) -> ApprovalLimit:
        if category not in FINANCIAL_CATEGORIES:
            raise InvalidInputError("Approval limits apply to purchase requests and expense claims.")
        if (position_id is None) == (user_id is None):
            raise InvalidInputError("Set the limit for either a position or one person.")
        subject = None
        if position_id is not None:
            position = await self.db.get(Position, position_id)
            if position is None or not position.is_active:
                raise InvalidInputError("Choose an active position.")
            subject = position.title
        else:
            person = await self.db.get(User, user_id)
            if person is None or not person.is_active:
                raise InvalidInputError("Choose an active person.")
            subject = person.email
        limit = ApprovalLimit(category=category, position_id=position_id, user_id=user_id, max_amount=max_amount, currency=currency, created_by=self.admin.id)
        self.db.add(limit)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError(f"{subject} already has a {category} approval limit. Change that one instead.") from None
        audit.record(self.db, self.ctx, "approval_limit.created", target=subject, details={"category": category, "max_amount": str(max_amount), "currency": currency})
        await self.db.commit()
        return limit

    async def update(self, limit_id: uuid.UUID, *, max_amount: Decimal | None, currency: str | None, is_active: bool | None) -> ApprovalLimit:
        limit = await self.db.get(ApprovalLimit, limit_id, with_for_update=True)
        if limit is None or not limit.is_active:
            raise NotFoundError("Approval limit not found.")
        before = {"max_amount": str(limit.max_amount), "currency": limit.currency}
        if max_amount is not None:
            limit.max_amount = max_amount
        if currency is not None:
            limit.currency = currency
        if is_active is False:
            limit.is_active = False  # removed limits are kept (inactive) for the audit trail
        limit.updated_at = datetime.now(UTC)
        audit.record(
            self.db, self.ctx, "approval_limit.removed" if is_active is False else "approval_limit.changed",
            target=str(limit.id), details={"from": before, "to": {"max_amount": str(limit.max_amount), "currency": limit.currency}, "category": limit.category},
        )
        await self.db.commit()
        return limit
