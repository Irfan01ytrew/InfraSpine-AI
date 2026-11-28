"""Admin-only routing policy management (Step T slice 4).

The smart model router's policy is one admin-editable row: which policy
ranks models (default + per-task-class overrides), per-model capability
tiers, and the monthly budget for routed calls (org-wide plus
per-department overrides). This API reads and writes it, and exposes the
slice-3 spend report so the admin can see spend against the budget.

The policy contains no credentials, keys or endpoints, so it is safe to
return from the API and to record in the audit log. Every route requires
an authenticated admin (``require_admin``).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AdminUser, DbSession, audit_context, require_admin
from app.services import audit
from app.services.routing_policy import (
    POLICIES,
    TASK_CLASSES,
    RoutingPolicyConfig,
    effective_budget_usd,
    list_departments,
    load_routing_policy,
    parse_routing_policy,
    save_routing_policy,
    validate_routing_policy,
)
from app.services.routing_records import routing_cost_report, routing_spent_this_month

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class RoutingPolicyIn(BaseModel):
    """What the admin editor sends. Extra fields are rejected; amounts are
    Decimals so $12.50 stays exact through JSON."""

    model_config = ConfigDict(extra="forbid")

    default_policy: str = Field(default="cheapest")
    task_class_policies: dict[str, str] = Field(default_factory=dict)
    capability_tiers: dict[str, int] = Field(default_factory=dict)
    monthly_budget_usd: Decimal | None = Field(default=None)
    department_budgets: dict[str, Decimal] = Field(default_factory=dict)


class DepartmentOut(BaseModel):
    id: uuid.UUID
    name: str


class RoutingPolicyOut(BaseModel):
    default_policy: str
    task_class_policies: dict[str, str]
    capability_tiers: dict[str, int]
    monthly_budget_usd: str | None
    department_budgets: dict[str, str]
    departments: list[DepartmentOut]
    spent_this_month_usd: str
    task_classes: list[str] = Field(default_factory=lambda: list(TASK_CLASSES))
    policies: list[str] = Field(default_factory=lambda: list(POLICIES))


class TaskClassCostOut(BaseModel):
    task_class: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_cost_usd: str
    avg_latency_ms: float | None
    refused_calls: int


class RoutingReportOut(BaseModel):
    days: int
    department_id: str | None
    spent_this_month_usd: str
    monthly_budget_usd: str | None
    remaining_usd: str | None
    by_task_class: list[TaskClassCostOut]


def _money(amount: Decimal | None) -> str | None:
    return None if amount is None else str(amount)


async def _out(db: DbSession) -> RoutingPolicyOut:
    config = await load_routing_policy(db)
    departments = await list_departments(db)
    spent = await routing_spent_this_month(db)
    return RoutingPolicyOut(
        default_policy=config.default_policy.value,
        task_class_policies={
            task_class.value: policy.value
            for task_class, policy in config.task_class_policies.items()
        },
        capability_tiers=dict(config.capability_tiers),
        monthly_budget_usd=_money(config.monthly_budget_usd),
        department_budgets={
            department_id: str(amount)
            for department_id, amount in config.department_budgets.items()
        },
        departments=[DepartmentOut(id=group.id, name=group.name) for group in departments],
        spent_this_month_usd=str(spent),
    )


@router.get("/routing-policy", response_model=RoutingPolicyOut)
async def get_routing_policy(db: DbSession) -> RoutingPolicyOut:
    """The current routing policy, the departments a budget can be set for,
    and this month's routed spend so far."""
    return await _out(db)


@router.put("/routing-policy", response_model=RoutingPolicyOut)
async def put_routing_policy(
    body: RoutingPolicyIn, request: Request, db: DbSession, admin: AdminUser
) -> RoutingPolicyOut:
    """Replace the routing policy. Every problem is reported at once (422);
    a valid policy is saved and audited. Takes effect on the next routed
    call -- no restart is needed."""
    data = body.model_dump()
    problems = validate_routing_policy(data)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    config = parse_routing_policy(data)
    previous = await load_routing_policy(db)
    await save_routing_policy(db, config, updated_by=admin.id)
    audit.record(
        db,
        audit_context(request, admin),
        "routing.policy_updated",
        target="routing_policy",
        details={
            "default_policy": {"from": previous.default_policy.value, "to": config.default_policy.value},
            "monthly_budget_usd": {
                "from": _money(previous.monthly_budget_usd),
                "to": _money(config.monthly_budget_usd),
            },
            "task_class_policies": {
                task_class.value: policy.value
                for task_class, policy in config.task_class_policies.items()
            },
            "departments_with_budgets": sorted(config.department_budgets),
        },
    )
    await db.commit()
    return await _out(db)


@router.get("/routing-report", response_model=RoutingReportOut)
async def get_routing_report(
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RoutingReportOut:
    """Routed-call spend for the last ``days`` days, per task class, plus
    this month's spend against the budget that applies (the department's
    own budget when one is set, else the org-wide one). Read-only."""
    config: RoutingPolicyConfig = await load_routing_policy(db)
    budget = effective_budget_usd(config, department_id)
    spent = await routing_spent_this_month(db, department_id=department_id)
    report = await routing_cost_report(db, days=days, department_id=department_id)
    return RoutingReportOut(
        days=days,
        department_id=str(department_id) if department_id is not None else None,
        spent_this_month_usd=str(spent),
        monthly_budget_usd=_money(budget),
        remaining_usd=_money(budget - spent) if budget is not None else None,
        by_task_class=[
            TaskClassCostOut(
                task_class=row.task_class,
                calls=row.calls,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                total_cost_usd=str(row.total_cost_usd),
                avg_latency_ms=row.avg_latency_ms,
                refused_calls=row.refused_calls,
            )
            for row in report
        ],
    )
