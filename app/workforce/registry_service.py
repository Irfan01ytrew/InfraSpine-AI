"""Step J (AI Workforce OS), slice 3: the org registry + lifecycle service.

The registry answers one question — "every agent in the org, who owns
it, what state it is in, and what it can still do?" — in a single read
view built from the three agent islands (Studio / governance / legacy
profiles) plus the org-level lifecycle in ``workforce_registry_agents``.

It grants nothing:

* The view is read-only and follows AK's ``unify.py`` pattern (read, not
  forked): a real record wins, anything an island does not express is
  left empty (deny by default) and listed in the view's ``notes`` — never
  guessed. AK's ``app.agent_controls`` code is not present on this
  branch, so the AK control-record join is a merge-time integration
  (plan §8); until then every view notes that its effective status
  reflects island + registry + owner only.
* Every gate goes through ``app.platform`` first: a principal who is not
  a member of the organisation scope gets 404 (``NotFoundError``), a
  member without the needed standing gets 403 (``PermissionDeniedError``).
* Lifecycle writes are ``agents.manage`` / ``workforce.manage`` on the
  organisation scope, an administrator, or — for pause/resume only —
  the agent's accountable owner (plan §7.7). Retiring an agent is
  administrator or manage-holder only.
* Agents are resolved from their island tables at write time and the
  call fails closed (404) when the agent does not exist.

The owner-revocation answer (plan §5 slice 3): when an agent's
accountable owner is revoked (``User.is_active`` is false — see
``app.platform.access.revoke_user_access``), the agent must be PAUSED
and its verified skills FROZEN. ``apply_owner_revocation`` is the hook
slice 5 wires into the revocation flow:

* Active agents owned by the revoked user are paused (lifecycle event
  + audit, reason "owner access revoked"); already-paused agents stay
  paused; retired agents stay retired.
* Verified skills are frozen: ``expires_at`` is moved to now, so they
  read as ``expired`` and never match an assignment again. The
  verification history (``verified_at`` / ``verified_by``) is preserved
  and a new accountable owner may re-verify later.
* Ownership is never reassigned — the registry entry keeps its owner,
  and the hook refuses to run for a user whose access has not actually
  been revoked.
* The read view derives the pause even before the hook has run
  (``effective_lifecycle``), so a missed hook cannot leave a revoked
  owner's agent looking active.

Why no ``execute_once`` here: lifecycle changes and skill freezes are
idempotent, reversible database state changes with no external dispatch
— pausing a paused agent is a no-op, freezing an already-expired skill
is a no-op, and the row lock plus read-back after the write gives the
same exactly-once guarantee at the database. ``execute_once`` is for
irreversible side effects with an external dispatch (payments, sends).
Every change is audited (``workforce.lifecycle_*``,
``workforce.skills_frozen``) in the same transaction.

Profile agents are department-owned (their accountable owner is the
department manager group, not a person), so a single user's revocation
does not pause them — recorded as a limitation in the plan (§5 slice 3).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.permission_catalog import (
    PERMISSION_AGENTS_MANAGE,
    PERMISSION_WORKFORCE_MANAGE,
    PERMISSION_WORKFORCE_READ,
)
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import AgentProfile, DepartmentManager, User
from app.db.workforce_registry_models import (
    WorkforceLifecycleEvent,
    WorkforceRegistryAgent,
)
from app.db.workforce_skill_models import WorkforceAgentSkill, WorkforceSkill
from app.governance.models import AgentIdentity
from app.platform import access as platform_access
from app.platform.access import Principal
from app.services import audit
from app.services.audit import AuditContext
from app.workforce.registry import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_PAUSED,
    LIFECYCLE_RETIRED,
    effective_lifecycle,
    island_status,
    may_pause_resume,
    may_retire,
    transition_allowed,
    validate_lifecycle_state,
)
from app.workforce.skill_service import ResolvedAgent, resolve_agent
from app.workforce.skills import is_skill_current

__all__ = [
    "RegistryAgentView",
    "WorkforceRegistryService",
    "apply_owner_revocation_for_platform",
]

AK_NOTE = (
    "AK control record is not joined on this branch; the AK join lands at merge — "
    "effective status here reflects island + registry + owner only."
)
PROFILE_OWNER_NOTE = (
    "Profile agents are department-owned; a single user's revocation does not pause them."
)
OWNER_REVOKED_NOTE = (
    "Accountable owner access is revoked; the agent is paused and its verified skills are frozen."
)
REVOKE_REASON = "owner access revoked"


@dataclass(frozen=True)
class RegistryAgentView:
    """One workforce agent as the org registry sees it, plus what could not be mapped."""

    kind: str
    agent_id: uuid.UUID
    display_name: str
    owner_id: uuid.UUID | None = None
    manager_group_id: uuid.UUID | None = None
    registry_state: str = LIFECYCLE_ACTIVE
    island_status: str = "active"
    effective_status: str = "active"
    owner_revoked: bool = False
    current_skills: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class WorkforceRegistryService:
    """Org-wide agent registry + lifecycle."""

    def __init__(self, db: AsyncSession, actor: Principal, ctx: AuditContext) -> None:
        self.db = db
        self.actor = actor
        self.ctx = ctx

    # -- gates -----------------------------------------------------------------

    async def _org_scope_id(self) -> uuid.UUID:
        return (await platform_access.ensure_org_scope(self.db)).id

    async def _has_manage(self) -> bool:
        """``agents.manage`` or ``workforce.manage`` on the org scope."""
        scope_id = await self._org_scope_id()
        for permission in (PERMISSION_AGENTS_MANAGE, PERMISSION_WORKFORCE_MANAGE):
            decision = await platform_access.authorize(
                self.db, self.actor, permission, scope_id)
            if decision.reason in {"scope_missing", "not_a_member"}:
                raise NotFoundError("Not found.")
            if decision.allowed:
                return True
        return False

    async def _require_read(self) -> None:
        """``workforce.read`` / ``workforce.manage`` / ``agents.manage`` on the org scope."""
        scope_id = await self._org_scope_id()
        for permission in (PERMISSION_WORKFORCE_READ, PERMISSION_WORKFORCE_MANAGE,
                           PERMISSION_AGENTS_MANAGE):
            decision = await platform_access.authorize(
                self.db, self.actor, permission, scope_id)
            if decision.allowed:
                return
            if decision.reason in {"scope_missing", "not_a_member"}:
                raise NotFoundError("Not found.")
        raise PermissionDeniedError("You do not have permission to view the workforce registry.")

    async def _is_admin(self) -> bool:
        user = await self.db.get(User, self.actor.user_id)
        return user is not None and user.is_active and user.role == "admin"

    async def _is_owner_or_manager(self, agent: ResolvedAgent) -> bool:
        user = await self.db.get(User, self.actor.user_id)
        if user is None or not user.is_active:
            return False
        if agent.owner_id is not None and user.id == agent.owner_id:
            return True
        if agent.manager_group_id is not None:
            return await self.db.scalar(select(DepartmentManager.manager_user_id).where(
                DepartmentManager.group_id == agent.manager_group_id,
                DepartmentManager.manager_user_id == user.id)) is not None
        return False

    async def _authorize_agent_read(self, agent: ResolvedAgent) -> None:
        """Registry reads: workforce reader/manage holder, admin, or the agent's owner."""
        scope_id = await self._org_scope_id()
        for permission in (PERMISSION_WORKFORCE_READ, PERMISSION_WORKFORCE_MANAGE,
                           PERMISSION_AGENTS_MANAGE):
            decision = await platform_access.authorize(
                self.db, self.actor, permission, scope_id)
            if decision.allowed:
                return
            if decision.reason in {"scope_missing", "not_a_member"}:
                raise NotFoundError("Not found.")
        if await self._is_admin() or await self._is_owner_or_manager(agent):
            return
        raise PermissionDeniedError("You do not have permission to view this agent.")

    async def _authorize_lifecycle(self, agent: ResolvedAgent, *, retire: bool) -> None:
        """Plan §7.7: manage holder or admin; pause/resume also the agent's owner."""
        if await self._has_manage():
            return
        is_admin = await self._is_admin()
        if retire:
            if may_retire(is_admin=is_admin, has_manage=False):
                return
        else:
            if may_pause_resume(is_admin=is_admin,
                                is_owner=await self._is_owner_or_manager(agent),
                                has_manage=False):
                return
        raise PermissionDeniedError(
            "Only the agent's owner, an administrator, or an agents.manage / "
            "workforce.manage holder can change this agent's lifecycle.")

    async def _authorize_revocation_hook(self) -> None:
        """The revocation hook is an admin / manage-holder operation."""
        if await self._has_manage():
            return
        if await self._is_admin():
            return
        raise PermissionDeniedError(
            "Only an administrator or an agents.manage / workforce.manage holder "
            "can apply an owner revocation.")

    # -- view building -----------------------------------------------------------

    async def _registry_rows(self) -> dict[tuple[str, uuid.UUID], WorkforceRegistryAgent]:
        rows = await self.db.scalars(select(WorkforceRegistryAgent))
        return {(row.agent_kind, row.agent_id): row for row in rows}

    async def _current_skills_map(
        self, agents: list[ResolvedAgent],
    ) -> dict[tuple[str, uuid.UUID], tuple[str, ...]]:
        """Verified, unexpired skill names per agent (the only set assignment may match on)."""
        if not agents:
            return {}
        keys = [(agent.kind, agent.agent_id) for agent in agents]
        rows = await self.db.execute(
            select(WorkforceAgentSkill, WorkforceSkill)
            .join(WorkforceSkill, WorkforceSkill.id == WorkforceAgentSkill.skill_id)
            .where(or_(*[
                (WorkforceAgentSkill.agent_kind == kind)
                & (WorkforceAgentSkill.agent_id == agent_id)
                for kind, agent_id in keys
            ]))
            .order_by(WorkforceSkill.name))
        now = datetime.now(UTC)
        out: dict[tuple[str, uuid.UUID], list[str]] = {}
        for link, skill in rows.all():
            if is_skill_current(link.verified_at, link.expires_at, now):
                out.setdefault((link.agent_kind, link.agent_id), []).append(skill.name)
        return {key: tuple(sorted(names)) for key, names in out.items()}

    async def _owner_revoked_map(self, owner_ids: set[uuid.UUID]) -> dict[uuid.UUID, bool]:
        """True for owners whose account is revoked (``is_active`` false)."""
        if not owner_ids:
            return {}
        rows = await self.db.execute(
            select(User.id, User.is_active).where(User.id.in_(owner_ids)))
        return {user_id: not is_active for user_id, is_active in rows.all()}

    async def _build_views(self, agents: list[ResolvedAgent]) -> list[RegistryAgentView]:
        registry = await self._registry_rows()
        skills = await self._current_skills_map(agents)
        owner_ids = {agent.owner_id for agent in agents if agent.owner_id is not None}
        revoked = await self._owner_revoked_map(owner_ids)
        views: list[RegistryAgentView] = []
        for agent in agents:
            row = registry.get((agent.kind, agent.agent_id))
            registry_state = row.lifecycle_state if row is not None else LIFECYCLE_ACTIVE
            island = await self._island_status(agent)
            owner_revoked = bool(
                agent.owner_id is not None and revoked.get(agent.owner_id, False))
            notes = [AK_NOTE]
            if agent.kind == "profile":
                notes.append(PROFILE_OWNER_NOTE)
            if row is not None and row.owner_user_id != agent.owner_id:
                notes.append(
                    "Accountable owner changed since the last lifecycle write; "
                    "ownership was not reassigned by the registry.")
            if owner_revoked:
                notes.append(OWNER_REVOKED_NOTE)
            views.append(RegistryAgentView(
                kind=agent.kind,
                agent_id=agent.agent_id,
                display_name=agent.display_name,
                owner_id=agent.owner_id,
                manager_group_id=agent.manager_group_id,
                registry_state=registry_state,
                island_status=island,
                effective_status=effective_lifecycle(
                    registry_state=registry_state,
                    island_status_value=island,
                    owner_revoked=owner_revoked),
                owner_revoked=owner_revoked,
                current_skills=skills.get((agent.kind, agent.agent_id), ()),
                notes=tuple(notes),
            ))
        views.sort(key=lambda view: (view.kind, view.display_name.lower()))
        return views

    async def _island_status(self, agent: ResolvedAgent) -> str:
        """The island's own status for this agent (disabled/paused/active)."""
        if agent.kind == "studio":
            row = await self.db.get(AgentStudioAgent, agent.agent_id)
            if row is None:
                return "disabled"
            return island_status("studio", enabled=row.enabled, paused=row.paused)
        if agent.kind == "governance":
            row = await self.db.get(AgentIdentity, agent.agent_id)
            if row is None:
                return "disabled"
            return island_status("governance", enabled=row.enabled)
        row = await self.db.scalar(select(AgentProfile).where(
            AgentProfile.agent_key == agent.agent_id, AgentProfile.enabled.is_(True)))
        return island_status("profile", enabled=row is not None)

    async def _all_agents(self) -> list[ResolvedAgent]:
        """Every island agent, resolved with its accountable owner."""
        agents: list[ResolvedAgent] = []
        for row in await self.db.scalars(
                select(AgentStudioAgent).order_by(AgentStudioAgent.name)):
            agents.append(ResolvedAgent(kind="studio", agent_id=row.id,
                                        display_name=row.name, owner_id=row.owner_id))
        for row in await self.db.scalars(
                select(AgentIdentity).order_by(AgentIdentity.name)):
            agents.append(ResolvedAgent(kind="governance", agent_id=row.id,
                                        display_name=row.name, owner_id=row.owner_user_id))
        for row in await self.db.scalars(select(AgentProfile).where(
                AgentProfile.enabled.is_(True)).order_by(AgentProfile.name)):
            agents.append(ResolvedAgent(kind="profile", agent_id=row.agent_key,
                                        display_name=row.name,
                                        manager_group_id=row.department_group_id))
        return agents

    # -- reads -------------------------------------------------------------------

    async def list_agents(self) -> list[RegistryAgentView]:
        """The whole org registry (``workforce.read`` or better)."""
        await self._require_read()
        return await self._build_views(await self._all_agents())

    async def get_agent(self, agent_kind: str, agent_id: uuid.UUID) -> RegistryAgentView:
        """One registry entry (reader, admin, or the agent's owner)."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        await self._authorize_agent_read(agent)
        return (await self._build_views([agent]))[0]

    async def effective_status(self, agent_kind: str, agent_id: uuid.UUID) -> str:
        """The registry's effective status for one agent, without a read gate.

        Internal enforcement helper (delegation policy, budget gate): the
        caller is already authorized for the operation it is gating — this
        only answers "what state is this agent in" (most restrictive of
        registry / island / owner). Raises ``NotFoundError`` for an
        unknown agent.
        """
        agent = await self._agent_or_404(agent_kind, agent_id)
        return (await self._build_views([agent]))[0].effective_status

    async def lifecycle_history(
        self, agent_kind: str, agent_id: uuid.UUID,
    ) -> list[WorkforceLifecycleEvent]:
        """Append-only lifecycle history, newest first."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        await self._authorize_agent_read(agent)
        rows = await self.db.scalars(select(WorkforceLifecycleEvent).where(
            WorkforceLifecycleEvent.agent_kind == agent.kind,
            WorkforceLifecycleEvent.agent_id == agent.agent_id,
        ).order_by(WorkforceLifecycleEvent.created_at.desc(),
                   WorkforceLifecycleEvent.id.desc()))
        return list(rows)

    # -- lifecycle writes ----------------------------------------------------------

    async def _agent_or_404(self, agent_kind: str, agent_id: uuid.UUID) -> ResolvedAgent:
        agent = await resolve_agent(self.db, agent_kind, agent_id)
        if agent is None:
            raise NotFoundError("Workforce agent not found.")
        return agent

    def _validate_reason(self, reason: str) -> str:
        clean = " ".join(str(reason or "").split())
        if not 1 <= len(clean) <= 500:
            raise InvalidInputError("A lifecycle reason of 1–500 characters is required.")
        return clean

    async def _locked_registry_row(
        self, agent: ResolvedAgent,
    ) -> WorkforceRegistryAgent:
        """The agent's registry row, locked; created (state ``active``) on first use."""
        row = await self.db.scalar(select(WorkforceRegistryAgent).where(
            WorkforceRegistryAgent.agent_kind == agent.kind,
            WorkforceRegistryAgent.agent_id == agent.agent_id).with_for_update())
        if row is None:
            row = WorkforceRegistryAgent(
                agent_kind=agent.kind, agent_id=agent.agent_id,
                owner_user_id=agent.owner_id,
                manager_group_id=agent.manager_group_id,
                lifecycle_state=LIFECYCLE_ACTIVE)
            self.db.add(row)
            await self.db.flush()
        return row

    async def _apply_transition(
        self, agent: ResolvedAgent, to_state: str, reason: str,
    ) -> RegistryAgentView:
        """Move an agent to ``to_state`` (idempotent no-op when already there)."""
        clean_reason = self._validate_reason(reason)
        state, error = validate_lifecycle_state(to_state)
        if error is not None:
            raise InvalidInputError(error)
        assert state is not None
        row = await self._locked_registry_row(agent)
        if row.lifecycle_state == state:
            await self.db.commit()
            return (await self._build_views([agent]))[0]
        if not transition_allowed(row.lifecycle_state, state):
            # Capture the state BEFORE the rollback: rollback expires the
            # row, and reading an expired attribute on an AsyncSession
            # raises MissingGreenlet (no greenlet context here).
            current = row.lifecycle_state
            await self.db.rollback()
            raise InvalidInputError(
                f"Cannot move a {current} agent to {state}.")
        from_state = row.lifecycle_state
        row.lifecycle_state = state
        row.state_reason = clean_reason
        row.state_changed_by_user_id = self.actor.user_id
        row.state_changed_at = datetime.now(UTC)
        # The owner snapshot refreshes from the island agent itself — the
        # registry never substitutes an owner.
        row.owner_user_id = agent.owner_id
        row.manager_group_id = agent.manager_group_id
        await self.db.flush()
        await self.db.refresh(row)
        if row.lifecycle_state != state:
            await self.db.rollback()
            raise InvalidInputError("Lifecycle change did not persist.")
        self.db.add(WorkforceLifecycleEvent(
            agent_kind=agent.kind, agent_id=agent.agent_id,
            from_state=from_state, to_state=state,
            actor_user_id=self.actor.user_id,
            owner_user_id=agent.owner_id, reason=clean_reason))
        event_name = {
            LIFECYCLE_PAUSED: "workforce.lifecycle_paused",
            LIFECYCLE_RETIRED: "workforce.lifecycle_retired",
            LIFECYCLE_ACTIVE: "workforce.lifecycle_resumed",
        }[state]
        audit.record(self.db, self.ctx, event_name,
                     target=str(agent.agent_id),
                     details={"agent_kind": agent.kind, "from_state": from_state,
                              "to_state": state, "reason": clean_reason})
        await self.db.commit()
        return (await self._build_views([agent]))[0]

    async def pause_agent(
        self, agent_kind: str, agent_id: uuid.UUID, *, reason: str,
    ) -> RegistryAgentView:
        """Pause an agent (owner, admin, or manage holder)."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        await self._authorize_lifecycle(agent, retire=False)
        return await self._apply_transition(agent, LIFECYCLE_PAUSED, reason)

    async def resume_agent(
        self, agent_kind: str, agent_id: uuid.UUID, *, reason: str,
    ) -> RegistryAgentView:
        """Resume a paused agent (owner, admin, or manage holder)."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        await self._authorize_lifecycle(agent, retire=False)
        return await self._apply_transition(agent, LIFECYCLE_ACTIVE, reason)

    async def retire_agent(
        self, agent_kind: str, agent_id: uuid.UUID, *, reason: str,
    ) -> RegistryAgentView:
        """Retire an agent, terminally (admin or manage holder only)."""
        agent = await self._agent_or_404(agent_kind, agent_id)
        await self._authorize_lifecycle(agent, retire=True)
        return await self._apply_transition(agent, LIFECYCLE_RETIRED, reason)

    # -- owner revocation ----------------------------------------------------------

    async def _freeze_agent_skills(
        self, agent: ResolvedAgent, *, now: datetime,
    ) -> int:
        """Freeze an agent's verified skills: move ``expires_at`` to now.

        The verification history is preserved (``verified_at`` /
        ``verified_by`` stay); the rows read as ``expired`` from this
        moment and never match an assignment again. Returns how many
        rows were frozen.
        """
        rows = await self.db.scalars(select(WorkforceAgentSkill).where(
            WorkforceAgentSkill.agent_kind == agent.kind,
            WorkforceAgentSkill.agent_id == agent.agent_id,
            WorkforceAgentSkill.verified_at.is_not(None),
            or_(WorkforceAgentSkill.expires_at.is_(None),
                WorkforceAgentSkill.expires_at > now)).with_for_update())
        frozen = 0
        for row in rows.all():
            # The expiry-after-verification check constraint needs
            # expires_at strictly after verified_at.
            freeze_at = now
            if row.verified_at is not None and freeze_at <= row.verified_at:
                freeze_at = row.verified_at + timedelta(microseconds=1)
            row.expires_at = freeze_at
            frozen += 1
        return frozen

    async def apply_owner_revocation(
        self, owner_id: uuid.UUID, *, reason: str,
    ) -> dict[str, int]:
        """Pause a revoked owner's agents and freeze their verified skills.

        Refuses to run for a user whose access has not actually been
        revoked. Profile agents are department-owned and are not paused
        by a single user's revocation. Ownership is never reassigned.
        Returns counts: agents paused, already paused, retired (skipped),
        and skills frozen.
        """
        await self._authorize_revocation_hook()
        return await self._apply_owner_revocation_inner(owner_id, reason=reason)

    async def _apply_owner_revocation_inner(
        self, owner_id: uuid.UUID, *, reason: str,
    ) -> dict[str, int]:
        """The revocation work itself, without the manage gate.

        Used by ``apply_owner_revocation`` (gated) and by
        ``apply_owner_revocation_for_platform`` (the caller already
        holds ``identity.revoke`` — the revocation itself is the
        authorization, so re-checking a workforce permission would
        refuse a legitimate revocation).
        """
        clean_reason = self._validate_reason(reason)
        owner = await self.db.get(User, owner_id)
        if owner is None:
            raise NotFoundError("Person not found.")
        if owner.is_active:
            raise InvalidInputError(
                "Owner access has not been revoked; refusing to pause their agents.")
        now = datetime.now(UTC)
        agents = [agent for agent in await self._all_agents()
                  if agent.owner_id == owner_id and agent.kind in ("studio", "governance")]
        counts = {"agents_paused": 0, "agents_already_paused": 0,
                  "agents_retired_skipped": 0, "skills_frozen": 0}
        for agent in agents:
            row = await self._locked_registry_row(agent)
            if row.lifecycle_state == LIFECYCLE_RETIRED:
                counts["agents_retired_skipped"] += 1
            elif row.lifecycle_state == LIFECYCLE_PAUSED:
                counts["agents_already_paused"] += 1
            else:
                from_state = row.lifecycle_state
                row.lifecycle_state = LIFECYCLE_PAUSED
                row.state_reason = REVOKE_REASON
                row.state_changed_by_user_id = self.actor.user_id
                row.state_changed_at = now
                row.owner_user_id = agent.owner_id
                row.manager_group_id = agent.manager_group_id
                await self.db.flush()
                await self.db.refresh(row)
                if row.lifecycle_state != LIFECYCLE_PAUSED:
                    await self.db.rollback()
                    raise InvalidInputError("Revocation pause did not persist.")
                self.db.add(WorkforceLifecycleEvent(
                    agent_kind=agent.kind, agent_id=agent.agent_id,
                    from_state=from_state, to_state=LIFECYCLE_PAUSED,
                    actor_user_id=self.actor.user_id,
                    owner_user_id=agent.owner_id, reason=REVOKE_REASON))
                audit.record(self.db, self.ctx, "workforce.lifecycle_paused",
                             target=str(agent.agent_id),
                             details={"agent_kind": agent.kind, "from_state": from_state,
                                      "to_state": LIFECYCLE_PAUSED, "reason": REVOKE_REASON})
                counts["agents_paused"] += 1
            counts["skills_frozen"] += await self._freeze_agent_skills(agent, now=now)
        audit.record(self.db, self.ctx, "workforce.owner_revocation_applied",
                     target=str(owner_id),
                     details={"reason": clean_reason, **counts})
        await self.db.commit()
        return counts


async def apply_owner_revocation_for_platform(
    db: AsyncSession, actor: Principal, ctx: AuditContext, owner_id: uuid.UUID,
    *, reason: str,
) -> dict[str, int]:
    """Module-level revocation hook for ``app.platform.access``.

    The caller already holds ``identity.revoke`` — the revocation itself
    is the authorization — so this calls the ungated inner method, not
    the manage-gated ``apply_owner_revocation`` (re-checking a workforce
    permission here would refuse a legitimate revocation). This function
    was referenced by the docstring and by ``revoke_user_access`` but was
    never defined; the hook's broad ``except`` in ``access.py`` swallowed
    the ImportError and the revocation silently did nothing (caught by
    the slice-5 DB test).
    """
    service = WorkforceRegistryService(db, actor, ctx)
    return await service._apply_owner_revocation_inner(owner_id, reason=reason)
