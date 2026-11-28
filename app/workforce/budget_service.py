"""Step J (AI Workforce OS), slice 5: budget enforcement wiring.

The budget answers one question — "may this agent start work now?" —
from three facts: the registry's effective status (slice 3), what the
agent has already spent / called in this day and month, and the limits
in force. It grants nothing:

* Every gate goes through ``app.platform`` first for the API surface: a
  principal who is not a member of the organisation scope gets 404
  (``NotFoundError``), a member without the needed standing gets 403
  (``PermissionDeniedError``). Budgets are written by a
  ``workforce.manage`` holder only.
* ``evaluate_start`` is the internal enforcement check used by the
  Agent Studio run paths (start, worker claim, claim execution). It
  takes no principal because the caller is already authorized for the
  run itself — it only answers whether the workforce state allows the
  start.
* Spend is derived, never invented: Studio runs carry
  ``estimated_cost_usd`` from the provider pricing table (Step T's cost
  estimates feed the same field when that branch merges). Governance
  agents have no money data on this branch — their enforced budget is
  the call-window ``max_calls``/``calls_used`` the governance island
  already carries; profile agents have no usage data at all. AK's spend
  ledger is the merge-time source for cross-island spend; until then J
  derives spend from run rows and says so in the usage notes.
* A missing budget row means the plan defaults apply (monthly spend cap
  USD 10, plan §7.1). A budget row with a ``NULL`` limit lifts that
  limit explicitly — that is how an administrator records "no cap".

Enforcement points (all fail closed — a refused start never runs):

* ``AgentStudioService.start_run`` — a budget refusal raises before
  the run is created; a registry-paused/retired agent's run is created
  queued and never runs inline (same semantics as the island pause).
* ``claim_agent_studio_run`` — runs of registry-paused/retired agents
  are never claimed (the claim query excludes them, like island-paused
  agents).
* ``AgentStudioService.execute_claimed`` — a budget refusal hands the
  claim back (queued, attempt not counted, retry delayed an hour) so a
  run paused or over budget between claim and execution never runs.
* ``revoke_user_access`` — after the account is revoked, the workforce
  owner-revocation hook pauses the owner's agents and freezes their
  verified skills (slice 3). The hook never fails the revocation
  itself: the read view already derives the pause from the revoked
  owner, and an administrator can re-run the hook from the API.

Credentials issued to a revoked agent fail because credentials are
only opened inside run execution (``_execute``), which is unreachable
once starts are refused — and the revocation itself bumps the owner's
``auth_version``, so their sessions are refused too.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.permission_catalog import (
    PERMISSION_WORKFORCE_MANAGE,
    PERMISSION_WORKFORCE_READ,
)
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun
from app.db.models import AgentProfile, User
from app.db.workforce_budget_models import WorkforceBudget, WorkforceOrgQuota
from app.db.workforce_registry_models import WorkforceRegistryAgent
from app.governance.models import AgentIdentity
from app.platform import access as platform_access
from app.platform.access import Principal
from app.services import audit
from app.services.audit import AuditContext
from app.workforce.budget import (
    DEFAULT_MONTHLY_SPEND_LIMIT_USD,
    BudgetLimits,
    check_budget,
    day_window,
    month_window,
    validate_budget_limits,
)
from app.workforce.registry import (
    LIFECYCLE_ACTIVE,
    effective_lifecycle,
    island_status,
)
from app.workforce.skill_service import resolve_agent
from app.workforce.skills import validate_agent_kind

__all__ = ["StartDecision", "WorkforceBudgetService", "evaluate_start"]

ORG_QUOTA_KEY = "org"
BUDGET_RETRY_DELAY_SECONDS = 3600


@dataclass(frozen=True)
class StartDecision:
    """Whether an agent may start work now.

    ``reason`` is ``""`` when allowed, else a machine-readable code:
    ``"agent_not_found"``, ``"not_active:<status>"`` (registry /
    island / owner), or a budget code from ``app.workforce.budget``
    (``"monthly_spend_exceeded"``, ...).
    """

    allowed: bool
    reason: str = ""


async def _effective_status(db: AsyncSession, *, kind: str, agent_id: uuid.UUID) -> str:
    """Most-restrictive status from registry row + island + owner (plain queries)."""
    agent = await resolve_agent(db, kind, agent_id)
    if agent is None:
        return "disabled"
    row = await db.scalar(select(WorkforceRegistryAgent).where(
        WorkforceRegistryAgent.agent_kind == kind,
        WorkforceRegistryAgent.agent_id == agent_id))
    registry_state = row.lifecycle_state if row is not None else LIFECYCLE_ACTIVE
    if kind == "studio":
        island_row = await db.get(AgentStudioAgent, agent_id)
        island = island_status(
            "studio",
            enabled=bool(island_row and island_row.enabled),
            paused=bool(island_row and island_row.paused)) if island_row is not None else "disabled"
    elif kind == "governance":
        island_row = await db.get(AgentIdentity, agent_id)
        island = island_status(
            "governance", enabled=bool(island_row and island_row.enabled)) \
            if island_row is not None else "disabled"
    else:
        profile_row = await db.scalar(select(AgentProfile).where(
            AgentProfile.agent_key == agent_id, AgentProfile.enabled.is_(True)))
        island = island_status("profile", enabled=profile_row is not None)
    owner_revoked = False
    if agent.owner_id is not None:
        owner = await db.get(User, agent.owner_id)
        owner_revoked = bool(owner is not None and not owner.is_active)
    return effective_lifecycle(
        registry_state=registry_state, island_status_value=island,
        owner_revoked=owner_revoked)


async def _spent_and_calls(
    db: AsyncSession, *, kind: str, agent_id: uuid.UUID,
    day_start: datetime, month_start: datetime, now: datetime,
) -> tuple[Decimal, Decimal, int, int]:
    """(spent today, spent this month, calls today, calls this month).

    Studio usage is derived from run rows. Governance usage is the
    island's call-window counter (no money data on this branch).
    Profile agents have no usage data on this branch.
    """
    if kind == "studio":
        spent_today = await db.scalar(select(func.coalesce(
            func.sum(AgentStudioRun.estimated_cost_usd), Decimal("0"))).where(
                AgentStudioRun.agent_id == agent_id,
                AgentStudioRun.created_at >= day_start)) or Decimal("0")
        spent_month = await db.scalar(select(func.coalesce(
            func.sum(AgentStudioRun.estimated_cost_usd), Decimal("0"))).where(
                AgentStudioRun.agent_id == agent_id,
                AgentStudioRun.created_at >= month_start)) or Decimal("0")
        calls_today = await db.scalar(select(func.count()).select_from(AgentStudioRun).where(
            AgentStudioRun.agent_id == agent_id,
            AgentStudioRun.created_at >= day_start)) or 0
        calls_month = await db.scalar(select(func.count()).select_from(AgentStudioRun).where(
            AgentStudioRun.agent_id == agent_id,
            AgentStudioRun.created_at >= month_start)) or 0
        return Decimal(spent_today), Decimal(spent_month), int(calls_today), int(calls_month)
    if kind == "governance":
        row = await db.get(AgentIdentity, agent_id)
        calls = 0
        if row is not None and row.window_started_at is not None:
            from datetime import timedelta
            window_end = row.window_started_at + timedelta(seconds=row.window_seconds or 3600)
            calls = int(row.calls_used or 0) if now < window_end else 0
        return Decimal("0"), Decimal("0"), calls, calls
    return Decimal("0"), Decimal("0"), 0, 0


async def _org_usage(
    db: AsyncSession, *, month_start: datetime,
) -> tuple[Decimal, int]:
    """(org spend this month, org agent count) — Studio spend + all islands."""
    org_spent = await db.scalar(select(func.coalesce(
        func.sum(AgentStudioRun.estimated_cost_usd), Decimal("0"))).where(
            AgentStudioRun.created_at >= month_start)) or Decimal("0")
    studio_count = await db.scalar(
        select(func.count()).select_from(AgentStudioAgent)) or 0
    governance_count = await db.scalar(
        select(func.count()).select_from(AgentIdentity)) or 0
    profile_count = await db.scalar(select(func.count()).select_from(AgentProfile).where(
        AgentProfile.enabled.is_(True))) or 0
    return Decimal(org_spent), int(studio_count) + int(governance_count) + int(profile_count)


async def evaluate_start(
    db: AsyncSession, *, kind: str, agent_id: uuid.UUID, now: datetime | None = None,
) -> StartDecision:
    """May this agent start work now? (Internal enforcement check.)

    Combines the registry effective status with the spend/call limits
    in force. Unknown agents and non-active agents are refused; a
    refused start never runs. Never raises on bad input — bad data
    becomes a refusal.
    """
    moment = now or datetime.now(UTC)
    kind, error = validate_agent_kind(kind)
    if error is not None:
        return StartDecision(allowed=False, reason="agent_not_found")
    assert kind is not None
    if await resolve_agent(db, kind, agent_id) is None:
        return StartDecision(allowed=False, reason="agent_not_found")
    status = await _effective_status(db, kind=kind, agent_id=agent_id)
    if status != "active":
        return StartDecision(allowed=False, reason=f"not_active:{status}")
    day_start, _ = day_window(moment)
    month_start, _ = month_window(moment)
    row = await db.scalar(select(WorkforceBudget).where(
        WorkforceBudget.agent_kind == kind, WorkforceBudget.agent_id == agent_id))
    if row is None:
        limits = BudgetLimits(monthly_spend_limit_usd=DEFAULT_MONTHLY_SPEND_LIMIT_USD)
    else:
        limits = BudgetLimits(
            daily_spend_limit_usd=row.daily_spend_limit_usd,
            monthly_spend_limit_usd=row.monthly_spend_limit_usd,
            daily_call_limit=row.daily_call_limit,
            monthly_call_limit=row.monthly_call_limit)
    spent_today, spent_month, calls_today, calls_month = await _spent_and_calls(
        db, kind=kind, agent_id=agent_id,
        day_start=day_start, month_start=month_start, now=moment)
    quota = await db.scalar(select(WorkforceOrgQuota).where(
        WorkforceOrgQuota.key == ORG_QUOTA_KEY))
    org_spent, org_count = await _org_usage(db, month_start=month_start)
    check = check_budget(
        spent_today_usd=spent_today, spent_this_month_usd=spent_month,
        calls_today=calls_today, calls_this_month=calls_month, limits=limits,
        org_spent_this_month_usd=org_spent,
        org_monthly_spend_limit_usd=quota.monthly_spend_limit_usd if quota else None,
        org_agent_count=org_count,
        org_max_agents=quota.max_agents if quota else None)
    if not check.allowed:
        return StartDecision(allowed=False, reason=check.reason)
    return StartDecision(allowed=True, reason="")


class WorkforceBudgetService:
    """Per-agent budgets + org quotas (API surface over ``evaluate_start``)."""

    def __init__(self, db: AsyncSession, actor: Principal, ctx: AuditContext) -> None:
        self.db = db
        self.actor = actor
        self.ctx = ctx

    # -- gates -----------------------------------------------------------------

    async def _org_scope_id(self) -> uuid.UUID:
        return (await platform_access.ensure_org_scope(self.db)).id

    async def _require_manage(self) -> None:
        """``workforce.manage`` on the org scope (404 non-members, 403 others)."""
        scope_id = await self._org_scope_id()
        await platform_access.require(self.db, self.actor, PERMISSION_WORKFORCE_MANAGE, scope_id)

    async def _require_read(self) -> None:
        """``workforce.read`` or ``workforce.manage`` on the org scope."""
        scope_id = await self._org_scope_id()
        for permission in (PERMISSION_WORKFORCE_READ, PERMISSION_WORKFORCE_MANAGE):
            decision = await platform_access.authorize(
                self.db, self.actor, permission, scope_id)
            if decision.allowed:
                return
            if decision.reason in {"scope_missing", "not_a_member"}:
                raise NotFoundError("Not found.")
        raise PermissionDeniedError("You do not have permission to view workforce budgets.")

    # -- per-agent budgets -------------------------------------------------------

    async def _agent_or_404(self, agent_kind: str, agent_id: uuid.UUID):
        agent = await resolve_agent(self.db, agent_kind, agent_id)
        if agent is None:
            raise NotFoundError("Workforce agent not found.")
        return agent

    async def get_budget(self, agent_kind: str, agent_id: uuid.UUID) -> dict:
        """The limits in force for an agent, with the defaults spelled out."""
        await self._require_read()
        agent_kind, error = validate_agent_kind(agent_kind)
        if error is not None:
            raise InvalidInputError(error)
        assert agent_kind is not None
        await self._agent_or_404(agent_kind, agent_id)
        row = await self.db.scalar(select(WorkforceBudget).where(
            WorkforceBudget.agent_kind == agent_kind,
            WorkforceBudget.agent_id == agent_id))
        if row is None:
            return {
                "agent_kind": agent_kind, "agent_id": agent_id,
                "daily_spend_limit_usd": None,
                "monthly_spend_limit_usd": DEFAULT_MONTHLY_SPEND_LIMIT_USD,
                "daily_call_limit": None, "monthly_call_limit": None,
                "explicit": False,
                "note": "Plan defaults: monthly spend cap USD 10; no other caps.",
            }
        return {
            "agent_kind": agent_kind, "agent_id": agent_id,
            "daily_spend_limit_usd": row.daily_spend_limit_usd,
            "monthly_spend_limit_usd": row.monthly_spend_limit_usd,
            "daily_call_limit": row.daily_call_limit,
            "monthly_call_limit": row.monthly_call_limit,
            "explicit": True,
            "note": "Explicit limits: a NULL limit means no cap for that window.",
        }

    async def set_budget(
        self, agent_kind: str, agent_id: uuid.UUID, *, limits: BudgetLimits,
    ) -> dict:
        """Set explicit limits for an agent (``workforce.manage`` only)."""
        await self._require_manage()
        agent_kind, error = validate_agent_kind(agent_kind)
        if error is not None:
            raise InvalidInputError(error)
        assert agent_kind is not None
        await self._agent_or_404(agent_kind, agent_id)
        problems = validate_budget_limits(limits)
        if problems:
            raise InvalidInputError("; ".join(problems))
        row = await self.db.scalar(select(WorkforceBudget).where(
            WorkforceBudget.agent_kind == agent_kind,
            WorkforceBudget.agent_id == agent_id).with_for_update())
        if row is None:
            row = WorkforceBudget(
                agent_kind=agent_kind, agent_id=agent_id,
                updated_by_user_id=self.actor.user_id)
            self.db.add(row)
        row.daily_spend_limit_usd = limits.daily_spend_limit_usd
        row.monthly_spend_limit_usd = limits.monthly_spend_limit_usd
        row.daily_call_limit = limits.daily_call_limit
        row.monthly_call_limit = limits.monthly_call_limit
        row.updated_by_user_id = self.actor.user_id
        await self.db.flush()
        await self.db.refresh(row)
        audit.record(self.db, self.ctx, "workforce.budget_set", target=str(agent_id),
                     details={"agent_kind": agent_kind,
                              "daily_spend_limit_usd": str(limits.daily_spend_limit_usd),
                              "monthly_spend_limit_usd": str(limits.monthly_spend_limit_usd),
                              "daily_call_limit": limits.daily_call_limit,
                              "monthly_call_limit": limits.monthly_call_limit})
        await self.db.commit()
        return await self.get_budget(agent_kind, agent_id)

    async def clear_budget(self, agent_kind: str, agent_id: uuid.UUID) -> None:
        """Drop an agent's explicit limits (plan defaults apply again)."""
        await self._require_manage()
        agent_kind, error = validate_agent_kind(agent_kind)
        if error is not None:
            raise InvalidInputError(error)
        assert agent_kind is not None
        await self._agent_or_404(agent_kind, agent_id)
        row = await self.db.scalar(select(WorkforceBudget).where(
            WorkforceBudget.agent_kind == agent_kind,
            WorkforceBudget.agent_id == agent_id).with_for_update())
        if row is not None:
            await self.db.delete(row)
        audit.record(self.db, self.ctx, "workforce.budget_cleared", target=str(agent_id),
                     details={"agent_kind": agent_kind})
        await self.db.commit()

    async def check_start(
        self, agent_kind: str, agent_id: uuid.UUID,
    ) -> StartDecision:
        """May this agent start work now? (``workforce.read`` or better.)"""
        await self._require_read()
        agent_kind, error = validate_agent_kind(agent_kind)
        if error is not None:
            raise InvalidInputError(error)
        assert agent_kind is not None
        await self._agent_or_404(agent_kind, agent_id)
        return await evaluate_start(self.db, kind=agent_kind, agent_id=agent_id)

    # -- org quotas --------------------------------------------------------------

    async def get_org_quota(self) -> dict:
        """The org quotas in force (``workforce.read`` or better)."""
        await self._require_read()
        quota = await self.db.scalar(select(WorkforceOrgQuota).where(
            WorkforceOrgQuota.key == ORG_QUOTA_KEY))
        return {
            "max_agents": quota.max_agents if quota else None,
            "monthly_spend_limit_usd": quota.monthly_spend_limit_usd if quota else None,
            "explicit": quota is not None,
            "note": "Unset means no org cap (plan §7.5); per-agent limits still apply.",
        }

    async def set_org_quota(
        self, *, max_agents: int | None, monthly_spend_limit_usd: Decimal | None,
    ) -> dict:
        """Set the org quotas (``workforce.manage`` only)."""
        await self._require_manage()
        if max_agents is not None and (
                not isinstance(max_agents, int) or isinstance(max_agents, bool)
                or max_agents < 0):
            raise InvalidInputError("Org max agents must be a non-negative integer.")
        if monthly_spend_limit_usd is not None:
            try:
                monthly_spend_limit_usd = Decimal(str(monthly_spend_limit_usd))
            except Exception:
                raise InvalidInputError("Org monthly spend limit must be an amount.") from None
            if monthly_spend_limit_usd < 0:
                raise InvalidInputError("Org monthly spend limit must be non-negative.")
        quota = await self.db.scalar(select(WorkforceOrgQuota).where(
            WorkforceOrgQuota.key == ORG_QUOTA_KEY).with_for_update())
        if quota is None:
            quota = WorkforceOrgQuota(key=ORG_QUOTA_KEY,
                                      updated_by_user_id=self.actor.user_id)
            self.db.add(quota)
        quota.max_agents = max_agents
        quota.monthly_spend_limit_usd = monthly_spend_limit_usd
        quota.updated_by_user_id = self.actor.user_id
        await self.db.flush()
        await self.db.refresh(quota)
        audit.record(self.db, self.ctx, "workforce.org_quota_set", target="org",
                     details={"max_agents": max_agents,
                              "monthly_spend_limit_usd": str(monthly_spend_limit_usd)})
        await self.db.commit()
        return await self.get_org_quota()
