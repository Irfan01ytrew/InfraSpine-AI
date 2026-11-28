"""Step J (AI Workforce OS), slice 3: org registry + lifecycle API.

Read-only registry views for every agent in the org, lifecycle actions
(pause / resume / retire), per-agent lifecycle history, and the
owner-revocation hook. All gates live in
``app.workforce.registry_service`` (platform ``require``/``authorize`` —
404 non-members, 403 without standing); this module only maps the
service onto HTTP.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession, audit_context
from app.platform import access as platform_access
from app.workforce.delegation import DelegationPolicy
from app.workforce.delegation_service import (
    DelegationAuditView,
    WorkforceDelegationService,
)
from app.workforce.registry_service import RegistryAgentView, WorkforceRegistryService

router = APIRouter(prefix="/api/workforce", tags=["workforce"])


class RegistryAgentOut(BaseModel):
    agent_kind: str
    agent_id: uuid.UUID
    display_name: str
    owner_user_id: uuid.UUID | None
    manager_group_id: uuid.UUID | None
    registry_state: str
    island_status: str
    effective_status: str
    owner_revoked: bool
    current_skills: list[str]
    notes: list[str]


class LifecycleEventOut(BaseModel):
    agent_kind: str
    agent_id: uuid.UUID
    from_state: str | None
    to_state: str
    actor_user_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None
    reason: str
    created_at: datetime


class LifecycleRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class OwnerRevocationRequest(BaseModel):
    owner_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=500)


class OwnerRevocationOut(BaseModel):
    agents_paused: int
    agents_already_paused: int
    agents_retired_skipped: int
    skills_frozen: int


def _service(db: DbSession, user: CurrentUser, request: Request) -> WorkforceRegistryService:
    return WorkforceRegistryService(
        db, platform_access.principal_for(user), audit_context(request, user))


def _view_out(view: RegistryAgentView) -> RegistryAgentOut:
    return RegistryAgentOut(
        agent_kind=view.kind,
        agent_id=view.agent_id,
        display_name=view.display_name,
        owner_user_id=view.owner_id,
        manager_group_id=view.manager_group_id,
        registry_state=view.registry_state,
        island_status=view.island_status,
        effective_status=view.effective_status,
        owner_revoked=view.owner_revoked,
        current_skills=list(view.current_skills),
        notes=list(view.notes),
    )


@router.get("/agents", response_model=list[RegistryAgentOut])
async def list_agents(
    request: Request, db: DbSession, user: CurrentUser,
) -> list[RegistryAgentOut]:
    views = await _service(db, user, request).list_agents()
    return [_view_out(view) for view in views]


@router.get("/agents/{agent_kind}/{agent_id}", response_model=RegistryAgentOut)
async def get_agent(
    agent_kind: str, agent_id: uuid.UUID,
    request: Request, db: DbSession, user: CurrentUser,
) -> RegistryAgentOut:
    view = await _service(db, user, request).get_agent(agent_kind, agent_id)
    return _view_out(view)


@router.get("/agents/{agent_kind}/{agent_id}/history",
            response_model=list[LifecycleEventOut])
async def lifecycle_history(
    agent_kind: str, agent_id: uuid.UUID,
    request: Request, db: DbSession, user: CurrentUser,
) -> list[LifecycleEventOut]:
    events = await _service(db, user, request).lifecycle_history(agent_kind, agent_id)
    return [LifecycleEventOut(
        agent_kind=event.agent_kind,
        agent_id=event.agent_id,
        from_state=event.from_state,
        to_state=event.to_state,
        actor_user_id=event.actor_user_id,
        owner_user_id=event.owner_user_id,
        reason=event.reason,
        created_at=event.created_at,
    ) for event in events]


@router.post("/agents/{agent_kind}/{agent_id}/pause", response_model=RegistryAgentOut)
async def pause_agent(
    agent_kind: str, agent_id: uuid.UUID, payload: LifecycleRequest,
    request: Request, db: DbSession, user: CurrentUser,
) -> RegistryAgentOut:
    view = await _service(db, user, request).pause_agent(
        agent_kind, agent_id, reason=payload.reason)
    return _view_out(view)


@router.post("/agents/{agent_kind}/{agent_id}/resume", response_model=RegistryAgentOut)
async def resume_agent(
    agent_kind: str, agent_id: uuid.UUID, payload: LifecycleRequest,
    request: Request, db: DbSession, user: CurrentUser,
) -> RegistryAgentOut:
    view = await _service(db, user, request).resume_agent(
        agent_kind, agent_id, reason=payload.reason)
    return _view_out(view)


@router.post("/agents/{agent_kind}/{agent_id}/retire", response_model=RegistryAgentOut)
async def retire_agent(
    agent_kind: str, agent_id: uuid.UUID, payload: LifecycleRequest,
    request: Request, db: DbSession, user: CurrentUser,
) -> RegistryAgentOut:
    view = await _service(db, user, request).retire_agent(
        agent_kind, agent_id, reason=payload.reason)
    return _view_out(view)


@router.post("/owner-revocations", response_model=OwnerRevocationOut)
async def apply_owner_revocation(
    payload: OwnerRevocationRequest,
    request: Request, db: DbSession, user: CurrentUser,
) -> OwnerRevocationOut:
    counts = await _service(db, user, request).apply_owner_revocation(
        payload.owner_id, reason=payload.reason)
    return OwnerRevocationOut(**counts)


# -- delegation policy (slice 4) -----------------------------------------------


class DelegationRuleIn(BaseModel):
    from_kind: str
    to_kind: str
    from_agent_id: uuid.UUID | None = None
    to_agent_id: uuid.UUID | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    allowed_scopes: list[str] = Field(default_factory=list)
    allows_sensitive: bool = False


class DelegationPolicyIn(BaseModel):
    rules: list[DelegationRuleIn] = Field(default_factory=list)
    max_depth: int = 2


class DelegationPolicyOut(BaseModel):
    version: int
    max_depth: int
    is_active: bool
    rules: list[dict]


class DelegationEvaluateIn(BaseModel):
    parent_kind: str
    parent_agent_id: uuid.UUID
    child_kind: str
    child_agent_id: uuid.UUID
    action: str = ""
    scope_id: str = ""
    sensitivity: str = "standard"
    depth: int = 1
    requested_tools: list[str] = Field(default_factory=list)
    originator_user_id: uuid.UUID | None = None
    approver_user_id: uuid.UUID | None = None


class DelegationDecisionOut(BaseModel):
    allowed: bool
    reason: str
    policy_version: int


class DelegationAuditOut(BaseModel):
    id: uuid.UUID
    parent_kind: str
    parent_agent_id: uuid.UUID
    child_kind: str
    child_agent_id: uuid.UUID
    parent_run_id: uuid.UUID | None
    child_run_id: uuid.UUID | None
    child_run_kind: str
    policy_version: int
    decision: str
    reason: str
    action: str
    scope_id: str
    depth: int


def _delegation_service(
    db: DbSession, user: CurrentUser, request: Request,
) -> WorkforceDelegationService:
    return WorkforceDelegationService(
        db, platform_access.principal_for(user), audit_context(request, user))


def _policy_out(policy: DelegationPolicy) -> DelegationPolicyOut:
    from app.workforce.delegation import policy_to_dict

    data = policy_to_dict(policy)
    return DelegationPolicyOut(
        version=policy.version, max_depth=policy.max_depth,
        is_active=policy.is_active, rules=data["rules"])


def _audit_out(view: DelegationAuditView) -> DelegationAuditOut:
    return DelegationAuditOut(
        id=view.id, parent_kind=view.parent_kind, parent_agent_id=view.parent_agent_id,
        child_kind=view.child_kind, child_agent_id=view.child_agent_id,
        parent_run_id=view.parent_run_id, child_run_id=view.child_run_id,
        child_run_kind=view.child_run_kind, policy_version=view.policy_version,
        decision=view.decision, reason=view.reason, action=view.action,
        scope_id=view.scope_id, depth=view.depth)


@router.post("/delegation-policies", response_model=DelegationPolicyOut)
async def create_delegation_policy(
    payload: DelegationPolicyIn,
    request: Request, db: DbSession, user: CurrentUser,
) -> DelegationPolicyOut:
    policy = await _delegation_service(db, user, request).create_policy(
        [rule.model_dump() for rule in payload.rules], max_depth=payload.max_depth)
    return _policy_out(policy)


@router.get("/delegation-policies", response_model=list[DelegationPolicyOut])
async def list_delegation_policies(
    request: Request, db: DbSession, user: CurrentUser,
) -> list[DelegationPolicyOut]:
    policies = await _delegation_service(db, user, request).list_policies()
    return [_policy_out(policy) for policy in policies]


@router.get("/delegation-policies/active", response_model=DelegationPolicyOut)
async def active_delegation_policy(
    request: Request, db: DbSession, user: CurrentUser,
) -> DelegationPolicyOut:
    from app.core.errors import NotFoundError

    policy = await _delegation_service(db, user, request).active_policy()
    if policy is None:
        raise NotFoundError("No active delegation policy.")
    return _policy_out(policy)


@router.post("/delegations/evaluate", response_model=DelegationDecisionOut)
async def evaluate_delegation_route(
    payload: DelegationEvaluateIn,
    request: Request, db: DbSession, user: CurrentUser,
) -> DelegationDecisionOut:
    decision = await _delegation_service(db, user, request).evaluate(
        payload.parent_kind, payload.parent_agent_id,
        payload.child_kind, payload.child_agent_id,
        action=payload.action, scope_id=payload.scope_id,
        sensitivity=payload.sensitivity, depth=payload.depth,
        requested_tools=frozenset(payload.requested_tools),
        originator_user_id=payload.originator_user_id,
        approver_user_id=payload.approver_user_id)
    return DelegationDecisionOut(
        allowed=decision.allowed, reason=decision.reason,
        policy_version=decision.policy_version)


@router.get("/delegations", response_model=list[DelegationAuditOut])
async def list_delegations(
    request: Request, db: DbSession, user: CurrentUser,
) -> list[DelegationAuditOut]:
    audits = await _delegation_service(db, user, request).list_audits()
    return [_audit_out(view) for view in audits]


# -- budgets (slice 5) -----------------------------------------------------------


class BudgetIn(BaseModel):
    daily_spend_limit_usd: float | None = None
    monthly_spend_limit_usd: float | None = None
    daily_call_limit: int | None = None
    monthly_call_limit: int | None = None


class BudgetOut(BaseModel):
    agent_kind: str
    agent_id: uuid.UUID
    daily_spend_limit_usd: float | None
    monthly_spend_limit_usd: float | None
    daily_call_limit: int | None
    monthly_call_limit: int | None
    explicit: bool
    note: str


class OrgQuotaIn(BaseModel):
    max_agents: int | None = None
    monthly_spend_limit_usd: float | None = None


class OrgQuotaOut(BaseModel):
    max_agents: int | None
    monthly_spend_limit_usd: float | None
    explicit: bool
    note: str


class StartCheckOut(BaseModel):
    allowed: bool
    reason: str


def _budget_service(
    db: DbSession, user: CurrentUser, request: Request,
) -> WorkforceBudgetService:
    from app.workforce.budget_service import WorkforceBudgetService

    return WorkforceBudgetService(
        db, platform_access.principal_for(user), audit_context(request, user))


def _budget_out(data: dict) -> BudgetOut:
    return BudgetOut(
        agent_kind=data["agent_kind"], agent_id=data["agent_id"],
        daily_spend_limit_usd=(
            float(data["daily_spend_limit_usd"])
            if data["daily_spend_limit_usd"] is not None else None),
        monthly_spend_limit_usd=(
            float(data["monthly_spend_limit_usd"])
            if data["monthly_spend_limit_usd"] is not None else None),
        daily_call_limit=data["daily_call_limit"],
        monthly_call_limit=data["monthly_call_limit"],
        explicit=data["explicit"], note=data["note"])


@router.get("/agents/{agent_kind}/{agent_id}/budget", response_model=BudgetOut)
async def get_agent_budget(
    agent_kind: str, agent_id: uuid.UUID,
    request: Request, db: DbSession, user: CurrentUser,
) -> BudgetOut:
    data = await _budget_service(db, user, request).get_budget(agent_kind, agent_id)
    return _budget_out(data)


@router.put("/agents/{agent_kind}/{agent_id}/budget", response_model=BudgetOut)
async def set_agent_budget(
    agent_kind: str, agent_id: uuid.UUID, payload: BudgetIn,
    request: Request, db: DbSession, user: CurrentUser,
) -> BudgetOut:
    from decimal import Decimal

    from app.workforce.budget import BudgetLimits

    limits = BudgetLimits(
        daily_spend_limit_usd=(
            Decimal(str(payload.daily_spend_limit_usd))
            if payload.daily_spend_limit_usd is not None else None),
        monthly_spend_limit_usd=(
            Decimal(str(payload.monthly_spend_limit_usd))
            if payload.monthly_spend_limit_usd is not None else None),
        daily_call_limit=payload.daily_call_limit,
        monthly_call_limit=payload.monthly_call_limit)
    data = await _budget_service(db, user, request).set_budget(
        agent_kind, agent_id, limits=limits)
    return _budget_out(data)


@router.delete("/agents/{agent_kind}/{agent_id}/budget", status_code=204)
async def clear_agent_budget(
    agent_kind: str, agent_id: uuid.UUID,
    request: Request, db: DbSession, user: CurrentUser,
) -> None:
    await _budget_service(db, user, request).clear_budget(agent_kind, agent_id)


@router.get("/agents/{agent_kind}/{agent_id}/budget/check", response_model=StartCheckOut)
async def check_agent_start(
    agent_kind: str, agent_id: uuid.UUID,
    request: Request, db: DbSession, user: CurrentUser,
) -> StartCheckOut:
    decision = await _budget_service(db, user, request).check_start(agent_kind, agent_id)
    return StartCheckOut(allowed=decision.allowed, reason=decision.reason)


@router.get("/org-quota", response_model=OrgQuotaOut)
async def get_org_quota(
    request: Request, db: DbSession, user: CurrentUser,
) -> OrgQuotaOut:
    data = await _budget_service(db, user, request).get_org_quota()
    return OrgQuotaOut(
        max_agents=data["max_agents"],
        monthly_spend_limit_usd=(
            float(data["monthly_spend_limit_usd"])
            if data["monthly_spend_limit_usd"] is not None else None),
        explicit=data["explicit"], note=data["note"])


@router.put("/org-quota", response_model=OrgQuotaOut)
async def set_org_quota(
    payload: OrgQuotaIn,
    request: Request, db: DbSession, user: CurrentUser,
) -> OrgQuotaOut:
    from decimal import Decimal

    data = await _budget_service(db, user, request).set_org_quota(
        max_agents=payload.max_agents,
        monthly_spend_limit_usd=(
            Decimal(str(payload.monthly_spend_limit_usd))
            if payload.monthly_spend_limit_usd is not None else None))
    return OrgQuotaOut(
        max_agents=data["max_agents"],
        monthly_spend_limit_usd=(
            float(data["monthly_spend_limit_usd"])
            if data["monthly_spend_limit_usd"] is not None else None),
        explicit=data["explicit"], note=data["note"])


# -- reporting (slice 6, read-only) ---------------------------------------------


class AgentReportOut(BaseModel):
    agent_kind: str
    agent_id: uuid.UUID
    display_name: str
    owner_user_id: uuid.UUID | None
    effective_status: str
    can_start: bool
    start_reason: str
    runs: int
    runs_by_status: dict[str, int]
    completed: int
    failed: int
    success_rate: float | None
    avg_duration_ms: float | None
    total_cost_usd: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_llm_calls: int
    cost_per_task_usd: float | None
    current_skills: list[str]
    delegations_as_parent: int
    delegations_as_child: int
    delegations_denied: int
    monthly_spend_limit_usd: float | None
    usage_note: str


class OrgReportOut(BaseModel):
    days: int
    window_start: datetime
    agents: list[AgentReportOut]
    total_agents: int
    agents_by_status: dict[str, int]
    total_runs: int
    total_completed: int
    total_failed: int
    success_rate: float | None
    total_cost_usd: float
    org_monthly_spend_limit_usd: float | None
    org_max_agents: int | None


class SpendReportOut(BaseModel):
    days: int
    window_start: datetime
    total_cost_usd: float
    total_runs: int
    cost_per_task_usd: float | None
    by_agent: list[dict]


def _reporting_service(
    db: DbSession, user: CurrentUser, request: Request,
):
    from app.workforce.reporting import WorkforceReportingService

    return WorkforceReportingService(
        db, platform_access.principal_for(user), audit_context(request, user))


def _agent_report_out(report) -> AgentReportOut:
    return AgentReportOut(
        agent_kind=report.kind, agent_id=report.agent_id,
        display_name=report.display_name, owner_user_id=report.owner_id,
        effective_status=report.effective_status, can_start=report.can_start,
        start_reason=report.start_reason, runs=report.runs,
        runs_by_status=dict(report.runs_by_status), completed=report.completed,
        failed=report.failed, success_rate=report.success_rate,
        avg_duration_ms=report.avg_duration_ms,
        total_cost_usd=float(report.total_cost_usd),
        total_prompt_tokens=report.total_prompt_tokens,
        total_completion_tokens=report.total_completion_tokens,
        total_llm_calls=report.total_llm_calls,
        cost_per_task_usd=(
            float(report.cost_per_task_usd)
            if report.cost_per_task_usd is not None else None),
        current_skills=list(report.current_skills),
        delegations_as_parent=report.delegations_as_parent,
        delegations_as_child=report.delegations_as_child,
        delegations_denied=report.delegations_denied,
        monthly_spend_limit_usd=(
            float(report.monthly_spend_limit_usd)
            if report.monthly_spend_limit_usd is not None else None),
        usage_note=report.usage_note)


@router.get("/report/org", response_model=OrgReportOut)
async def org_report(
    request: Request, db: DbSession, user: CurrentUser, days: int = 30,
) -> OrgReportOut:
    report = await _reporting_service(db, user, request).org_report(days=days)
    return OrgReportOut(
        days=report.days, window_start=report.window_start,
        agents=[_agent_report_out(item) for item in report.agents],
        total_agents=report.total_agents,
        agents_by_status=dict(report.agents_by_status),
        total_runs=report.total_runs, total_completed=report.total_completed,
        total_failed=report.total_failed, success_rate=report.success_rate,
        total_cost_usd=float(report.total_cost_usd),
        org_monthly_spend_limit_usd=(
            float(report.org_monthly_spend_limit_usd)
            if report.org_monthly_spend_limit_usd is not None else None),
        org_max_agents=report.org_max_agents)


@router.get("/report/spend", response_model=SpendReportOut)
async def spend_report(
    request: Request, db: DbSession, user: CurrentUser, days: int = 30,
) -> SpendReportOut:
    report = await _reporting_service(db, user, request).spend_report(days=days)
    return SpendReportOut(
        days=report.days, window_start=report.window_start,
        total_cost_usd=float(report.total_cost_usd),
        total_runs=report.total_runs,
        cost_per_task_usd=(
            float(report.cost_per_task_usd)
            if report.cost_per_task_usd is not None else None),
        by_agent=[dict(item) for item in report.by_agent])


@router.get("/report/agents/{agent_kind}/{agent_id}", response_model=AgentReportOut)
async def agent_report(
    agent_kind: str, agent_id: uuid.UUID,
    request: Request, db: DbSession, user: CurrentUser, days: int = 30,
) -> AgentReportOut:
    report = await _reporting_service(db, user, request).agent_report(
        agent_kind, agent_id, days=days)
    return _agent_report_out(report)


# -- skills (slice 2 service; routes land with the admin UI, slice 7) ----------


class SkillOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    category: str | None


class SkillIn(BaseModel):
    name: str
    description: str = ""
    category: str | None = None


class AgentSkillOut(BaseModel):
    skill_id: uuid.UUID
    name: str
    category: str | None
    status: str
    verified_by_user_id: uuid.UUID | None
    verified_at: datetime | None
    expires_at: datetime | None


class SkillAttachIn(BaseModel):
    skill_id: uuid.UUID


class SkillVerifyIn(BaseModel):
    expires_at: datetime | None = None


def _skill_service(
    db: DbSession, user: CurrentUser, request: Request,
):
    from app.workforce.skill_service import SkillRegistryService

    return SkillRegistryService(
        db, platform_access.principal_for(user), audit_context(request, user))


@router.get("/skills", response_model=list[SkillOut])
async def list_skills(
    request: Request, db: DbSession, user: CurrentUser,
) -> list[SkillOut]:
    rows = await _skill_service(db, user, request).list_skills()
    return [SkillOut(id=row.id, name=row.name, description=row.description,
                     category=row.category) for row in rows]


@router.post("/skills", response_model=SkillOut)
async def create_skill(
    payload: SkillIn,
    request: Request, db: DbSession, user: CurrentUser,
) -> SkillOut:
    row = await _skill_service(db, user, request).create_skill(
        payload.name, description=payload.description, category=payload.category)
    return SkillOut(id=row.id, name=row.name, description=row.description,
                    category=row.category)


@router.get("/agents/{agent_kind}/{agent_id}/skills", response_model=list[AgentSkillOut])
async def agent_skills(
    agent_kind: str, agent_id: uuid.UUID,
    request: Request, db: DbSession, user: CurrentUser,
) -> list[AgentSkillOut]:
    rows = await _skill_service(db, user, request).agent_skills(agent_kind, agent_id)
    return [AgentSkillOut(**row) for row in rows]


@router.post("/agents/{agent_kind}/{agent_id}/skills", response_model=AgentSkillOut)
async def attach_skill(
    agent_kind: str, agent_id: uuid.UUID, payload: SkillAttachIn,
    request: Request, db: DbSession, user: CurrentUser,
) -> AgentSkillOut:
    svc = _skill_service(db, user, request)
    await svc.add_skill(agent_kind, agent_id, payload.skill_id)
    rows = await svc.agent_skills(agent_kind, agent_id)
    return AgentSkillOut(**next(row for row in rows if row["skill_id"] == payload.skill_id))


@router.post("/agents/{agent_kind}/{agent_id}/skills/{skill_id}/verify",
             response_model=AgentSkillOut)
async def verify_skill(
    agent_kind: str, agent_id: uuid.UUID, skill_id: uuid.UUID, payload: SkillVerifyIn,
    request: Request, db: DbSession, user: CurrentUser,
) -> AgentSkillOut:
    svc = _skill_service(db, user, request)
    await svc.verify_skill(agent_kind, agent_id, skill_id, expires_at=payload.expires_at)
    rows = await svc.agent_skills(agent_kind, agent_id)
    return AgentSkillOut(**next(row for row in rows if row["skill_id"] == skill_id))


@router.post("/agents/{agent_kind}/{agent_id}/skills/{skill_id}/revoke",
             response_model=AgentSkillOut)
async def revoke_skill(
    agent_kind: str, agent_id: uuid.UUID, skill_id: uuid.UUID,
    request: Request, db: DbSession, user: CurrentUser,
) -> AgentSkillOut:
    svc = _skill_service(db, user, request)
    await svc.revoke_skill(agent_kind, agent_id, skill_id)
    rows = await svc.agent_skills(agent_kind, agent_id)
    return AgentSkillOut(**next(row for row in rows if row["skill_id"] == skill_id))


@router.delete("/agents/{agent_kind}/{agent_id}/skills/{skill_id}", status_code=204)
async def remove_skill(
    agent_kind: str, agent_id: uuid.UUID, skill_id: uuid.UUID,
    request: Request, db: DbSession, user: CurrentUser,
) -> None:
    await _skill_service(db, user, request).remove_skill(agent_kind, agent_id, skill_id)
