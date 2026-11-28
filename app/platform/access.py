"""Identity and permission decisions shared by every module and channel.

One rule everywhere: a principal (web session, verified channel identity or agent) acts with the memberships of its
user. A membership on a scope applies to that scope and its descendants. No membership on the scope chain -> 404
(the resource is not revealed); membership without the permission -> 403.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from app.core.security import hash_session_token
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import ROLE_ADMIN, User, UserSession
from app.platform.models import (
    PlatformApprovalRequest, PlatformChannelIdentity, PlatformMembership, PlatformRole, PlatformRolePermission,
    PlatformScope,
)

WILDCARD = "*"
BASELINE_SOURCE = "legacy:users"
# Until module memberships move onto the platform (step D), installation administrators keep these platform
# administration permissions on every scope. Business permissions (invoice.verify, payment.approve,
# funds.transfer, ...) are never implied by the legacy admin role.
ADMIN_BRIDGE_PERMISSIONS = frozenset({"identity.revoke", "org.settings.manage", "platform.manage"})
# Exercising these permissions needs an approval of the given permission first.
APPROVAL_REQUIRED = {"funds.transfer": "payment.approve"}
# Separation of duties: one person may perform at most one of these duties on the same subject (e.g. one invoice).
SEPARATED_DUTIES = frozenset({"invoice.verify", "payment.approve", "funds.transfer"})


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    email: str
    kind: Literal["user", "agent", "service"]
    channel: str
    auth_version: int
    trace_id: uuid.UUID = field(default_factory=uuid.uuid4)
    session_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    requires_approval: bool = False
    approval_permission: str | None = None


@dataclass(frozen=True)
class ScopeRef:
    id: uuid.UUID
    kind: str
    name: str


@dataclass(frozen=True)
class ScopeFilter:
    scope_ids: frozenset[uuid.UUID]

    def sql(self, column):
        """A WHERE clause for a scope_id column; an empty filter matches nothing."""
        return column.in_(sorted(self.scope_ids)) if self.scope_ids else literal(False)


def _now() -> datetime:
    return datetime.now(UTC)


def principal_for(user: User, *, channel: str = "web", kind: str = "user", session_id: uuid.UUID | None = None,
                  agent_id: uuid.UUID | None = None, trace_id: uuid.UUID | None = None) -> Principal:
    """A Principal for an already-authenticated user object (web request, worker acting for a stored owner).
    Its auth_version is the one loaded with the user, so a revoke between load and use is still detected."""
    from sqlalchemy import inspect as sa_inspect

    version = 0 if "auth_version" in sa_inspect(user).unloaded else (user.auth_version or 0)
    return Principal(user.id, user.email, kind, channel, version, trace_id or uuid.uuid4(), session_id, agent_id)


# ----------------------------------------------------------------------------------------------- principals

async def resolve_principal(
    db: AsyncSession, *, session_token: str | None = None, channel: str | None = None,
    external_id: str | None = None, agent_id: uuid.UUID | None = None, trace_id: uuid.UUID | None = None,
) -> Principal:
    """Resolve exactly one source to the acting principal, or raise PermissionDeniedError."""
    sources = [session_token is not None, channel is not None or external_id is not None, agent_id is not None]
    if sum(sources) != 1:
        raise InvalidInputError("Resolve a principal from exactly one source.")
    trace = trace_id or uuid.uuid4()
    now = _now()
    if session_token is not None:
        row = (await db.execute(
            select(User, UserSession.id).join(UserSession, UserSession.user_id == User.id).where(
                UserSession.token_hash == hash_session_token(session_token), UserSession.revoked_at.is_(None),
                UserSession.expires_at > now, User.is_active.is_(True),
            )
        )).first()
        if row is None:
            raise PermissionDeniedError("Your session has ended. Sign in again.")
        user, session_id = row
        return Principal(user.id, user.email, "user", "web", user.auth_version, trace, session_id=session_id)
    if agent_id is not None:
        row = (await db.execute(
            select(User, AgentStudioAgent).join(AgentStudioAgent, AgentStudioAgent.owner_id == User.id).where(
                AgentStudioAgent.id == agent_id, User.is_active.is_(True)
            )
        )).first()
        if row is None or not row[1].enabled or row[1].paused:
            raise PermissionDeniedError("This agent is not available to act.")
        user, agent = row
        return Principal(user.id, user.email, "agent", "agent", user.auth_version, trace, agent_id=agent.id)
    if not channel or not external_id:
        raise InvalidInputError("Both the channel and the external identity are required.")
    user = await db.scalar(
        select(User).join(PlatformChannelIdentity, PlatformChannelIdentity.user_id == User.id).where(
            PlatformChannelIdentity.channel == channel, PlatformChannelIdentity.external_id == external_id,
            PlatformChannelIdentity.revoked_at.is_(None), PlatformChannelIdentity.verified_at.is_not(None),
            User.is_active.is_(True),
        )
    )
    if user is None:
        raise PermissionDeniedError("This number is not linked to an InfraSpine account.")
    return Principal(user.id, user.email, "user", channel, user.auth_version, trace)


# ----------------------------------------------------------------------------------------------- scopes

async def ensure_org_scope(db: AsyncSession, name: str = "Organisation") -> ScopeRef:
    await db.execute(insert(PlatformScope).values(id=uuid.uuid4(), kind="org", parent_id=None, name=name)
                     .on_conflict_do_nothing())
    row = await db.scalar(select(PlatformScope).where(PlatformScope.kind == "org"))
    return ScopeRef(row.id, row.kind, row.name)


async def create_scope(db: AsyncSession, *, kind: str, parent_id: uuid.UUID, name: str, module: str | None = None,
                       legacy_table: str | None = None, legacy_id: uuid.UUID | None = None) -> ScopeRef:
    if kind == "org":
        raise InvalidInputError("Use ensure_org_scope for the organisation.")
    if await db.get(PlatformScope, parent_id) is None:
        raise NotFoundError("Scope not found.")
    row = PlatformScope(kind=kind, parent_id=parent_id, name=name, module=module, legacy_table=legacy_table,
                        legacy_id=legacy_id)
    db.add(row)
    await db.flush()
    return ScopeRef(row.id, row.kind, row.name)


async def _scope_chain(db: AsyncSession, scope_id: uuid.UUID) -> list[uuid.UUID]:
    """The scope and its ancestors (nearest first); empty if the scope is missing or inactive."""
    chain = select(PlatformScope.id, PlatformScope.parent_id).where(
        PlatformScope.id == scope_id, PlatformScope.active.is_(True)
    ).cte("chain", recursive=True)
    parent = select(PlatformScope.id, PlatformScope.parent_id).join(chain, PlatformScope.id == chain.c.parent_id) \
        .where(PlatformScope.active.is_(True))
    chain = chain.union_all(parent)
    return list(await db.scalars(select(chain.c.id)))


def _active_membership(now: datetime):
    return and_(
        PlatformMembership.revoked_at.is_(None),
        or_(PlatformMembership.valid_from.is_(None), PlatformMembership.valid_from <= now),
        or_(PlatformMembership.valid_to.is_(None), PlatformMembership.valid_to > now),
    )


async def _permissions_on(db: AsyncSession, user_id: uuid.UUID, chain: list[uuid.UUID]) -> tuple[bool, set[str]]:
    now = _now()
    # The baseline organisation membership every active account has (mirrored from users) grants org-wide
    # permissions but does not reveal department/module scopes: without a membership there they stay 404.
    member = await db.scalar(select(PlatformMembership.id).where(
        PlatformMembership.user_id == user_id, PlatformMembership.scope_id.in_(chain), _active_membership(now),
        or_(PlatformMembership.source != BASELINE_SOURCE, PlatformMembership.scope_id == chain[0]),
    ).limit(1))
    perms = set(await db.scalars(
        select(PlatformRolePermission.permission)
        .join(PlatformMembership, PlatformMembership.role_id == PlatformRolePermission.role_id)
        .where(PlatformMembership.user_id == user_id, PlatformMembership.scope_id.in_(chain), _active_membership(now))
    ))
    return member is not None, perms


async def effective_permissions(db: AsyncSession, user_id: uuid.UUID, scope_id: uuid.UUID) -> set[str]:
    """Permissions a user holds on a scope right now (memberships on the scope and its ancestors)."""
    chain = await _scope_chain(db, scope_id)
    if not chain:
        return set()
    _, perms = await _permissions_on(db, user_id, chain)
    return perms


# ----------------------------------------------------------------------------------------------- decisions

async def _principal_current(db: AsyncSession, p: Principal) -> tuple[User | None, str | None]:
    user = await db.get(User, p.user_id, populate_existing=True)
    if user is None or not user.is_active:
        return None, "user_inactive"
    if user.auth_version != p.auth_version:
        return None, "principal_revoked"
    if p.session_id is not None:
        session = await db.get(UserSession, p.session_id, populate_existing=True)
        if session is None or session.revoked_at is not None or session.expires_at <= _now():
            return None, "session_revoked"
    if p.kind == "agent":
        agent = await db.get(AgentStudioAgent, p.agent_id, populate_existing=True)
        if agent is None or not agent.enabled or agent.paused:
            return None, "agent_unavailable"
    return user, None


async def authorize(db: AsyncSession, p: Principal, permission: str, scope_id: uuid.UUID, *,
                    amount: Decimal | None = None) -> Decision:
    user, problem = await _principal_current(db, p)
    if problem:
        return Decision(False, problem)
    chain = await _scope_chain(db, scope_id)
    if not chain:
        return Decision(False, "scope_missing")
    member, perms = await _permissions_on(db, p.user_id, chain)
    if user.role == ROLE_ADMIN and permission in ADMIN_BRIDGE_PERMISSIONS:
        return Decision(True, "legacy_admin")
    if not member:
        return Decision(False, "not_a_member")
    if permission not in perms and WILDCARD not in perms:
        return Decision(False, "permission_missing")
    needed = APPROVAL_REQUIRED.get(permission)
    return Decision(True, "granted", requires_approval=needed is not None, approval_permission=needed)


async def require(db: AsyncSession, p: Principal, permission: str, scope_id: uuid.UUID, *,
                  amount: Decimal | None = None) -> ScopeRef:
    decision = await authorize(db, p, permission, scope_id, amount=amount)
    if not decision.allowed:
        if decision.reason in {"scope_missing", "not_a_member"}:
            raise NotFoundError("Not found.")
        raise PermissionDeniedError("You do not have permission for this action.")
    scope = await db.get(PlatformScope, scope_id)
    return ScopeRef(scope.id, scope.kind, scope.name)


async def visible_scopes(db: AsyncSession, p: Principal, permission: str) -> ScopeFilter:
    """Every active scope where the principal holds `permission` (granted scopes and their descendants)."""
    _, problem = await _principal_current(db, p)
    if problem:
        return ScopeFilter(frozenset())
    now = _now()
    roots = (
        select(PlatformMembership.scope_id.label("id"))
        .join(PlatformRolePermission, PlatformRolePermission.role_id == PlatformMembership.role_id)
        .join(PlatformScope, PlatformScope.id == PlatformMembership.scope_id)
        .where(PlatformMembership.user_id == p.user_id, _active_membership(now), PlatformScope.active.is_(True),
               PlatformRolePermission.permission.in_((permission, WILDCARD)))
    ).cte("granted", recursive=True)
    children = select(PlatformScope.id).join(roots, PlatformScope.parent_id == roots.c.id).where(PlatformScope.active.is_(True))
    tree = roots.union_all(children)
    return ScopeFilter(frozenset(await db.scalars(select(tree.c.id).distinct())))


# ----------------------------------------------------------------------------------------------- administration

async def ensure_role(db: AsyncSession, key: str, permissions: set[str] | frozenset[str], *, name: str | None = None,
                      system: bool = False) -> uuid.UUID:
    await db.execute(insert(PlatformRole).values(id=uuid.uuid4(), key=key, name=name or key, system=system)
                     .on_conflict_do_nothing(index_elements=[PlatformRole.key]))
    role_id = await db.scalar(select(PlatformRole.id).where(PlatformRole.key == key))
    for permission in permissions:
        await db.execute(insert(PlatformRolePermission).values(role_id=role_id, permission=permission)
                         .on_conflict_do_nothing())
    return role_id


async def grant_membership(db: AsyncSession, actor: Principal, *, user_id: uuid.UUID, scope_id: uuid.UUID,
                           role_key: str, valid_to: datetime | None = None) -> PlatformMembership:
    await require(db, actor, "platform.manage", scope_id)
    role_id = await db.scalar(select(PlatformRole.id).where(PlatformRole.key == role_key))
    if role_id is None:
        raise NotFoundError("Role not found.")
    row = PlatformMembership(scope_id=scope_id, user_id=user_id, role_id=role_id, valid_to=valid_to,
                             granted_by=actor.user_id)
    db.add(row)
    await db.flush()
    return row


async def revoke_user_access(db: AsyncSession, actor: Principal, user_id: uuid.UUID, reason: str) -> dict[str, int]:
    """Stop a user everywhere at once: memberships, sessions, channel identities, pending approval requests, and
    any Principal already resolved for them (auth_version bump). The account itself is not deleted."""
    from app.platform.events import audit, publish

    org = await ensure_org_scope(db)
    await require(db, actor, "identity.revoke", org.id)
    if actor.user_id == user_id:
        raise InvalidInputError("You cannot revoke your own access.")
    target = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if target is None:
        raise NotFoundError("Person not found.")
    from sqlalchemy import delete

    from app.db.models import AccessGroupMember, DepartmentManager
    from app.platform.handlers import cancel_queued_work
    from app.platform.sync import MODULES, _member_table

    now = _now()
    counts = {}
    # Stamp platform memberships first. Setting is_active flushes the ORM hook, which would otherwise
    # mark the same rows as legacy:removed and let a later reactivation re-grant them.
    counts["memberships"] = (await db.execute(
        update(PlatformMembership).where(PlatformMembership.user_id == user_id,
                                         PlatformMembership.revoked_at.is_(None))
        .values(revoked_at=now, revoke_reason="platform:access_revoked")
    )).rowcount or 0
    target.auth_version += 1
    target.is_active = False  # sign-in is refused until an administrator deliberately reactivates the account
    legacy = []
    for spec in MODULES.values():  # module member tables: deactivate (rows kept for history) or remove
        table = _member_table(spec)
        if "active" in table.c:
            legacy.append((f"legacy_{spec.module}", update(table).where(table.c.user_id == user_id, table.c.active.is_(True))
                           .values(active=False)))
        else:
            legacy.append((f"legacy_{spec.module}", delete(table).where(table.c.user_id == user_id)))
    legacy.append(("legacy_group_memberships", delete(AccessGroupMember).where(AccessGroupMember.user_id == user_id)))
    legacy.append(("legacy_department_manager", delete(DepartmentManager).where(DepartmentManager.manager_user_id == user_id)))
    for name, statement in (
        *legacy,
        ("sessions", update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
         .values(revoked_at=now)),
        ("channel_identities", update(PlatformChannelIdentity).where(
            PlatformChannelIdentity.user_id == user_id, PlatformChannelIdentity.revoked_at.is_(None)).values(revoked_at=now)),
        ("pending_approvals", update(PlatformApprovalRequest).where(
            PlatformApprovalRequest.requested_by == user_id, PlatformApprovalRequest.status == "pending")
         .values(status="cancelled", decided_at=now, decision_note="requester access revoked")),
    ):
        counts[name] = (await db.execute(statement)).rowcount or 0
    counts.update(await cancel_queued_work(db, user_id))
    reason = reason.strip()[:300] or "unspecified"
    await publish(db, "identity.session_revoked", org.id, {"user_id": str(user_id), "reason": reason, **counts},
                  actor.trace_id)
    audit(db, actor, "platform.access_revoked", target=str(user_id), details={"reason": reason, **counts})
    await db.commit()
    # Step J slice 5: the revoked owner's workforce agents are paused and
    # their verified skills frozen. This runs after the commit above, in
    # its own transaction, and never fails the revocation itself: even if
    # the hook errors, the registry read view already derives "paused"
    # from the revoked owner, and an administrator can re-run the hook
    # from POST /api/workforce/owner-revocations. Local import: the
    # workforce package imports this module.
    try:
        from app.services.audit import AuditContext
        from app.workforce.registry_service import apply_owner_revocation_for_platform

        hook_ctx = AuditContext(actor_user_id=actor.user_id, actor_email=actor.email,
                                ip_address=None, request_id=str(actor.trace_id))
        hook_counts = await apply_owner_revocation_for_platform(
            db, actor, hook_ctx, user_id, reason=f"access revoked: {reason}")
        counts["workforce_agents_paused"] = hook_counts["agents_paused"]
        counts["workforce_skills_frozen"] = hook_counts["skills_frozen"]
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "Workforce owner-revocation hook failed for %s; the registry "
            "still derives the pause from the revoked owner.",
            user_id, exc_info=True)
    return counts
