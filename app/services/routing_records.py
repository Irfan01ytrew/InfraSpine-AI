"""Step T slice 3: persist routing telemetry and report spend per task class.

One ModelRoutingRecord per routed call: task class, chosen provider/model,
alternates tried, token usage, latency, estimated vs actual cost. Records
carry ids and numbers only -- never prompt text, answers, keys or endpoints.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ModelRoutingRecord
from app.services.model_router import ModelCandidate, RoutingDecision, estimate_cost_usd

__all__ = [
    "TaskClassCost",
    "record_routing",
    "record_refused_routing",
    "routing_cost_report",
    "routing_spent_this_month",
]


@dataclass(frozen=True)
class TaskClassCost:
    """Aggregated routing spend for one task class."""

    task_class: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_cost_usd: Decimal
    avg_latency_ms: float | None
    refused_calls: int


def _actual_cost_usd(
    decision: RoutingDecision,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    settings: Settings | None,
) -> Decimal | None:
    """Re-price a finished call from actual token counts. Unknown when the
    chosen model has no configured rate -- never guessed as zero."""
    if settings is None or prompt_tokens is None or completion_tokens is None:
        return None
    rates = settings.llm_model_pricing_json.get(decision.key) or {}
    if rates.get("input_per_1m_tokens_usd") is None or rates.get("output_per_1m_tokens_usd") is None:
        return None
    candidate = ModelCandidate(
        provider=decision.provider,
        model=decision.model,
        context_window=0,
        max_output_tokens=0,
        input_per_1m_tokens_usd=Decimal(str(rates["input_per_1m_tokens_usd"])),
        output_per_1m_tokens_usd=Decimal(str(rates["output_per_1m_tokens_usd"])),
    )
    return estimate_cost_usd(candidate, prompt_tokens, completion_tokens)


async def record_routing(
    db: AsyncSession,
    decision: RoutingDecision,
    *,
    request_id: str | None = None,
    user_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: int | None = None,
    status: str = "ok",
    alternates_tried: int = 0,
    settings: Settings | None = None,
) -> ModelRoutingRecord:
    """Persist one routed call. Adds and flushes; the caller commits, so a
    telemetry failure can never lose the answer it describes."""
    record = ModelRoutingRecord(
        request_id=request_id,
        user_id=user_id,
        department_id=department_id,
        task_class=decision.task_class.value,
        policy=decision.policy.value,
        provider=decision.provider,
        model=decision.model,
        alternates=[candidate.key for candidate in decision.alternates],
        alternates_tried=alternates_tried,
        status=status,
        budget_action=decision.budget_action,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        estimated_cost_usd=decision.estimated_cost_usd,
        actual_cost_usd=_actual_cost_usd(decision, prompt_tokens, completion_tokens, settings),
    )
    db.add(record)
    await db.flush()
    return record


async def record_refused_routing(
    db: AsyncSession,
    *,
    task_class: str,
    provider: str | None,
    model: str | None,
    policy: str = "cheapest",
    estimated_cost_usd: Decimal | None = None,
    request_id: str | None = None,
    user_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
) -> ModelRoutingRecord:
    """Persist a routing the budget guard refused. The model was never
    called, so there are no tokens or latency to record."""
    record = ModelRoutingRecord(
        request_id=request_id,
        user_id=user_id,
        department_id=department_id,
        task_class=task_class,
        policy=policy,
        provider=provider,
        model=model,
        alternates=[],
        alternates_tried=0,
        status="refused",
        budget_action="refuse",
        estimated_cost_usd=estimated_cost_usd,
    )
    db.add(record)
    await db.flush()
    return record


async def routing_spent_this_month(db: AsyncSession, *, department_id: uuid.UUID | None = None) -> Decimal:
    """Spend so far this UTC month: actual cost where known, else the
    estimate. Refused calls never spent anything and are excluded."""
    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stmt = select(
        func.coalesce(
            func.sum(func.coalesce(ModelRoutingRecord.actual_cost_usd, ModelRoutingRecord.estimated_cost_usd)),
            0,
        )
    ).where(
        ModelRoutingRecord.created_at >= start,
        ModelRoutingRecord.status != "refused",
    )
    if department_id is not None:
        stmt = stmt.where(ModelRoutingRecord.department_id == department_id)
    total = (await db.execute(stmt)).scalar_one()
    return Decimal(str(total))


async def routing_cost_report(
    db: AsyncSession, *, days: int = 30, department_id: uuid.UUID | None = None
) -> list[TaskClassCost]:
    """Per-task-class cost/latency report. Cost per call is the actual cost
    where token counts were recorded, else the routing estimate; unpriced
    calls contribute nothing (they are never counted as free, they are
    simply absent from the total)."""
    start = datetime.now(UTC) - timedelta(days=days)
    filters = [ModelRoutingRecord.created_at >= start]
    if department_id is not None:
        filters.append(ModelRoutingRecord.department_id == department_id)
    rows = (
        await db.execute(
            select(
                ModelRoutingRecord.task_class,
                func.count(),
                func.coalesce(func.sum(ModelRoutingRecord.prompt_tokens), 0),
                func.coalesce(func.sum(ModelRoutingRecord.completion_tokens), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                ModelRoutingRecord.status != "refused",
                                func.coalesce(
                                    ModelRoutingRecord.actual_cost_usd,
                                    ModelRoutingRecord.estimated_cost_usd,
                                ),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.avg(ModelRoutingRecord.latency_ms),
            )
            .where(*filters)
            .group_by(ModelRoutingRecord.task_class)
            .order_by(func.count().desc())
        )
    ).all()
    refused_rows = (
        await db.execute(
            select(ModelRoutingRecord.task_class, func.count())
            .where(*filters, ModelRoutingRecord.status == "refused")
            .group_by(ModelRoutingRecord.task_class)
        )
    ).all()
    refused = {task_class: int(count) for task_class, count in refused_rows}
    return [
        TaskClassCost(
            task_class=task_class,
            calls=int(calls),
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            total_cost_usd=Decimal(str(total_cost)),
            avg_latency_ms=float(avg_latency) if avg_latency is not None else None,
            refused_calls=refused.get(task_class, 0),
        )
        for task_class, calls, prompt_tokens, completion_tokens, total_cost, avg_latency in rows
    ]
