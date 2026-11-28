"""Step J (AI Workforce OS), slice 2: the skill-registry service.

The registry answers one question — "which skills does this agent have,
and which of them are verified and current?" — so assignment (slice 1)
can match on facts. It grants nothing:

* A skill row is a fact, not a permission. Recording or verifying a skill
  never gives the agent a platform permission, a data scope, or a tool.
  Enforcement stays with the AK controls and ``app.platform``.
* Verification follows plan §7.3: the agent's accountable owner, an
  administrator, or a holder of ``workforce.manage`` on the organisation
  scope. The catalog itself (creating a skill) needs ``workforce.manage``.
* Every gate goes through ``app.platform`` first: a principal who is not
  a member of the organisation scope gets 404 (``NotFoundError``), a
  member without the needed standing gets 403 (``PermissionDeniedError``).
* Agents are resolved from their island tables at write time and the call
  fails closed (404) when the agent does not exist: Studio agents by
  ``owner_id``, governance agents by ``owner_user_id``, legacy profiles by
  the department that owns the enabled version (a department manager is
  the accountable owner there).

Why no ``execute_once`` here: verification is an idempotent, reversible
database state change with no external dispatch — re-verifying the same
row only refreshes its timestamps, and the unique constraint on
(agent kind, agent id, skill) plus a row lock plus a read-back after the
write gives the same exactly-once guarantee at the database. ``execute_once``
is for irreversible side effects with an external dispatch (payments,
sends); its duty check cannot express the plan's owner-verification
rule, because a duty is a platform permission and ownership is not one.
Every change is audited (``workforce.skill_*``) in the same transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.permission_catalog import PERMISSION_WORKFORCE_MANAGE, PERMISSION_WORKFORCE_READ
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import AgentProfile, DepartmentManager, User
from app.db.workforce_skill_models import WorkforceAgentSkill, WorkforceSkill
from app.governance.models import AgentIdentity
from app.platform import access as platform_access
from app.platform.access import Principal
from app.services import audit
from app.services.audit import AuditContext
from app.workforce.skills import (
    default_expiry,
    is_skill_current,
    may_verify_skill,
    skill_status,
    validate_agent_kind,
    validate_skill_name,
)

__all__ = ["ResolvedAgent", "SkillRegistryService"]


@dataclass(frozen=True)
class ResolvedAgent:
    """An island agent as the registry sees it, with its accountable owner."""

    kind: str
    agent_id: uuid.UUID
    display_name: str
    owner_id: uuid.UUID | None = None
    manager_group_id: uuid.UUID | None = None


async def resolve_agent(db: AsyncSession, kind: str, agent_id: uuid.UUID) -> ResolvedAgent | None:
    """Find the island agent and its accountable owner, or None.

    ``profile`` agents are identified by their stable ``agent_key`` (the
    enabled version); their accountable owner is the department that owns
    them, represented here by its group id for the manager check.
    """
    kind, error = validate_agent_kind(kind)
    if error is not None:
        raise InvalidInputError(error)
    if kind == "studio":
        row = await db.get(AgentStudioAgent, agent_id)
        if row is None:
            return None
        return ResolvedAgent(kind="studio", agent_id=agent_id, display_name=row.name,
                             owner_id=row.owner_id)
    if kind == "governance":
        row = await db.get(AgentIdentity, agent_id)
        if row is None:
            return None
        return ResolvedAgent(kind="governance", agent_id=agent_id, display_name=row.name,
                             owner_id=row.owner_user_id)
    row = await db.scalar(select(AgentProfile).where(
        AgentProfile.agent_key == agent_id, AgentProfile.enabled.is_(True)))
    if row is None:
        return None
    return ResolvedAgent(kind="profile", agent_id=agent_id, display_name=row.name,
                         manager_group_id=row.department_group_id)


class SkillRegistryService:
    """Org-wide skill catalog + per-agent verified skills."""

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
        raise PermissionDeniedError("You do not have permission to view workforce skills.")

    async def _authorize_write(self, agent: ResolvedAgent) -> None:
        """Plan §7.3: ``workforce.manage`` holder, admin, or the agent's owner.

        Non-members of the organisation scope get 404 even when they are an
        owner on paper; members without any of the three standings get 403.
        """
        scope_id = await self._org_scope_id()
        decision = await platform_access.authorize(
            self.db, self.actor, PERMISSION_WORKFORCE_MANAGE, scope_id)
        if decision.reason in {"scope_missing", "not_a_member"}:
            raise NotFoundError("Not found.")
        if decision.allowed:
            return
        user = await self.db.get(User, self.actor.user_id)
        if user is not None and user.is_active:
            is_owner = agent.owner_id is not None and user.id == agent.owner_id
            is_manager = False
            if agent.manager_group_id is not None:
                is_manager = await self.db.scalar(select(DepartmentManager.manager_user_id).where(
                    DepartmentManager.group_id == agent.manager_group_id,
                    DepartmentManager.manager_user_id == user.id)) is not None
            if may_verify_skill(is_admin=user.role == "admin", is_owner=is_owner or is_manager,
                                has_workforce_manage=False):
                return
        raise PermissionDeniedError(
            "Only the agent's owner, an administrator, or a workforce.manage holder "
            "can change this agent's skills.")

    async def _authorize_agent_read(self, agent: ResolvedAgent) -> None:
        """Per-agent reads: workforce reader, admin, or the agent's owner."""
        scope_id = await self._org_scope_id()
        for permission in (PERMISSION_WORKFORCE_READ, PERMISSION_WORKFORCE_MANAGE):
            decision = await platform_access.authorize(
                self.db, self.actor, permission, scope_id)
            if decision.allowed:
                return
            if decision.reason in {"scope_missing", "not_a_member"}:
                raise NotFoundError("Not found.")
        user = await self.db.get(User, self.actor.user_id)
        if user is not None and user.is_active:
            is_owner = agent.owner_id is not None and user.id == agent.owner_id
            is_manager = False
            if agent.manager_group_id is not None:
                is_manager = await self.db.scalar(select(DepartmentManager.manager_user_id).where(
                    DepartmentManager.group_id == agent.manager_group_id,
                    DepartmentManager.manager_user_id == user.id)) is not None
            if user.role == "admin" or is_owner or is_manager:
                return
        raise PermissionDeniedError("You do not have permission to view this agent's skills.")

    # -- catalog ---------------------------------------------------------------

    async def create_skill(
        self, name: str, *, description: str = "", category: str | None = None,
    ) -> WorkforceSkill:
        """Add a skill to the org-wide catalog (``workforce.manage`` only)."""
        await self._require_manage()
        clean, error = validate_skill_name(name)
        if error is not None:
            raise InvalidInputError(error)
        if description and len(description) > 500:
            raise InvalidInputError("Skill description must be at most 500 characters.")
        if category is not None and (not category.strip() or len(category.strip()) > 60):
            raise InvalidInputError("Skill category must be 1 to 60 characters.")
        existing = await self.db.scalar(
            select(WorkforceSkill).where(WorkforceSkill.name == clean))
        if existing is not None:
            raise ConflictError(f"A skill named {clean!r} already exists.")
        row = WorkforceSkill(name=clean, description=description or "",
                             category=category.strip() if category else None,
                             created_by_user_id=self.actor.user_id)
        self.db.add(row)
        audit.record(self.db, self.ctx, "workforce.skill_created", target=str(row.id),
                     details={"name": clean})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def list_skills(self) -> list[WorkforceSkill]:
        """The whole catalog (``workforce.read`` or ``workforce.manage``)."""
        await self._require_read()
        rows = await self.db.scalars(select(WorkforceSkill).order_by(WorkforceSkill.name))
        return list(rows)

    # -- per-agent skills ------------------------------------------------------

    async def _agent_or_404(self, agent_kind: str, agent_id: uuid.UUID) -> ResolvedAgent:
        agent = await resolve_agent(self.db, agent_kind, agent_id)
        if agent is None:
            raise NotFoundError("Workforce agent not found.")
        return agent

    async def _skill_or_404(self, skill_id: uuid.UUID) -> WorkforceSkill:
        row = await self.db.get(WorkforceSkill, skill_id)
        if row is None:
            raise NotFoundError("Skill not found.")
        return row

    async def add_skill(
        self, agent_kind: str, agent_id: uuid.UUID, skill_id: uuid.UUID,
    ) -> WorkforceAgentSkill:
        """Attach a skill to an agent, unverified. Idempotent: attaching the
        same skill twice returns the same row."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        skill = await self._skill_or_404(skill_id)
        await self._authorize_write(agent)
        await self.db.execute(pg_insert(WorkforceAgentSkill).values(
            agent_kind=agent.kind, agent_id=agent.agent_id, skill_id=skill.id,
            created_by_user_id=self.actor.user_id,
        ).on_conflict_do_nothing(index_elements=["agent_kind", "agent_id", "skill_id"]))
        row = await self.db.scalar(select(WorkforceAgentSkill).where(
            WorkforceAgentSkill.agent_kind == agent.kind,
            WorkforceAgentSkill.agent_id == agent.agent_id,
            WorkforceAgentSkill.skill_id == skill.id))
        audit.record(self.db, self.ctx, "workforce.skill_added", target=str(agent.agent_id),
                     details={"agent_kind": agent.kind, "skill_id": str(skill.id),
                              "skill_name": skill.name})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def _locked_row(
        self, agent: ResolvedAgent, skill_id: uuid.UUID,
    ) -> WorkforceAgentSkill:
        row = await self.db.scalar(select(WorkforceAgentSkill).where(
            WorkforceAgentSkill.agent_kind == agent.kind,
            WorkforceAgentSkill.agent_id == agent.agent_id,
            WorkforceAgentSkill.skill_id == skill_id).with_for_update())
        if row is None:
            raise NotFoundError("Skill is not attached to this agent.")
        return row

    async def verify_skill(
        self, agent_kind: str, agent_id: uuid.UUID, skill_id: uuid.UUID,
        *, expires_at: datetime | None = None,
    ) -> WorkforceAgentSkill:
        """Verify an attached skill (plan §7.3 gate). Re-verifying refreshes
        the verification; an explicit expiry must be in the future."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        skill = await self._skill_or_404(skill_id)
        await self._authorize_write(agent)
        now = datetime.now(UTC)
        if expires_at is not None and expires_at <= now:
            raise InvalidInputError("Skill expiry must be in the future.")
        row = await self._locked_row(agent, skill_id)
        row.verified_by_user_id = self.actor.user_id
        row.verified_at = now
        row.expires_at = expires_at if expires_at is not None else default_expiry(now)
        await self.db.flush()
        # Read-back: the row must read as verified with the values written.
        await self.db.refresh(row)
        if row.verified_at is None or row.verified_by_user_id != self.actor.user_id:
            await self.db.rollback()
            raise InvalidInputError("Skill verification did not persist.")
        audit.record(self.db, self.ctx, "workforce.skill_verified", target=str(agent.agent_id),
                     details={"agent_kind": agent.kind, "skill_id": str(skill.id),
                              "skill_name": skill.name,
                              "expires_at": row.expires_at.isoformat() if row.expires_at else None})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def revoke_skill(
        self, agent_kind: str, agent_id: uuid.UUID, skill_id: uuid.UUID,
    ) -> WorkforceAgentSkill:
        """Un-verify an attached skill (the row stays, so the history is
        auditable). Same gate as verification."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        skill = await self._skill_or_404(skill_id)
        await self._authorize_write(agent)
        row = await self._locked_row(agent, skill_id)
        row.verified_by_user_id = None
        row.verified_at = None
        row.expires_at = None
        await self.db.flush()
        await self.db.refresh(row)
        if row.verified_at is not None:
            await self.db.rollback()
            raise InvalidInputError("Skill revocation did not persist.")
        audit.record(self.db, self.ctx, "workforce.skill_revoked", target=str(agent.agent_id),
                     details={"agent_kind": agent.kind, "skill_id": str(skill.id),
                              "skill_name": skill.name})
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def remove_skill(
        self, agent_kind: str, agent_id: uuid.UUID, skill_id: uuid.UUID,
    ) -> None:
        """Detach a skill from an agent entirely. Same gate as verification."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        skill = await self._skill_or_404(skill_id)
        await self._authorize_write(agent)
        row = await self._locked_row(agent, skill_id)
        await self.db.delete(row)
        audit.record(self.db, self.ctx, "workforce.skill_removed", target=str(agent.agent_id),
                     details={"agent_kind": agent.kind, "skill_id": str(skill.id),
                              "skill_name": skill.name})
        await self.db.commit()

    async def agent_skills(
        self, agent_kind: str, agent_id: uuid.UUID,
    ) -> list[dict]:
        """Every skill attached to an agent, with its derived status."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        await self._authorize_agent_read(agent)
        rows = await self.db.execute(
            select(WorkforceAgentSkill, WorkforceSkill)
            .join(WorkforceSkill, WorkforceSkill.id == WorkforceAgentSkill.skill_id)
            .where(WorkforceAgentSkill.agent_kind == agent.kind,
                   WorkforceAgentSkill.agent_id == agent.agent_id)
            .order_by(WorkforceSkill.name))
        now = datetime.now(UTC)
        out: list[dict] = []
        for link, skill in rows.all():
            out.append({
                "skill_id": skill.id,
                "name": skill.name,
                "category": skill.category,
                "status": skill_status(link.verified_at, link.expires_at, now),
                "verified_by_user_id": link.verified_by_user_id,
                "verified_at": link.verified_at,
                "expires_at": link.expires_at,
            })
        return out

    async def current_skills(
        self, agent_kind: str, agent_id: uuid.UUID,
    ) -> list[str]:
        """Verified, unexpired skill names — the only set assignment (slice 1)
        may match on. Returns the normalized names, sorted."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        await self._authorize_agent_read(agent)
        rows = await self.db.execute(
            select(WorkforceAgentSkill, WorkforceSkill)
            .join(WorkforceSkill, WorkforceSkill.id == WorkforceAgentSkill.skill_id)
            .where(WorkforceAgentSkill.agent_kind == agent.kind,
                   WorkforceAgentSkill.agent_id == agent.agent_id)
            .order_by(WorkforceSkill.name))
        now = datetime.now(UTC)
        names = [skill.name for link, skill in rows.all()
                 if is_skill_current(link.verified_at, link.expires_at, now)]
        return sorted(names)
