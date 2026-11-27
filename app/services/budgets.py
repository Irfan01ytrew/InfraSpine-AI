"""Department budgets and the spend recorded against them.

Spend comes from the department's purchase requests and expense claims in the budget currency:
* approved  — closed as approved within the budget period;
* committed — submitted within the period and still being approved.
A final approval that would take approved spend over a budget is refused for 'block' budgets and recorded
as an overrun for 'warn' budgets.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.approval_models import FINANCIAL_CATEGORIES, Budget
from app.db.models import ROLE_ADMIN, ROLE_MANAGER, AccessGroup, DepartmentManager, ServiceRequest, User
from app.services import audit
from app.services.audit import AuditContext
from app.platform.timezone import org_today

_OPEN = ("submitted", "acknowledged", "in_progress", "waiting_requester")


@dataclass(frozen=True)
class BudgetUse:
    budget: Budget
    department_name: str
    approved: Decimal
    committed: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.budget.amount - self.approved


def _categories(budget: Budget) -> tuple[str, ...]:
    return FINANCIAL_CATEGORIES if budget.category == "all" else (budget.category,)


async def budget_use(db: AsyncSession, budget: Budget, department_name: str = "") -> BudgetUse:
    base = (
        ServiceRequest.department_group_id == budget.department_group_id,
        ServiceRequest.currency == budget.currency,
        ServiceRequest.category.in_(_categories(budget)),
    )
    approved = await db.scalar(select(func.coalesce(func.sum(ServiceRequest.amount), 0)).where(
        *base, ServiceRequest.status == "resolved",
        cast(ServiceRequest.closed_at, Date).between(budget.period_start, budget.period_end),
    ))
    committed = await db.scalar(select(func.coalesce(func.sum(ServiceRequest.amount), 0)).where(
        *base, ServiceRequest.status.in_(_OPEN),
        cast(ServiceRequest.created_at, Date).between(budget.period_start, budget.period_end),
    ))
    cents = Decimal("0.01")
    return BudgetUse(budget, department_name, Decimal(approved).quantize(cents), Decimal(committed).quantize(cents))


async def budgets_for_request(db: AsyncSession, request: ServiceRequest, on: date | None = None) -> list[BudgetUse]:
    """Active budgets of the request's department and currency covering `on` (default today)."""
    if request.category not in FINANCIAL_CATEGORIES or request.department_group_id is None or request.amount is None:
        return []
    on = on or await org_today(db)
    rows = (await db.scalars(select(Budget).where(
        Budget.is_active.is_(True), Budget.department_group_id == request.department_group_id,
        Budget.currency == request.currency, Budget.category.in_((request.category, "all")),
        Budget.period_start <= on, Budget.period_end >= on,
    ))).all()
    return [await budget_use(db, budget) for budget in rows]


async def check_budgets_for_approval(db: AsyncSession, request: ServiceRequest) -> dict:
    """Refuse a final approval that breaks a blocking budget; return overrun details for the audit record."""
    overruns = []
    for use in await budgets_for_request(db, request):
        after = use.approved + request.amount
        if after > use.budget.amount:
            if use.budget.enforcement == "block":
                raise PermissionDeniedError(
                    f"Approving this would exceed the budget “{use.budget.name}”: {use.budget.currency} "
                    f"{use.approved:,.2f} of {use.budget.amount:,.2f} is already approved."
                )
            overruns.append({"budget": use.budget.name, "over_by": str(after - use.budget.amount)})
    return {"over_budget": overruns} if overruns else {}


class BudgetService:
    def __init__(self, db: AsyncSession, user: User, ctx: AuditContext) -> None:
        self.db = db
        self.user = user
        self.ctx = ctx

    async def readable(self) -> list[BudgetUse]:
        """Administrators see every budget; department managers see their departments'; others none."""
        query = select(Budget, AccessGroup.name).join(AccessGroup, AccessGroup.id == Budget.department_group_id).where(Budget.is_active.is_(True))
        if self.user.role == ROLE_MANAGER:
            managed = select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == self.user.id)
            query = query.where(Budget.department_group_id.in_(managed))
        elif self.user.role != ROLE_ADMIN:
            raise PermissionDeniedError("Budgets are visible to administrators and department managers.")
        rows = (await self.db.execute(query.order_by(Budget.period_start.desc(), AccessGroup.name, Budget.category))).all()
        return [await budget_use(self.db, budget, name) for budget, name in rows]

    def _admin_only(self) -> None:
        if self.user.role != ROLE_ADMIN:
            raise PermissionDeniedError("Only administrators manage budgets.")

    async def create(self, *, name: str, department_group_id: uuid.UUID, category: str, period_start: date, period_end: date,
                     amount: Decimal, currency: str, enforcement: str) -> Budget:
        self._admin_only()
        if period_end < period_start:
            raise InvalidInputError("The budget period must end on or after it starts.")
        group = await self.db.get(AccessGroup, department_group_id)
        if group is None or group.kind != "department":
            raise InvalidInputError("Choose a department.")
        budget = Budget(name=" ".join(name.split()), department_group_id=department_group_id, category=category, period_start=period_start,
                        period_end=period_end, amount=amount, currency=currency, enforcement=enforcement, created_by=self.user.id)
        self.db.add(budget)
        audit.record(self.db, self.ctx, "budget.created", target=budget.name, details={
            "department": group.name, "category": category, "amount": str(amount), "currency": currency,
            "period": f"{period_start}..{period_end}", "enforcement": enforcement,
        })
        await self.db.commit()
        return budget

    async def update(self, budget_id: uuid.UUID, *, amount: Decimal | None, enforcement: str | None, is_active: bool | None) -> Budget:
        self._admin_only()
        budget = await self.db.get(Budget, budget_id, with_for_update=True)
        if budget is None or not budget.is_active:
            raise NotFoundError("Budget not found.")
        before = {"amount": str(budget.amount), "enforcement": budget.enforcement}
        if amount is not None:
            budget.amount = amount
        if enforcement is not None:
            budget.enforcement = enforcement
        if is_active is False:
            budget.is_active = False  # closed budgets stay for the audit trail
        budget.updated_at = datetime.now(UTC)
        audit.record(self.db, self.ctx, "budget.closed" if is_active is False else "budget.changed", target=budget.name,
                     details={"from": before, "to": {"amount": str(budget.amount), "enforcement": budget.enforcement}})
        await self.db.commit()
        return budget
