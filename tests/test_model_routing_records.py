"""Step T slice 3: routing records, cost/latency report, budget guard.

Pure budget decisions are tested without a database; persistence and the
report run against the throwaway *_test database. Records carry ids and
numbers only -- never prompt text, answers, keys or endpoints.
"""

import asyncio
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db.session import test_engine_options as _engine_options
from app.services.model_router import (
    BudgetAction,
    ModelCandidate,
    RoutingPolicy,
    TaskClass,
    check_budget,
    route,
)
from app.services.routing_records import (
    record_refused_routing,
    record_routing,
    routing_cost_report,
    routing_spent_this_month,
)
from tests.conftest import TEST_DATABASE_URL
from tests.test_model_router_wiring import PRICING, _run

D = Decimal


def _flash() -> ModelCandidate:
    return ModelCandidate(
        provider="deepseek",
        model="deepseek-flash",
        context_window=1_000_000,
        max_output_tokens=384_000,
        vision=True,
        capability_tier=1,
        latency_tier=1,
        input_per_1m_tokens_usd=D("0.10"),
        output_per_1m_tokens_usd=D("0.40"),
    )


def _pro() -> ModelCandidate:
    return ModelCandidate(
        provider="deepseek",
        model="deepseek-v4-pro",
        context_window=1_000_000,
        max_output_tokens=384_000,
        capability_tier=3,
        latency_tier=3,
        input_per_1m_tokens_usd=D("2.00"),
        output_per_1m_tokens_usd=D("8.00"),
    )


def _settings():
    return Settings(llm_model_pricing_json=PRICING)


# ------------------------------------------------------------ pure budget guard


def test_check_budget_allows_without_budget():
    decision = check_budget(TaskClass.COMPLEX, spent_usd=D("999"), estimated_cost_usd=D("5"), monthly_budget_usd=None)
    assert decision.action is BudgetAction.ALLOW


def test_check_budget_allows_when_it_fits():
    decision = check_budget(
        TaskClass.STANDARD, spent_usd=D("1.00"), estimated_cost_usd=D("0.50"), monthly_budget_usd=D("2.00")
    )
    assert decision.action is BudgetAction.ALLOW


def test_check_budget_downgrades_simple_tasks_over_budget():
    decision = check_budget(
        TaskClass.SIMPLE, spent_usd=D("1.90"), estimated_cost_usd=D("0.50"), monthly_budget_usd=D("2.00")
    )
    assert decision.action is BudgetAction.DOWNGRADE
    assert "downgraded" in decision.reason


def test_check_budget_refuses_complex_tasks_over_budget():
    decision = check_budget(
        TaskClass.COMPLEX, spent_usd=D("1.90"), estimated_cost_usd=D("0.50"), monthly_budget_usd=D("2.00")
    )
    assert decision.action is BudgetAction.REFUSE


def test_check_budget_refuses_unpriced_models():
    # Unknown cost is never treated as free.
    decision = check_budget(
        TaskClass.SIMPLE, spent_usd=D("0"), estimated_cost_usd=None, monthly_budget_usd=D("2.00")
    )
    assert decision.action is BudgetAction.REFUSE


# ------------------------------------------------------------ persistence + report


def test_record_and_report_match():
    settings = _settings()

    async def _go(session):
        simple = route(TaskClass.SIMPLE, [_flash(), _pro()], estimated_prompt_tokens=100, estimated_completion_tokens=50)
        complex_decision = route(
            TaskClass.COMPLEX, [_flash(), _pro()],
            estimated_prompt_tokens=1000, estimated_completion_tokens=500, policy=RoutingPolicy.BALANCED,
        )
        await record_routing(
            session, simple, prompt_tokens=100, completion_tokens=50, latency_ms=120, settings=settings
        )
        await record_routing(
            session, simple, prompt_tokens=100, completion_tokens=50, latency_ms=80, settings=settings
        )
        await record_routing(
            session, complex_decision, prompt_tokens=1000, completion_tokens=500, latency_ms=900,
            settings=settings,
        )
        await record_refused_routing(
            session, task_class="simple", provider="deepseek", model="deepseek-flash",
            estimated_cost_usd=D("0.0001"),
        )
        report = await routing_cost_report(session, days=30)
        spent = await routing_spent_this_month(session)
        return report, spent

    report, spent = _run(_go)
    assert {row.task_class for row in report} == {"simple", "complex"}
    simple = next(row for row in report if row.task_class == "simple")
    assert simple.calls == 3  # two ok + one refused
    assert simple.prompt_tokens == 200 and simple.completion_tokens == 100
    # Actual flash cost per call: (100*0.10 + 50*0.40) / 1e6 = 0.00003; refused adds nothing.
    assert simple.total_cost_usd == D("0.00006")
    assert simple.avg_latency_ms == 100.0  # refused calls carry no latency
    assert simple.refused_calls == 1
    complex_row = next(row for row in report if row.task_class == "complex")
    assert complex_row.calls == 1
    # Actual pro cost: (1000*2.00 + 500*8.00) / 1e6 = 0.006.
    assert complex_row.total_cost_usd == D("0.006")
    assert complex_row.avg_latency_ms == 900.0
    assert complex_row.refused_calls == 0
    assert spent == D("0.00606")


def test_record_stores_ids_and_numbers_only():
    settings = _settings()

    async def _go(session):
        decision = route(TaskClass.SIMPLE, [_flash(), _pro()])
        record = await record_routing(
            session, decision, prompt_tokens=10, completion_tokens=5, latency_ms=42,
            request_id="req-1", settings=settings,
        )
        return record

    record = _run(_go)
    assert record.task_class == "simple"
    assert record.provider == "deepseek" and record.model == "deepseek-flash"
    assert record.alternates == ["deepseek:deepseek-v4-pro"]
    assert record.status == "ok" and record.budget_action == "allow"
    assert record.request_id == "req-1"
    assert record.latency_ms == 42


def test_report_is_empty_without_records():
    assert _run(lambda session: routing_cost_report(session, days=30)) == []
    assert _run(lambda session: routing_spent_this_month(session)) == D("0")


def test_report_and_spent_filter_by_department():
    settings = _settings()
    department = uuid.uuid4()

    async def _go(session):
        decision = route(TaskClass.SIMPLE, [_flash(), _pro()])
        await record_routing(
            session, decision, prompt_tokens=100, completion_tokens=50, latency_ms=10,
            department_id=department, settings=settings,
        )
        await record_routing(
            session, decision, prompt_tokens=100, completion_tokens=50, latency_ms=20, settings=settings
        )
        dept_report = await routing_cost_report(session, days=30, department_id=department)
        all_report = await routing_cost_report(session, days=30)
        dept_spent = await routing_spent_this_month(session, department_id=department)
        all_spent = await routing_spent_this_month(session)
        return dept_report, all_report, dept_spent, all_spent

    dept_report, all_report, dept_spent, all_spent = _run(_go)
    assert len(dept_report) == 1 and dept_report[0].calls == 1
    assert len(all_report) == 1 and all_report[0].calls == 2
    assert dept_spent == D("0.00003")
    assert all_spent == D("0.00006")
