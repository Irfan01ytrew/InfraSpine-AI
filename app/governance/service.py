"""Tenant administration and fresh authorization; titles never imply permissions."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import ROLE_ADMIN, Document, User, UserSession
from app.governance import models as m
from app.governance.schemas import PERMISSIONS
from app.services import audit
from app.services.audit import AuditContext


def now():
    return datetime.now(UTC)


async def event(db, tenant_id, actor, action, *, outcome="success", target=None, details=None):
    # Deliberately no arbitrary prompts, action arguments, credentials or document text.
    ctx = AuditContext(actor.id, actor.email, None, None)
    audit.record(db, ctx, "security." + action, outcome=outcome, target=str(target or tenant_id), details=details or {})
    await db.execute(insert(m.SecurityEvent).values(tenant_id=tenant_id, body={
        "actor_id": str(actor.id), "action": action, "outcome": outcome,
        "target": str(target or tenant_id), "at": now().isoformat(), "details": details or {},
    }))


async def deny(db, actor, tenant_id, reason):
    ctx = AuditContext(actor.id, actor.email, None, None)
    await audit.record_failure_now(db, ctx, "security.authorization_denied", target=str(tenant_id), details={"reason": reason})
    raise PermissionDeniedError("The current identity is not authorized for this tenant or action.")


async def grants(db, tenant_id, user_id):
    rows = (await db.execute(select(m.Role).join(m.Assignment, (m.Assignment.tenant_id == m.Role.tenant_id) & (m.Assignment.role_id == m.Role.id)).where(
        m.Role.tenant_id == tenant_id, m.Assignment.user_id == user_id,
        m.Assignment.revoked_at.is_(None), or_(m.Assignment.expires_at.is_(None), m.Assignment.expires_at > func.now()),
    ))).scalars().all()
    return {str(row.id) for row in rows}, {p for row in rows for p in row.permissions}


async def context(db: AsyncSession, actor: User, tenant_id, permission=None, *, lock=False):
    stmt = select(m.Tenant).where(m.Tenant.id == tenant_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    tenant = await db.scalar(stmt)
    member = await db.scalar(select(m.Member).join(User, User.id == m.Member.user_id).where(
        m.Member.tenant_id == tenant_id, m.Member.user_id == actor.id,
        m.Member.active.is_(True), User.is_active.is_(True),
        or_(m.Member.expires_at.is_(None), m.Member.expires_at > func.now()),
    ).execution_options(populate_existing=True))
    if tenant is None or not tenant.enabled or member is None:
        await deny(db, actor, tenant_id, "tenant_or_membership_inactive")
    roles, permissions = await grants(db, tenant_id, actor.id)
    if permission and permission not in permissions:
        await deny(db, actor, tenant_id, "permission_missing")
    return tenant, member, roles, permissions


async def scoped(db, model, tenant_id, object_id):
    row = await db.scalar(select(model).where(model.tenant_id == tenant_id, model.id == object_id).execution_options(populate_existing=True))
    if row is None:
        raise NotFoundError()
    return row


async def check_department(db, tenant_id, department_id):
    if department_id:
        await scoped(db, m.Department, tenant_id, department_id)


async def create_tenant(db, actor, payload):
    if actor.role != ROLE_ADMIN:
        await deny(db, actor, uuid.UUID(int=0), "platform_administrator_required")
    # This bootstrap is explicit and only for a currently unbound administrator.
    await db.scalar(select(User).where(User.id == actor.id).with_for_update())
    if await db.scalar(select(m.Member.user_id).where(m.Member.user_id == actor.id)):
        raise ConflictError("This identity already belongs to an organization.")
    tenant = m.Tenant(name=payload.name)
    db.add(tenant)
    await db.flush()
    member = m.Member(tenant_id=tenant.id, user_id=actor.id, all_departments=True)
    db.add(member)
    admin_role = m.Role(tenant_id=tenant.id, name="Tenant Admin", permissions=sorted(PERMISSIONS))
    db.add(admin_role)
    # These are names/templates, not privileged job-title shortcuts.
    for name in ("CEO", "CFO", "Executive", "Director", "Manager", "Employee", "Auditor"):
        db.add(m.Role(tenant_id=tenant.id, name=name, permissions=["security.read", "security.audit"] if name == "Auditor" else []))
    await db.flush()
    db.add(m.Assignment(tenant_id=tenant.id, user_id=actor.id, role_id=admin_role.id))
    await event(db, tenant.id, actor, "tenant_created")
    await db.commit()
    return tenant


async def add_member(db, actor, tenant_id, payload):
    await context(db, actor, tenant_id, "security.manage", lock=True)
    await check_department(db, tenant_id, payload.department_id)
    user = await db.scalar(select(User).where(func.lower(User.email) == payload.email.lower(), User.is_active.is_(True)).with_for_update())
    if user is None:
        raise NotFoundError("An active provisioned user is required.")
    if await db.scalar(select(m.Member.user_id).where(m.Member.user_id == user.id)):
        raise ConflictError("The identity is already bound to an organization; membership cannot be moved.")
    if payload.expires_at and payload.expires_at <= now():
        raise InvalidInputError("Membership expiry must be in the future.")
    row = m.Member(tenant_id=tenant_id, user_id=user.id, **payload.model_dump(exclude={"email"}))
    db.add(row)
    await db.flush()
    await event(db, tenant_id, actor, "member_added", target=user.id)
    await db.commit()
    return row


async def revoke_member(db, actor, tenant_id, user_id):
    await context(db, actor, tenant_id, "security.manage", lock=True)
    if actor.id == user_id:
        raise PermissionDeniedError("Another authorized administrator must revoke this membership.")
    member = await db.get(m.Member, (tenant_id, user_id))
    if member is None:
        raise NotFoundError()
    member.active = False
    await db.execute(update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None)).values(revoked_at=now()))
    await event(db, tenant_id, actor, "member_revoked", target=user_id)
    await db.commit()


async def assign_role(db, actor, tenant_id, payload):
    await context(db, actor, tenant_id, "security.manage", lock=True)
    if actor.id == payload.user_id:
        raise PermissionDeniedError("Self-assignment is not permitted.")
    member = await db.get(m.Member, (tenant_id, payload.user_id))
    if member is None or not member.active:
        raise NotFoundError()
    role = await scoped(db, m.Role, tenant_id, payload.role_id)
    if payload.expires_at and payload.expires_at <= now():
        raise InvalidInputError("Role expiry must be in the future.")
    existing = await db.get(m.Assignment, (tenant_id, payload.user_id, role.id))
    if existing is not None:
        raise ConflictError("This role assignment already exists.")
    row = m.Assignment(tenant_id=tenant_id, **payload.model_dump())
    db.add(row)
    await event(db, tenant_id, actor, "role_assigned", target=payload.user_id, details={"role_id": str(role.id)})
    await db.commit()
    return row


async def create_policy(db, actor, tenant_id, payload):
    tenant, *_ = await context(db, actor, tenant_id, "security.manage", lock=True)
    for rule in payload.rules:
        for model, values in ((m.Role, rule.role_ids), (m.Department, rule.department_ids), (m.Resource, rule.resource_ids)):
            for value in values:
                await scoped(db, model, tenant_id, value)
    version = (await db.scalar(select(func.max(m.PolicyVersion.version)).where(m.PolicyVersion.tenant_id == tenant_id)) or 0) + 1
    row = m.PolicyVersion(tenant_id=tenant_id, version=version, base_version=tenant.policy_version, rules=payload.model_dump(mode="json")["rules"], created_by=actor.id)
    db.add(row)
    await event(db, tenant_id, actor, "policy_proposed", details={"version": version})
    await db.commit()
    return row


async def activate_policy(db, actor, tenant_id, version):
    tenant, *_ = await context(db, actor, tenant_id, "security.manage", lock=True)
    policy = await db.get(m.PolicyVersion, (tenant_id, version))
    if policy is None:
        raise NotFoundError()
    if policy.created_by == actor.id:
        raise PermissionDeniedError("A different authorized administrator must review this policy.")
    if policy.base_version != tenant.policy_version:
        raise ConflictError("The active policy changed. Propose and review a fresh version.")
    tenant.policy_version = version
    await event(db, tenant_id, actor, "policy_activated", details={"version": version})
    await db.commit()


async def register_resource(db, actor, tenant_id, payload):
    await context(db, actor, tenant_id, "security.manage", lock=True)
    await check_department(db, tenant_id, payload.department_id)
    if payload.kind == "document":
        from app.rag.access import readable_by
        doc = await db.scalar(select(Document).where(Document.id == payload.external_id, readable_by(actor)))
        if doc is None:
            raise NotFoundError()
        owner = await db.get(m.Member, (tenant_id, doc.owner_id))
        if owner is None:
            raise PermissionDeniedError("The source owner must belong to this tenant.")
        label = doc.filename[:160]
    else:
        agent = await scoped(db, m.AgentIdentity, tenant_id, payload.external_id)
        label = agent.name
    if await db.scalar(select(m.Resource.id).where(m.Resource.kind == payload.kind, m.Resource.external_id == payload.external_id)):
        raise ConflictError("This resource is already registered.")
    row = m.Resource(tenant_id=tenant_id, label=label, **payload.model_dump())
    db.add(row)
    await db.flush()
    await event(db, tenant_id, actor, "resource_registered", target=row.id)
    await db.commit()
    return row
