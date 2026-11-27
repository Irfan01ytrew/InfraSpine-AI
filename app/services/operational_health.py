"""Operational health, LLM spend attribution, and department quota tracking."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccessGroup, AccessGroupMember, RagTrace
from app.schemas.reports import (
    DepartmentUsageQuota,
    OperationalHealthDashboard,
    ProviderCostDetail,
    QuotaAlert,
)

# In-memory quota overrides per department (persisted across service lifetime)
_DEPARTMENT_QUOTA_CONFIGS: dict[str, dict[str, Any]] = {}
DEFAULT_MONTHLY_TOKEN_QUOTA = 1_000_000
DEFAULT_ALERT_THRESHOLD_PERCENT = 80.0

# Standard pricing table: (prompt_per_1k, completion_per_1k) in USD
PROVIDER_RATES: dict[str, tuple[float, float]] = {
    "gemini-1.5-pro": (0.00125, 0.00500),
    "gemini-2.0-flash": (0.00010, 0.00040),
    "gemini-1.5-flash": (0.000075, 0.00030),
    "gemini": (0.00050, 0.00150),
    "gpt-4o": (0.00250, 0.01000),
    "gpt-4o-mini": (0.00015, 0.00060),
    "openai": (0.00200, 0.00800),
    "claude-3-5-sonnet": (0.00300, 0.01500),
    "claude-3-haiku": (0.00025, 0.00125),
    "anthropic": (0.00300, 0.01500),
}


def calculate_token_cost(
    provider: str | None,
    model: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float:
    """Calculate estimated LLM spend based on provider pricing rates."""
    if not provider:
        return 0.0

    p_tokens = prompt_tokens or 0
    c_tokens = completion_tokens or 0

    key = (model or "").lower()
    rates = None
    for k, r in PROVIDER_RATES.items():
        if k in key:
            rates = r
            break

    if not rates:
        rates = PROVIDER_RATES.get((provider or "").lower(), (0.0, 0.0))

    cost = (p_tokens / 1000.0) * rates[0] + (c_tokens / 1000.0) * rates[1]
    return round(cost, 4)


def set_department_quota(
    department_id: uuid.UUID,
    monthly_token_quota: int,
    alert_threshold_percent: float = 80.0,
) -> None:
    """Configure custom quota and alert threshold for a department."""
    _DEPARTMENT_QUOTA_CONFIGS[str(department_id)] = {
        "monthly_token_quota": max(10_000, monthly_token_quota),
        "alert_threshold_percent": min(100.0, max(1.0, alert_threshold_percent)),
    }


async def get_provider_costs(
    db: AsyncSession,
    days: int = 30,
) -> list[ProviderCostDetail]:
    """Calculate aggregated token usage and spend attribution by provider and model."""
    start = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(
            RagTrace.provider,
            RagTrace.model,
            func.coalesce(func.sum(RagTrace.llm_calls), 0),
            func.coalesce(func.sum(RagTrace.prompt_tokens), 0),
            func.coalesce(func.sum(RagTrace.completion_tokens), 0),
        )
        .where(RagTrace.created_at >= start, RagTrace.provider.is_not(None))
        .group_by(RagTrace.provider, RagTrace.model)
        .order_by(func.coalesce(func.sum(RagTrace.prompt_tokens), 0).desc())
    )
    rows = (await db.execute(stmt)).all()

    details: list[ProviderCostDetail] = []
    for provider, model, calls, prompt_tok, comp_tok in rows:
        cost = calculate_token_cost(provider, model, prompt_tok, comp_tok)
        details.append(
            ProviderCostDetail(
                provider=provider or "unknown",
                model=model,
                calls=int(calls),
                prompt_tokens=int(prompt_tok),
                completion_tokens=int(comp_tok),
                estimated_cost_usd=cost,
            )
        )
    return details


async def get_department_quotas(
    db: AsyncSession,
    days: int = 30,
) -> tuple[list[DepartmentUsageQuota], list[QuotaAlert]]:
    """Calculate department usage vs configured quotas and generate threshold alerts."""
    start = datetime.now(UTC) - timedelta(days=days)

    # 1. Fetch all departments
    dept_stmt = select(AccessGroup).where(AccessGroup.kind == "department").order_by(AccessGroup.name)
    departments = (await db.execute(dept_stmt)).scalars().all()

    # 2. Aggregate traces by user
    user_traces_stmt = (
        select(
            RagTrace.user_id,
            func.count(RagTrace.id),
            func.coalesce(func.sum(RagTrace.prompt_tokens), 0),
            func.coalesce(func.sum(RagTrace.completion_tokens), 0),
        )
        .where(RagTrace.created_at >= start, RagTrace.user_id.is_not(None))
        .group_by(RagTrace.user_id)
    )
    user_traces = {
        row[0]: (int(row[1]), int(row[2]), int(row[3]))
        for row in (await db.execute(user_traces_stmt)).all()
    }

    # 3. Fetch department memberships
    member_stmt = select(AccessGroupMember.group_id, AccessGroupMember.user_id)
    memberships = (await db.execute(member_stmt)).all()

    dept_users: dict[uuid.UUID, list[uuid.UUID]] = {}
    for gid, uid in memberships:
        dept_users.setdefault(gid, []).append(uid)

    quotas: list[DepartmentUsageQuota] = []
    alerts: list[QuotaAlert] = []

    for dept in departments:
        uids = dept_users.get(dept.id, [])
        dept_questions = sum(user_traces.get(uid, (0, 0, 0))[0] for uid in uids)
        dept_prompt_tokens = sum(user_traces.get(uid, (0, 0, 0))[1] for uid in uids)
        dept_comp_tokens = sum(user_traces.get(uid, (0, 0, 0))[2] for uid in uids)
        total_tokens = dept_prompt_tokens + dept_comp_tokens
        dept_cost = calculate_token_cost("gemini", "gemini-1.5-pro", dept_prompt_tokens, dept_comp_tokens)

        # Config lookup
        cfg = _DEPARTMENT_QUOTA_CONFIGS.get(
            str(dept.id),
            {
                "monthly_token_quota": DEFAULT_MONTHLY_TOKEN_QUOTA,
                "alert_threshold_percent": DEFAULT_ALERT_THRESHOLD_PERCENT,
            },
        )
        monthly_quota = cfg["monthly_token_quota"]
        threshold_pct = cfg["alert_threshold_percent"]

        utilization = round((total_tokens / monthly_quota) * 100.0, 2)
        is_triggered = utilization >= threshold_pct
        severity = None

        if utilization >= 100.0:
            severity = "critical"
            alerts.append(
                QuotaAlert(
                    department_id=str(dept.id),
                    department_name=dept.name,
                    message=f"Department '{dept.name}' exceeded monthly token quota ({utilization}% used).",
                    severity="critical",
                    utilization_percent=utilization,
                )
            )
        elif utilization >= threshold_pct:
            severity = "warning"
            alerts.append(
                QuotaAlert(
                    department_id=str(dept.id),
                    department_name=dept.name,
                    message=f"Department '{dept.name}' reached {utilization}% of monthly token quota.",
                    severity="warning",
                    utilization_percent=utilization,
                )
            )

        quotas.append(
            DepartmentUsageQuota(
                department_id=str(dept.id),
                department_name=dept.name,
                questions_count=dept_questions,
                token_count=total_tokens,
                estimated_cost_usd=dept_cost,
                monthly_token_quota=monthly_quota,
                utilization_percent=utilization,
                alert_threshold_percent=threshold_pct,
                is_alert_triggered=is_triggered,
                alert_severity=severity,
            )
        )

    return quotas, alerts


async def get_operational_dashboard(
    db: AsyncSession,
    days: int = 30,
) -> OperationalHealthDashboard:
    """Build complete operational health, cost attribution, and quota dashboard."""
    provider_costs = await get_provider_costs(db, days)
    dept_quotas, active_alerts = await get_department_quotas(db, days)

    total_spend = sum(p.estimated_cost_usd for p in provider_costs)
    total_tokens = sum(p.prompt_tokens + p.completion_tokens for p in provider_costs)
    status = "degraded" if any(a.severity == "critical" for a in active_alerts) else "healthy"

    return OperationalHealthDashboard(
        system_status=status,
        total_spend_usd=round(total_spend, 4),
        total_tokens_used=total_tokens,
        active_alerts=active_alerts,
        department_quotas=dept_quotas,
        provider_costs=provider_costs,
    )
