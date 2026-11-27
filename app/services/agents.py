"""Persisted department-scoped agent profiles and fixed tool-policy validation."""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permission_catalog import PERMISSION_AGENTS_MANAGE
from app.services.access_control import delegated_department_scope, holds_department_capability
from app.core.crypto import CredentialCipher, EncryptedValue
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    AccessGroup,
    AccessGroupMember,
    AgentProfile,
    AgentProfileRun,
    DepartmentManager,
    User,
)
from app.services import audit
from app.services.audit import AuditContext

_NOT_FOUND = "Agent profile not found."
_TOOLS_BY_TYPE = {
    "knowledge_research": frozenset({"knowledge_search"}),
    "market_research": frozenset({"knowledge_search", "public_web_research"}),
    "company_analysis": frozenset({"knowledge_search", "company_data_analysis"}),
    "employee_training": frozenset({"knowledge_search", "training_plan"}),
}
_TOOL_REGISTRY = frozenset().union(*_TOOLS_BY_TYPE.values())
_RETRYABLE_RUN_CAPABILITIES = frozenset({"knowledge_search", "company_data_analysis"})
AGENT_RUN_STALE_AFTER = timedelta(minutes=30)
AGENT_RUN_RECOVERY_BATCH_SIZE = 100


def seal_run_value(cipher: CredentialCipher, run_id: uuid.UUID, purpose: str, value: str) -> EncryptedValue:
    aad = f"infraspine:agent-profile-run:v1:{run_id}:{purpose}".encode("ascii")
    return cipher.encrypt(value, associated_data=aad)


def open_run_value(cipher: CredentialCipher, run: AgentProfileRun, purpose: str) -> str:
    encrypted = EncryptedValue(
        ciphertext=getattr(run, f"{purpose}_ciphertext"),
        nonce=getattr(run, f"{purpose}_nonce"),
        key_id=getattr(run, f"{purpose}_key_id"),
    )
    aad = f"infraspine:agent-profile-run:v1:{run.id}:{purpose}".encode("ascii")
    return cipher.decrypt(encrypted, associated_data=aad)


async def recover_abandoned_runs(sessionmaker, *, now: datetime | None = None) -> int:
    """Mark old in-flight records interrupted after an API process restart, with one audit event each."""
    now = now or datetime.now(timezone.utc)
    stale_before = now - AGENT_RUN_STALE_AFTER
    context = AuditContext(None, "system:agent-run-recovery", None, None)
    recovered = 0
    while True:
        async with sessionmaker() as db:
            rows = list(
                (
                    await db.execute(
                        select(AgentProfileRun)
                        .where(
                            AgentProfileRun.status == "running",
                            or_(
                                and_(
                                    AgentProfileRun.execution_mode == "queue",
                                    AgentProfileRun.locked_until.is_not(None),
                                    AgentProfileRun.locked_until <= now,
                                ),
                                and_(
                                    AgentProfileRun.execution_mode != "queue",
                                    AgentProfileRun.started_at <= stale_before,
                                ),
                            ),
                        )
                        .order_by(AgentProfileRun.started_at, AgentProfileRun.id)
                        .limit(AGENT_RUN_RECOVERY_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            if not rows:
                break
            for run in rows:
                run.failure_code = "interrupted"
                run.locked_until = None
                if run.execution_mode == "queue" and run.attempts < 2:
                    run.status = "queued"
                    run.available_at = now
                    run.finished_at = None
                    action = "agent.profile_run_requeued"
                    outcome = "success"
                else:
                    run.status = "failed"
                    run.finished_at = now
                    action = "agent.profile_run_interrupted"
                    outcome = "failure"
                audit.record(
                    db,
                    context,
                    action,
                    outcome=outcome,
                    target=str(run.id),
                    details={"profile_id": str(run.profile_id), "attempt": run.attempts},
                )
            await db.commit()
            recovered += len(rows)
            if len(rows) < AGENT_RUN_RECOVERY_BATCH_SIZE:
                break
    return recovered


async def require_run_owner_access(db: AsyncSession, actor: User, run: AgentProfileRun) -> None:
    """Keep retained result access tied to the original requester and current department membership."""
    if run.requester_user_id != actor.id or run.department_group_id is None:
        raise PermissionDeniedError("This agent run is not available to this account.")
    await _department(db, actor, run.department_group_id, manage=False)


async def require_profile_history_access(db: AsyncSession, actor: User, profile: AgentProfile) -> None:
    """Check present department membership before exposing the caller's retained run history."""
    await _department(db, actor, profile.department_group_id, manage=False)


def _validate_config(*, name: str, agent_type: str, knowledge_scope: str, tool_policy: list[str]) -> tuple[str, list[str]]:
    normalized_name = " ".join(name.split())
    if not normalized_name or len(normalized_name) > 120:
        raise InvalidInputError("Agent name must contain between 1 and 120 characters.")
    if agent_type not in _TOOLS_BY_TYPE:
        raise InvalidInputError("This agent type is not supported.")
    if knowledge_scope not in {"requester_acl", "department_only"}:
        raise InvalidInputError("This knowledge scope is not supported.")
    if not tool_policy or len(tool_policy) > len(_TOOL_REGISTRY):
        raise InvalidInputError("Select at least one registered agent capability.")
    if len(set(tool_policy)) != len(tool_policy):
        raise InvalidInputError("Agent capabilities cannot be repeated.")
    if not set(tool_policy) <= _TOOLS_BY_TYPE[agent_type]:
        raise InvalidInputError("The selected capability is not registered for this agent type.")
    return normalized_name, sorted(tool_policy)


async def _department(db: AsyncSession, actor: User, group_id: uuid.UUID, *, manage: bool) -> AccessGroup:
    group = await db.get(AccessGroup, group_id)
    if group is None or group.kind != "department":
        raise NotFoundError(_NOT_FOUND)
    if actor.role == ROLE_ADMIN:
        return group
    manager = actor.role == ROLE_MANAGER and await db.get(DepartmentManager, (group_id, actor.id)) is not None
    if manage:
        if manager or await holds_department_capability(db, actor.id, PERMISSION_AGENTS_MANAGE, group_id):
            return group
        raise PermissionDeniedError("Only an administrator, the department manager or an agents.manage holder can manage agents.")
    member = await db.get(AccessGroupMember, (group_id, actor.id))
    if manager or member is not None:
        return group
    raise PermissionDeniedError("Department membership or manager access is required.")


def _record(db: AsyncSession, ctx: AuditContext, action: str, agent: AgentProfile, details: dict) -> None:
    audit.record(db, ctx, action, target=str(agent.id), details=details)


async def list_profiles(db: AsyncSession, actor: User) -> list[AgentProfile]:
    if actor.role == ROLE_ADMIN:
        result = await db.execute(select(AgentProfile).order_by(AgentProfile.agent_key, AgentProfile.version))
        return list(result.scalars())

    memberships = await db.execute(
        select(AccessGroupMember.group_id).where(AccessGroupMember.user_id == actor.id)
    )
    manager_groups = await db.execute(
        select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == actor.id)
    )
    managed = set(manager_groups.scalars())
    delegated = await delegated_department_scope(db, actor.id, PERMISSION_AGENTS_MANAGE)
    if delegated is None:  # agents.manage for every department: see every profile, including disabled ones
        result = await db.execute(select(AgentProfile).order_by(AgentProfile.agent_key, AgentProfile.version))
        return list(result.scalars())
    managed |= delegated
    visible_groups = set(memberships.scalars()) | managed
    if not visible_groups:
        return []
    query = select(AgentProfile).where(AgentProfile.department_group_id.in_(visible_groups))
    # Members see enabled agents only; whoever manages a department also sees its disabled agents.
    query = query.where(AgentProfile.enabled.is_(True) | AgentProfile.department_group_id.in_(managed or {None}))
    result = await db.execute(query.order_by(AgentProfile.agent_key, AgentProfile.version))
    return list(result.scalars())


def _validate_run_profile(profile: AgentProfile, capability: str) -> None:
    """Fail closed if an old or hand-edited row does not match the server's capability registry."""
    if not profile.enabled:
        raise NotFoundError(_NOT_FOUND)
    allowed_for_type = _TOOLS_BY_TYPE.get(profile.agent_type)
    if allowed_for_type is None or capability not in allowed_for_type or capability not in profile.tool_policy:
        raise PermissionDeniedError("This capability is not enabled for the selected agent profile.")


async def profile_for_run(
    db: AsyncSession, actor: User, profile_id: uuid.UUID, capability: str
) -> AgentProfile:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        raise NotFoundError(_NOT_FOUND)
    _validate_run_profile(profile, capability)
    await _department(db, actor, profile.department_group_id, manage=False)
    return profile


async def manageable_departments(db: AsyncSession, actor: User) -> list[AccessGroup]:
    query = select(AccessGroup).where(AccessGroup.kind == "department")
    if actor.role != ROLE_ADMIN:
        delegated = await delegated_department_scope(db, actor.id, PERMISSION_AGENTS_MANAGE)
        if delegated is not None:
            managed = select(DepartmentManager.group_id).where(DepartmentManager.manager_user_id == actor.id)
            query = query.where(AccessGroup.id.in_(delegated) | AccessGroup.id.in_(managed))
    result = await db.execute(query.order_by(AccessGroup.name))
    return list(result.scalars())


async def create_profile(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    *,
    name: str,
    department_group_id: uuid.UUID,
    agent_type: str,
    knowledge_scope: str,
    tool_policy: list[str],
) -> AgentProfile:
    group = await _department(db, actor, department_group_id, manage=True)
    normalized_name, normalized_tools = _validate_config(
        name=name, agent_type=agent_type, knowledge_scope=knowledge_scope, tool_policy=tool_policy
    )
    profile = AgentProfile(
        id=uuid.uuid4(),
        agent_key=uuid.uuid4(),
        version=1,
        name=normalized_name,
        agent_type=agent_type,
        department_group_id=group.id,
        knowledge_scope=knowledge_scope,
        tool_policy=normalized_tools,
        enabled=True,
        created_by_user_id=actor.id,
    )
    db.add(profile)
    _record(
        db,
        ctx,
        "agent.profile_created",
        profile,
        {"version": 1, "department_id": str(group.id), "agent_type": agent_type, "tools": normalized_tools},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("The agent profile could not be saved.") from exc
    await db.refresh(profile)
    return profile


async def create_version(
    db: AsyncSession,
    actor: User,
    ctx: AuditContext,
    profile_id: uuid.UUID,
    *,
    expected_version: int,
    name: str,
    agent_type: str,
    knowledge_scope: str,
    tool_policy: list[str],
) -> AgentProfile:
    current = (
        await db.execute(select(AgentProfile).where(AgentProfile.id == profile_id).with_for_update())
    ).scalar_one_or_none()
    if current is None:
        raise NotFoundError(_NOT_FOUND)
    await _department(db, actor, current.department_group_id, manage=True)
    if not current.enabled or current.version != expected_version:
        raise ConflictError("The agent profile version changed. Reload before editing.")
    normalized_name, normalized_tools = _validate_config(
        name=name, agent_type=agent_type, knowledge_scope=knowledge_scope, tool_policy=tool_policy
    )
    current.enabled = False
    await db.flush()
    profile = AgentProfile(
        id=uuid.uuid4(),
        agent_key=current.agent_key,
        version=current.version + 1,
        name=normalized_name,
        agent_type=agent_type,
        department_group_id=current.department_group_id,
        knowledge_scope=knowledge_scope,
        tool_policy=normalized_tools,
        enabled=True,
        created_by_user_id=actor.id,
    )
    db.add(profile)
    _record(
        db,
        ctx,
        "agent.profile_versioned",
        profile,
        {"version": profile.version, "department_id": str(profile.department_group_id), "tools": normalized_tools},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("The agent profile changed concurrently; reload and retry.") from exc
    await db.refresh(profile)
    return profile


async def disable_profile(
    db: AsyncSession, actor: User, ctx: AuditContext, profile_id: uuid.UUID
) -> AgentProfile:
    profile = (
        await db.execute(select(AgentProfile).where(AgentProfile.id == profile_id).with_for_update())
    ).scalar_one_or_none()
    if profile is None:
        raise NotFoundError(_NOT_FOUND)
    await _department(db, actor, profile.department_group_id, manage=True)
    if not profile.enabled:
        return profile
    profile.enabled = False
    _record(db, ctx, "agent.profile_disabled", profile, {"version": profile.version})
    await db.commit()
    await db.refresh(profile)
    return profile
