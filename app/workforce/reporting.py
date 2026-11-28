"""Step J (AI Workforce OS), slice 6: workforce reporting (read-only).

The reporting answers "how is the workforce doing?" — org-wide
utilization, success/failure, and cost per agent and per task. It reads
only:

* Every gate goes through ``app.platform`` first: a principal who is not
  a member of the organisation scope gets 404 (``NotFoundError``), a
  member without ``workforce.read`` / ``workforce.manage`` gets 403.
* The numbers come from facts already recorded: Studio run rows
  (status, duration, tokens, ``estimated_cost_usd``), the registry view
  (slice 3), the delegation audit (slice 4) and the budget rows
  (slice 5). Nothing here writes, dispatches, or re-prices anything.
* Money is the recorded cost of runs already done (``estimated_cost_usd``
  from the provider pricing table; Step T feeds the same field when
  that branch merges). Prices are never invented. Governance and profile
  agents have no run rows on this branch, so their reports show zero
  runs with a note saying so — never a guessed number.
* Step N (Cost Intelligence) consumes these per-agent facts for ROI;
  J produces them, N aggregates them — no duplication.

Ranges are UTC calendar windows ending now (``days`` back, default 30,
max 365). A run counts in the window its ``created_at`` falls in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.permission_catalog import (
    PERMISSION_WORKFORCE_MANAGE,
    PERMISSION_WORKFORCE_READ,
)
from app.db.agent_studio_models import AgentStudioRun
from app.db.workforce_budget_models import WorkforceOrgQuota
from app.db.workforce_delegation_models import WorkforceDelegationAudit
from app.platform import access as platform_access
from app.platform.access import Principal
from app.services.audit import AuditContext
from app.workforce.budget_service import ORG_QUOTA_KEY, evaluate_start
from app.workforce.registry_service import WorkforceRegistryService
from app.workforce.skill_service import resolve_agent
from app.workforce.skills import validate_agent_kind

__all__ = [
    "AgentReport",
    "OrgReport",
    "SpendReport",
    "WorkforceReportingService",
    "avg_duration_ms",
    "cost_per_task",
    "success_rate",
]

DEFAULT_REPORT_DAYS = 30
MAX_REPORT_DAYS = 365
_RUN_STATUSES = ("queued", "running", "awaiting_approval", "completed", "failed", "cancelled")


def success_rate(completed: int, failed: int) -> float | None:
    """Share of terminal runs that completed; None when there are none."""
    terminal = completed + failed
    if terminal <= 0:
        return None
    return round(completed / terminal, 4)


def avg_duration_ms(durations_ms: list[float]) -> float | None:
    """Mean run duration in ms; None when no finished run was measured."""
    if not durations_ms:
        return None
    return round(sum(durations_ms) / len(durations_ms), 2)


def cost_per_task(total_cost_usd: Decimal, runs: int) -> Decimal | None:
    """Mean recorded cost per run; None when there are no runs."""
    if runs <= 0:
        return None
    return (total_cost_usd / runs).quantize(Decimal("0.00000001"))


@dataclass(frozen=True)
class AgentReport:
    """One agent's workforce report for the window."""

    kind: str
    agent_id: uuid.UUID
    display_name: str
    owner_id: uuid.UUID | None
    effective_status: str
    can_start: bool
    start_reason: str
    runs: int
    runs_by_status: dict[str, int]
    completed: int
    failed: int
    success_rate: float | None
    avg_duration_ms: float | None
    total_cost_usd: Decimal
    total_prompt_tokens: int
    total_completion_tokens: int
    total_llm_calls: int
    cost_per_task_usd: Decimal | None
    current_skills: tuple[str, ...]
    delegations_as_parent: int
    delegations_as_child: int
    delegations_denied: int
    monthly_spend_limit_usd: Decimal | None
    usage_note: str


@dataclass(frozen=True)
class OrgReport:
    """The org-wide workforce report for the window."""

    days: int
    window_start: datetime
    agents: tuple[AgentReport, ...]
    total_agents: int
    agents_by_status: dict[str, int]
    total_runs: int
    total_completed: int
    total_failed: int
    success_rate: float | None
    total_cost_usd: Decimal
    org_monthly_spend_limit_usd: Decimal | None
    org_max_agents: int | None


@dataclass(frozen=True)
class SpendReport:
    """Cost per agent and per task for the window (feeds Step N)."""

    days: int
    window_start: datetime
    total_cost_usd: Decimal
    total_runs: int
    cost_per_task_usd: Decimal | None
    by_agent: tuple[dict, ...]


class WorkforceReportingService:
    """Read-only org-wide workforce reporting."""

    def __init__(self, db: AsyncSession, actor: Principal, ctx: AuditContext) -> None:
        self.db = db
        self.actor = actor
        self.ctx = ctx

    # -- gates -----------------------------------------------------------------

    async def _require_read(self) -> None:
        """``workforce.read`` or ``workforce.manage`` on the org scope."""
        scope_id = (await platform_access.ensure_org_scope(self.db)).id
        for permission in (PERMISSION_WORKFORCE_READ, PERMISSION_WORKFORCE_MANAGE):
            decision = await platform_access.authorize(
                self.db, self.actor, permission, scope_id)
            if decision.allowed:
                return
            if decision.reason in {"scope_missing", "not_a_member"}:
                raise NotFoundError("Not found.")
        raise PermissionDeniedError("You do not have permission to view workforce reports.")

    @staticmethod
    def _window(days: int) -> tuple[int, datetime]:
        if not isinstance(days, int) or isinstance(days, bool) or days < 1:
            raise InvalidInputError("Report days must be an integer of at least 1.")
        days = min(days, MAX_REPORT_DAYS)
        return days, datetime.now(UTC) - timedelta(days=days)

    # -- aggregation -------------------------------------------------------------

    async def _agent_report(
        self, *, kind: str, agent_id: uuid.UUID, days: int, window_start: datetime,
        runs: list[AgentStudioRun] | None = None,
        delegations: list[WorkforceDelegationAudit] | None = None,
    ) -> AgentReport:
        registry = WorkforceRegistryService(self.db, self.actor, self.ctx)
        view = await registry.get_agent(kind, agent_id)
        decision = await evaluate_start(self.db, kind=kind, agent_id=agent_id)
        agent_runs: list[AgentStudioRun] = []
        usage_note = ""
        if kind == "studio":
            if runs is None:
                rows = await self.db.scalars(select(AgentStudioRun).where(
                    AgentStudioRun.agent_id == agent_id,
                    AgentStudioRun.created_at >= window_start))
                agent_runs = list(rows.all())
            else:
                agent_runs = [run for run in runs if run.agent_id == agent_id]
        else:
            usage_note = (
                "No run rows on this branch for governance/profile agents; "
                "utilization and cost are reported as zero, never estimated.")
        counts = {status: 0 for status in _RUN_STATUSES}
        durations: list[float] = []
        total_cost = Decimal("0")
        prompt_tokens = 0
        completion_tokens = 0
        llm_calls = 0
        for run in agent_runs:
            if run.status in counts:
                counts[run.status] += 1
            if run.started_at is not None and run.finished_at is not None:
                durations.append(
                    (run.finished_at - run.started_at).total_seconds() * 1000)
            if run.estimated_cost_usd is not None:
                total_cost += Decimal(run.estimated_cost_usd)
            prompt_tokens += int(run.prompt_tokens or 0)
            completion_tokens += int(run.completion_tokens or 0)
            llm_calls += int(run.llm_calls or 0)
        if delegations is None:
            rows = await self.db.scalars(select(WorkforceDelegationAudit).where(
                WorkforceDelegationAudit.created_at >= window_start))
            delegations = list(rows.all())
        as_parent = sum(
            1 for row in delegations
            if row.parent_kind == kind and row.parent_agent_id == agent_id)
        as_child = sum(
            1 for row in delegations
            if row.child_kind == kind and row.child_agent_id == agent_id)
        denied = sum(
            1 for row in delegations
            if row.decision == "denied"
            and ((row.parent_kind == kind and row.parent_agent_id == agent_id)
                 or (row.child_kind == kind and row.child_agent_id == agent_id)))
        from app.workforce.budget import DEFAULT_MONTHLY_SPEND_LIMIT_USD
        from app.workforce.budget_service import WorkforceBudgetService

        budget = await WorkforceBudgetService(
            self.db, self.actor, self.ctx).get_budget(kind, agent_id)
        return AgentReport(
            kind=kind, agent_id=agent_id, display_name=view.display_name,
            owner_id=view.owner_id, effective_status=view.effective_status,
            can_start=decision.allowed, start_reason=decision.reason,
            runs=len(agent_runs), runs_by_status=counts,
            completed=counts["completed"], failed=counts["failed"],
            success_rate=success_rate(counts["completed"], counts["failed"]),
            avg_duration_ms=avg_duration_ms(durations),
            total_cost_usd=total_cost,
            total_prompt_tokens=prompt_tokens,
            total_completion_tokens=completion_tokens,
            total_llm_calls=llm_calls,
            cost_per_task_usd=cost_per_task(total_cost, len(agent_runs)),
            current_skills=view.current_skills,
            delegations_as_parent=as_parent, delegations_as_child=as_child,
            delegations_denied=denied,
            monthly_spend_limit_usd=(
                budget["monthly_spend_limit_usd"]
                if budget["monthly_spend_limit_usd"] is not None
                else DEFAULT_MONTHLY_SPEND_LIMIT_USD),
            usage_note=usage_note)

    async def agent_report(
        self, agent_kind: str, agent_id: uuid.UUID, *, days: int = DEFAULT_REPORT_DAYS,
    ) -> AgentReport:
        """One agent's report (``workforce.read`` or better)."""
        await self._require_read()
        agent_kind, error = validate_agent_kind(agent_kind)
        if error is not None:
            raise InvalidInputError(error)
        assert agent_kind is not None
        if await resolve_agent(self.db, agent_kind, agent_id) is None:
            raise NotFoundError("Workforce agent not found.")
        days, window_start = self._window(days)
        return await self._agent_report(
            kind=agent_kind, agent_id=agent_id, days=days, window_start=window_start)

    async def org_report(self, *, days: int = DEFAULT_REPORT_DAYS) -> OrgReport:
        """The org-wide report (``workforce.read`` or better)."""
        await self._require_read()
        days, window_start = self._window(days)
        registry = WorkforceRegistryService(self.db, self.actor, self.ctx)
        views = await registry.list_agents()
        runs = list((await self.db.scalars(select(AgentStudioRun).where(
            AgentStudioRun.created_at >= window_start))).all())
        delegations = list((await self.db.scalars(select(WorkforceDelegationAudit).where(
            WorkforceDelegationAudit.created_at >= window_start))).all())
        reports: list[AgentReport] = []
        for view in views:
            reports.append(await self._agent_report(
                kind=view.kind, agent_id=view.agent_id, days=days,
                window_start=window_start, runs=runs, delegations=delegations))
        by_status: dict[str, int] = {}
        for report in reports:
            by_status[report.effective_status] = by_status.get(report.effective_status, 0) + 1
        total_cost = sum((report.total_cost_usd for report in reports), Decimal("0"))
        quota = await self.db.scalar(select(WorkforceOrgQuota).where(
            WorkforceOrgQuota.key == ORG_QUOTA_KEY))
        return OrgReport(
            days=days, window_start=window_start,
            agents=tuple(reports), total_agents=len(reports),
            agents_by_status=by_status,
            total_runs=sum(report.runs for report in reports),
            total_completed=sum(report.completed for report in reports),
            total_failed=sum(report.failed for report in reports),
            success_rate=success_rate(
                sum(report.completed for report in reports),
                sum(report.failed for report in reports)),
            total_cost_usd=total_cost,
            org_monthly_spend_limit_usd=(
                quota.monthly_spend_limit_usd if quota else None),
            org_max_agents=quota.max_agents if quota else None)

    async def spend_report(self, *, days: int = DEFAULT_REPORT_DAYS) -> SpendReport:
        """Cost per agent and per task (``workforce.read`` or better)."""
        await self._require_read()
        days, window_start = self._window(days)
        report = await self.org_report(days=days)
        by_agent = tuple({
            "agent_kind": item.kind,
            "agent_id": str(item.agent_id),
            "display_name": item.display_name,
            "runs": item.runs,
            "total_cost_usd": str(item.total_cost_usd),
            "cost_per_task_usd": (
                str(item.cost_per_task_usd) if item.cost_per_task_usd is not None else None),
            "total_llm_calls": item.total_llm_calls,
        } for item in report.agents)
        return SpendReport(
            days=days, window_start=window_start,
            total_cost_usd=report.total_cost_usd, total_runs=report.total_runs,
            cost_per_task_usd=cost_per_task(report.total_cost_usd, report.total_runs),
            by_agent=by_agent)
