"""Step J (AI Workforce OS), slice 4: the delegation policy service.

The policy engine answers one question — "may agent A hand this work to
agent B?" — with an audit trail. It grants nothing by itself:

* Every gate goes through ``app.platform`` first: a principal who is not
  a member of the organisation scope gets 404 (``NotFoundError``), a
  member without the needed standing gets 403 (``PermissionDeniedError``).
  Policies are written by a ``workforce.manage`` holder only; reads need
  ``workforce.read`` or better.
* Both agents are resolved from their island tables at decision time
  and the call fails closed (404) when either does not exist.
* The child's effective status comes from the registry (slice 3): a
  paused / retired / disabled agent, or one whose owner was revoked,
  never takes delegated work.
* The child's own tool list is the ceiling: the delegation may ask for
  tools only the child itself holds. The delegated task's free text is
  never read — it cannot raise the child's permissions, change a payee,
  or change what an approval covers.
* Every decision — allowed and denied — is written to the append-only
  audit table with the parent run, the child execution, the policy
  version and the reason, so the trail exists even when no policy was
  in force (then the version is 0 and the reason is ``no_active_policy``:
  the parent's own allow-list was the only control).

Why no ``execute_once`` here: policy writes and audit rows are
idempotent, reversible database state changes with no external dispatch
— creating a policy version only deactivates the previous one, and an
audit row is a record, not a dispatch. The row lock plus read-back
after the write gives the same exactly-once guarantee at the database.

The child run on the unified engine (``origin='workforce'``) is a
merge-time integration, not built here: a ``wf_runs`` row would be
claimed by the engine worker and executed a second time unless the
engine gains a delegation handler, and the ``wf_definitions`` origin
check constraint does not admit ``'workforce'`` yet. On this branch the
child executes inline in the parent's Agent Studio graph, so the audit
records ``child_run_kind='inline'`` and a fresh delegation-execution id
as ``child_run_id``. The audit shape does not change when the engine
takes over.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.permission_catalog import (
    PERMISSION_WORKFORCE_MANAGE,
    PERMISSION_WORKFORCE_READ,
)
from app.db.agent_studio_models import AgentStudioAgent
from app.db.workforce_delegation_models import (
    CHILD_RUN_KIND_INLINE,
    WorkforceDelegationAudit,
    WorkforceDelegationPolicy,
)
from app.governance.models import AgentIdentity
from app.platform import access as platform_access
from app.platform.access import Principal
from app.services import audit
from app.services.audit import AuditContext
from app.workforce.delegation import (
    DelegationDecision,
    DelegationPolicy,
    DelegationRequest,
    evaluate_delegation,
    policy_from_dict,
    validate_policy_rules,
)
from app.workforce.registry_service import WorkforceRegistryService
from app.workforce.skill_service import ResolvedAgent, resolve_agent
from app.workforce.skills import validate_agent_kind

__all__ = ["DelegationAuditView", "WorkforceDelegationService"]


@dataclass(frozen=True)
class DelegationAuditView:
    """One delegation decision, as the API returns it."""

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


class WorkforceDelegationService:
    """Versioned delegation policies + the decision audit trail."""

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
        raise PermissionDeniedError("You do not have permission to view delegation policies.")

    # -- policies ---------------------------------------------------------------

    async def _active_row(self) -> WorkforceDelegationPolicy | None:
        return await self.db.scalar(select(WorkforceDelegationPolicy).where(
            WorkforceDelegationPolicy.is_active.is_(True)).order_by(
                WorkforceDelegationPolicy.version.desc()))

    async def _row_to_policy(self, row: WorkforceDelegationPolicy) -> DelegationPolicy | None:
        return policy_from_dict({
            "version": row.version,
            "max_depth": row.max_depth,
            "is_active": row.is_active,
            "rules": row.rules if isinstance(row.rules, list) else [],
        })

    async def active_policy(self) -> DelegationPolicy | None:
        """The active policy, if one exists (``workforce.read`` or better)."""
        await self._require_read()
        row = await self._active_row()
        if row is None:
            return None
        return await self._row_to_policy(row)

    async def list_policies(self) -> list[DelegationPolicy]:
        """Every policy version, newest first (``workforce.read`` or better)."""
        await self._require_read()
        rows = await self.db.scalars(select(WorkforceDelegationPolicy).order_by(
            WorkforceDelegationPolicy.version.desc()))
        policies: list[DelegationPolicy] = []
        for row in rows.all():
            policy = await self._row_to_policy(row)
            if policy is not None:
                policies.append(policy)
        return policies

    async def create_policy(
        self, rules: list[dict], *, max_depth: int = 2,
    ) -> DelegationPolicy:
        """Publish a new policy version (``workforce.manage`` only).

        The new version becomes the only active policy; the previous
        active policy is deactivated in the same transaction. Versions
        are never edited in place.
        """
        await self._require_manage()
        problems = validate_policy_rules(rules)
        if problems:
            raise InvalidInputError("; ".join(problems))
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) \
                or not 1 <= max_depth <= 5:
            raise InvalidInputError("Delegation max depth must be an integer between 1 and 5.")
        version = (await self.db.scalar(
            select(func.max(WorkforceDelegationPolicy.version)))) or 0
        version += 1
        # Deactivate the previous active policy in the same transaction;
        # the bulk UPDATE locks the rows it touches.
        await self.db.execute(
            WorkforceDelegationPolicy.__table__.update().where(
                WorkforceDelegationPolicy.is_active.is_(True)).values(is_active=False))
        row = WorkforceDelegationPolicy(
            version=version,
            rules=[dict(rule) for rule in rules],
            max_depth=max_depth,
            is_active=True,
            created_by_user_id=self.actor.user_id)
        self.db.add(row)
        await self.db.flush()
        policy = await self._row_to_policy(row)
        if policy is None:
            await self.db.rollback()
            raise InvalidInputError("Delegation policy did not persist.")
        audit.record(self.db, self.ctx, "workforce.delegation_policy_created",
                     target=str(row.id),
                     details={"version": version, "max_depth": max_depth,
                              "rule_count": len(rules)})
        await self.db.commit()
        return policy

    # -- decisions ---------------------------------------------------------------

    async def _child_tools(self, agent: ResolvedAgent) -> frozenset[str]:
        """The child's own tools — the ceiling for any delegation to it.

        Studio agents carry ``tool_permissions``; governance agents carry
        ``tools``; profile agents carry no tool list on this branch, so
        they can only be delegated tool-free work (recorded by the empty
        set, never guessed).
        """
        if agent.kind == "studio":
            row = await self.db.get(AgentStudioAgent, agent.agent_id)
            tools = (row.tool_permissions or []) if row is not None else []
        elif agent.kind == "governance":
            row = await self.db.get(AgentIdentity, agent.agent_id)
            tools = (row.tools or []) if row is not None else []
        else:
            tools = []
        return frozenset(str(tool).strip() for tool in tools if str(tool).strip())

    async def _build_request(
        self, parent: ResolvedAgent, child: ResolvedAgent, *,
        action: str, scope_id: str, sensitivity: str, depth: int,
        requested_tools: frozenset[str],
        originator_user_id: uuid.UUID | None,
        approver_user_id: uuid.UUID | None,
    ) -> tuple[DelegationRequest, DelegationPolicy | None]:
        registry = WorkforceRegistryService(self.db, self.actor, self.ctx)
        child_status = await registry.effective_status(child.kind, child.agent_id)
        row = await self._active_row()
        policy = await self._row_to_policy(row) if row is not None else None
        request = DelegationRequest(
            parent_kind=parent.kind, parent_agent_id=parent.agent_id,
            child_kind=child.kind, child_agent_id=child.agent_id,
            action=action, scope_id=scope_id, sensitivity=sensitivity,
            depth=depth, requested_tools=requested_tools,
            child_tools=await self._child_tools(child),
            child_status=child_status,
            originator_user_id=originator_user_id,
            approver_user_id=approver_user_id)
        return request, policy

    async def _decide(
        self, parent_kind: str, parent_id: uuid.UUID, child_kind: str, child_id: uuid.UUID, *,
        action: str = "", scope_id: str = "", sensitivity: str = "standard",
        depth: int = 1, requested_tools: frozenset[str] | None = None,
        originator_user_id: uuid.UUID | None = None,
        approver_user_id: uuid.UUID | None = None,
    ) -> DelegationDecision:
        """The decision itself, without any read gate.

        The API dry-run gates through ``evaluate``; the Studio path calls
        ``record_delegation``, which uses this directly — the run owner is
        already authorized to run their own agent, and the policy check
        is an internal enforcement decision, not a user-facing read.
        """
        parent_kind, error = validate_agent_kind(parent_kind)
        if error is not None:
            raise InvalidInputError(error)
        child_kind, error = validate_agent_kind(child_kind)
        if error is not None:
            raise InvalidInputError(error)
        assert parent_kind is not None and child_kind is not None
        parent = await resolve_agent(self.db, parent_kind, parent_id)
        if parent is None:
            raise NotFoundError("Delegating agent not found.")
        child = await resolve_agent(self.db, child_kind, child_id)
        if child is None:
            raise NotFoundError("Receiving agent not found.")
        request, policy = await self._build_request(
            parent, child, action=action, scope_id=scope_id, sensitivity=sensitivity,
            depth=depth, requested_tools=requested_tools or frozenset(),
            originator_user_id=originator_user_id, approver_user_id=approver_user_id)
        return evaluate_delegation(policy, request)

    async def evaluate(
        self, parent_kind: str, parent_id: uuid.UUID, child_kind: str, child_id: uuid.UUID, *,
        action: str = "", scope_id: str = "", sensitivity: str = "standard",
        depth: int = 1, requested_tools: frozenset[str] | None = None,
        originator_user_id: uuid.UUID | None = None,
        approver_user_id: uuid.UUID | None = None,
    ) -> DelegationDecision:
        """A dry-run decision: no audit row is written.

        The task description is not an input here at all — it is untrusted
        text and can never change a decision.
        """
        await self._require_read()
        return await self._decide(
            parent_kind, parent_id, child_kind, child_id,
            action=action, scope_id=scope_id, sensitivity=sensitivity, depth=depth,
            requested_tools=requested_tools, originator_user_id=originator_user_id,
            approver_user_id=approver_user_id)

    async def record_delegation(
        self, parent_kind: str, parent_id: uuid.UUID, child_kind: str, child_id: uuid.UUID, *,
        action: str = "", scope_id: str = "", sensitivity: str = "standard",
        depth: int = 1, requested_tools: frozenset[str] | None = None,
        originator_user_id: uuid.UUID | None = None,
        approver_user_id: uuid.UUID | None = None,
        parent_run_id: uuid.UUID | None = None,
        commit: bool = True,
    ) -> tuple[DelegationDecision, DelegationAuditView]:
        """Decide a delegation and write the audit row (allowed and denied).

        Used by the Agent Studio delegation path: the decision gates the
        child execution, and the audit row is the trail. With
        ``commit=False`` the audit insert runs in a savepoint and the
        caller owns the commit (the Studio run's own transaction). This
        path is deliberately ungated: the run owner is already
        authorized to run their own agent, and the policy check is an
        internal enforcement decision.
        """
        decision = await self._decide(
            parent_kind, parent_id, child_kind, child_id,
            action=action, scope_id=scope_id, sensitivity=sensitivity, depth=depth,
            requested_tools=requested_tools, originator_user_id=originator_user_id,
            approver_user_id=approver_user_id)
        child_run_id = uuid.uuid4()
        row = WorkforceDelegationAudit(
            parent_kind=parent_kind, parent_agent_id=parent_id,
            child_kind=child_kind, child_agent_id=child_id,
            parent_run_id=parent_run_id, child_run_id=child_run_id,
            child_run_kind=CHILD_RUN_KIND_INLINE,
            policy_version=decision.policy_version,
            decision="allowed" if decision.allowed else "denied",
            reason=decision.reason, action=action.strip().lower(),
            scope_id=scope_id.strip(), depth=depth,
            created_by_user_id=self.actor.user_id)
        if commit:
            self.db.add(row)
            audit.record(self.db, self.ctx, "workforce.delegation_decided",
                         target=str(child_id),
                         details={"parent_agent_id": str(parent_id),
                                  "policy_version": decision.policy_version,
                                  "decision": "allowed" if decision.allowed else "denied",
                                  "reason": decision.reason})
            await self.db.commit()
            await self.db.refresh(row)
        else:
            async with self.db.begin_nested():
                self.db.add(row)
                await self.db.flush()
                await self.db.refresh(row)
        view = DelegationAuditView(
            id=row.id, parent_kind=row.parent_kind, parent_agent_id=row.parent_agent_id,
            child_kind=row.child_kind, child_agent_id=row.child_agent_id,
            parent_run_id=row.parent_run_id, child_run_id=row.child_run_id,
            child_run_kind=row.child_run_kind, policy_version=row.policy_version,
            decision=row.decision, reason=row.reason, action=row.action,
            scope_id=row.scope_id, depth=row.depth)
        return decision, view

    async def list_audits(self, *, limit: int = 100) -> list[DelegationAuditView]:
        """The decision trail, newest first (``workforce.read`` or better)."""
        await self._require_read()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            limit = 100
        limit = min(limit, 500)
        rows = await self.db.scalars(select(WorkforceDelegationAudit).order_by(
            WorkforceDelegationAudit.created_at.desc(),
            WorkforceDelegationAudit.id.desc()).limit(limit))
        return [DelegationAuditView(
            id=row.id, parent_kind=row.parent_kind, parent_agent_id=row.parent_agent_id,
            child_kind=row.child_kind, child_agent_id=row.child_agent_id,
            parent_run_id=row.parent_run_id, child_run_id=row.child_run_id,
            child_run_kind=row.child_run_kind, policy_version=row.policy_version,
            decision=row.decision, reason=row.reason, action=row.action,
            scope_id=row.scope_id, depth=row.depth) for row in rows.all()]
