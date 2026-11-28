"""Mirror existing module tenants/members and groups into platform scopes and memberships (step D, P0–P5).

The module member tables stay the place where their UIs grant roles during the transition; this module keeps the
platform copy in step with them, and the platform is where access is *revoked* and principals are checked:

* backfill_all()      one pass over every legacy table (migration 0100, the worker on start, tests),
* the ORM hook        mirrors every member insert/update/delete made through a session in the same transaction,
* reconcile_member()  the access-time fallback for rows written without the ORM (bulk SQL, restores).

All functions take a *synchronous* Connection so the same code runs inside Alembic, a flush event and
AsyncSession.run_sync. Mirrored rows carry source='legacy:<table>'. A row revoked by the platform
(revoke_reason 'platform:*', e.g. revoke_user_access) is never re-mirrored implicitly; only an explicit module
change made after the revoke (the ORM hook) grants it again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import event, inspect, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.platform.models import PlatformMembership, PlatformRole, PlatformRolePermission, PlatformScope

S, M, R, RP = PlatformScope.__table__, PlatformMembership.__table__, PlatformRole.__table__, PlatformRolePermission.__table__


@dataclass(frozen=True)
class ModuleSpec:
    module: str              # permission prefix, e.g. "recovery" -> "recovery.access"
    tenant_table: str
    member_table: str
    roles: tuple[str, ...]   # legacy role values ("member" when the table has no role column)
    extra: dict[str, frozenset[str]] | None = None


MODULES: dict[str, ModuleSpec] = {
    "company_graph": ModuleSpec("company_graph", "company_graph_tenants", "company_graph_members",
                                ("owner", "approver", "editor", "viewer"),
                                {"owner": frozenset({"connector.write", "connector.read",
                                                     # Step K v2 slice 6: the graph owner may
                                                     # execute approved twin proposals of either
                                                     # class (owner + coverage duties) and may
                                                     # approve coverage-class proposals too —
                                                     # the owner role is a superset of approver,
                                                     # the same way it already holds the
                                                     # approver's connector.read.
                                                     "twin.apply", "twin.apply.coverage",
                                                     "company_graph.role.approver"}),
                                 "editor": frozenset({"connector.write", "connector.read"}),
                                 "approver": frozenset({"connector.read",
                                                        # Step K v2 slice 6: a graph approver may
                                                        # execute approved coverage-class proposals.
                                                        "twin.apply.coverage"}),
                                 "viewer": frozenset({"connector.read"})}),
    "governance": ModuleSpec("governance", "security_tenants", "security_members", ("member",)),
    "continuity": ModuleSpec("continuity", "bc_tenants", "bc_members", ("owner", "approver", "planner", "executor", "viewer")),
    "process_intelligence": ModuleSpec("process_intelligence", "pi_tenants", "pi_memberships", ("viewer", "analyst", "steward")),
    "recovery": ModuleSpec("recovery", "recovery_tenants", "recovery_members", ("owner", "responder", "observer")),
}
BY_MEMBER_TABLE = {spec.member_table: spec for spec in MODULES.values()}

# Organisation-wide and group roles mirrored from users / access groups.
ORG_ROLES = {
    "org.member": frozenset({"org.access", "agent.run", "documents.read", "channel.reply"}),
    # Mirrors the legacy installation administrator: every department's workflow authority (legacy is_manager)
    # and the QA console. It holds no business duties (invoice.verify, payment.approve, funds.transfer).
    "org.admin": frozenset({"org.admin", "group.access", "workflow.access", "workflow.manage", "qa.manage"}),
}
GROUP_ROLES = {
    "group.member": frozenset({"group.access", "workflow.access"}),
    "group.manager": frozenset({"group.access", "workflow.access", "workflow.manage"}),
}


def role_key(module: str, role: str) -> str:
    return f"{module}.{role}"


def role_permissions(key: str) -> frozenset[str]:
    if key in ORG_ROLES:
        return ORG_ROLES[key]
    if key in GROUP_ROLES:
        return GROUP_ROLES[key]
    module, _, role = key.rpartition(".")
    spec = MODULES.get(module)
    if spec is None or role not in spec.roles:
        raise ValueError(f"Unknown platform role {key}")
    return frozenset({f"{module}.access", f"{module}.role.{role}"}) | (spec.extra or {}).get(role, frozenset())


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------------- scopes and roles

def ensure_role_sync(conn: Connection, key: str) -> uuid.UUID:
    conn.execute(insert(R).values(id=uuid.uuid4(), key=key, name=key, system=True).on_conflict_do_nothing(index_elements=["key"]))
    role_id = conn.scalar(select(R.c.id).where(R.c.key == key))
    for permission in role_permissions(key):
        conn.execute(insert(RP).values(role_id=role_id, permission=permission).on_conflict_do_nothing())
    return role_id


def ensure_org_sync(conn: Connection) -> uuid.UUID:
    conn.execute(insert(S).values(id=uuid.uuid4(), kind="org", parent_id=None, name="Organisation").on_conflict_do_nothing())
    return conn.scalar(select(S.c.id).where(S.c.kind == "org"))


def ensure_scope_sync(conn: Connection, *, legacy_table: str, legacy_id: uuid.UUID, kind: str, name: str,
                      parent_id: uuid.UUID | None = None, module: str | None = None) -> uuid.UUID:
    existing = conn.scalar(select(S.c.id).where(S.c.legacy_table == legacy_table, S.c.legacy_id == legacy_id))
    if existing:
        return existing
    parent_id = parent_id or ensure_org_sync(conn)
    conn.execute(insert(S).values(id=uuid.uuid4(), kind=kind, parent_id=parent_id, name=(name or legacy_table)[:200],
                                  module=module, legacy_table=legacy_table, legacy_id=legacy_id)
                 .on_conflict_do_nothing(constraint="uq_platform_scopes_legacy"))
    return conn.scalar(select(S.c.id).where(S.c.legacy_table == legacy_table, S.c.legacy_id == legacy_id))


def module_scope_sync(conn: Connection, module: str, tenant_id: uuid.UUID) -> uuid.UUID | None:
    spec = MODULES[module]
    from sqlalchemy import table, column
    tenant = table(spec.tenant_table, column("id"), column("name"))
    name = conn.scalar(select(tenant.c.name).where(tenant.c.id == tenant_id))
    if name is None:
        return None
    return ensure_scope_sync(conn, legacy_table=spec.tenant_table, legacy_id=tenant_id, kind="module_space",
                             name=name, module=module)


def group_scope_sync(conn: Connection, group_id: uuid.UUID, _depth: int = 0) -> uuid.UUID | None:
    from app.db.models import AccessGroup
    g = AccessGroup.__table__
    row = conn.execute(select(g.c.id, g.c.name, g.c.kind, g.c.parent_id).where(g.c.id == group_id)).first()
    if row is None:
        return None
    existing = conn.scalar(select(S.c.id).where(S.c.legacy_table == "access_groups", S.c.legacy_id == group_id))
    if existing:
        return existing
    parent = group_scope_sync(conn, row.parent_id, _depth + 1) if row.parent_id and _depth < 20 else None
    return ensure_scope_sync(conn, legacy_table="access_groups", legacy_id=group_id,
                             kind="department" if row.kind == "department" else "workspace", name=row.name, parent_id=parent)


# ------------------------------------------------------------------------------------------- memberships

def sync_membership(conn: Connection, *, scope_id: uuid.UUID, user_id: uuid.UUID, source: str,
                    role: str | None, valid_to: datetime | None = None, explicit: bool = False,
                    roles_of_source: tuple[str, ...] | None = None) -> str:
    """Make the platform mirror of one legacy grant match `role` (None = no longer granted).

    Returns 'unchanged' | 'granted' | 'changed' | 'revoked' | 'blocked'."""
    now = _now()
    active = conn.execute(select(M.c.id, M.c.role_id, M.c.valid_to, R.c.key).join(R, R.c.id == M.c.role_id).where(
        M.c.scope_id == scope_id, M.c.user_id == user_id, M.c.source == source, M.c.revoked_at.is_(None))).all()
    if role is None:
        if active:
            conn.execute(update(M).where(M.c.id.in_([r.id for r in active])).values(revoked_at=now, revoke_reason="legacy:removed"))
            return "revoked"
        return "unchanged"
    if len(active) == 1 and active[0].key == role and active[0].valid_to == valid_to:
        return "unchanged"
    if not active and not explicit:
        blocked = conn.scalar(select(M.c.id).where(
            M.c.scope_id == scope_id, M.c.user_id == user_id, M.c.source == source, M.c.revoked_at.is_not(None),
            M.c.revoke_reason.like("platform:%")).limit(1))
        if blocked:
            return "blocked"
    if active:
        conn.execute(update(M).where(M.c.id.in_([r.id for r in active])).values(revoked_at=now, revoke_reason="legacy:changed"))
    role_id = ensure_role_sync(conn, role)
    conn.execute(insert(M).values(id=uuid.uuid4(), scope_id=scope_id, user_id=user_id, role_id=role_id, source=source,
                                  valid_to=valid_to).on_conflict_do_nothing())
    return "changed" if active else "granted"


def _member_role(spec: ModuleSpec, row) -> str | None:
    mapping = row._mapping
    if "active" in mapping and not mapping["active"]:
        return None
    role = mapping.get("role") or "member"
    return role_key(spec.module, role) if role in spec.roles else None


def reconcile_module_member(conn: Connection, module: str, tenant_id: uuid.UUID, user_id: uuid.UUID, *,
                            explicit: bool = False) -> tuple[uuid.UUID | None, str]:
    spec = MODULES[module]
    scope_id = module_scope_sync(conn, module, tenant_id)
    if scope_id is None:
        return None, "no_scope"
    member = _member_table(spec)
    row = conn.execute(select(member).where(member.c.tenant_id == tenant_id, member.c.user_id == user_id)).first()
    role = _member_role(spec, row) if row is not None else None
    valid_to = row._mapping.get("expires_at") if row is not None else None
    return scope_id, sync_membership(conn, scope_id=scope_id, user_id=user_id, source=f"legacy:{spec.member_table}",
                                     role=role, valid_to=valid_to, explicit=explicit)


def _member_table(spec: ModuleSpec):
    from app.db.base import Base
    return Base.metadata.tables[spec.member_table]


def reconcile_group_member(conn: Connection, group_id: uuid.UUID, user_id: uuid.UUID, *, explicit: bool = False) -> tuple[uuid.UUID | None, str]:
    from app.db.models import AccessGroupMember, DepartmentManager
    scope_id = group_scope_sync(conn, group_id)
    if scope_id is None:
        return None, "no_scope"
    gm, dm = AccessGroupMember.__table__, DepartmentManager.__table__
    member = conn.scalar(select(gm.c.user_id).where(gm.c.group_id == group_id, gm.c.user_id == user_id))
    manager = conn.scalar(select(dm.c.manager_user_id).where(dm.c.group_id == group_id, dm.c.manager_user_id == user_id))
    a = sync_membership(conn, scope_id=scope_id, user_id=user_id, source="legacy:access_group_members",
                        role="group.member" if member else None, explicit=explicit)
    b = sync_membership(conn, scope_id=scope_id, user_id=user_id, source="legacy:department_managers",
                        role="group.manager" if manager else None, explicit=explicit)
    return scope_id, b if a == "unchanged" else a


def reconcile_user(conn: Connection, user_id: uuid.UUID, *, explicit: bool = False) -> tuple[uuid.UUID, str]:
    from app.db.models import User
    u = User.__table__
    row = conn.execute(select(u.c.is_active, u.c.role).where(u.c.id == user_id)).first()
    org = ensure_org_sync(conn)
    active = bool(row and row.is_active)
    a = sync_membership(conn, scope_id=org, user_id=user_id, source="legacy:users",
                        role="org.member" if active else None, explicit=explicit)
    b = sync_membership(conn, scope_id=org, user_id=user_id, source="legacy:users.admin",
                        role="org.admin" if active and row.role == "admin" else None, explicit=explicit)
    return org, a if a != "unchanged" else b


def backfill_all(conn: Connection) -> dict[str, int]:
    """Idempotent full mirror of users, groups and the five module tenant/member tables."""
    from sqlalchemy import table, column
    from app.db.models import AccessGroup, AccessGroupMember, DepartmentManager, User
    counts: dict[str, int] = {}
    ensure_org_sync(conn)
    for key in (*ORG_ROLES, *GROUP_ROLES):
        ensure_role_sync(conn, key)
    for (user_id,) in conn.execute(select(User.__table__.c.id)).all():
        counts["users"] = counts.get("users", 0) + (reconcile_user(conn, user_id)[1] != "unchanged")
    for (group_id,) in conn.execute(select(AccessGroup.__table__.c.id)).all():
        group_scope_sync(conn, group_id)
    pairs = {(r.group_id, r.user_id) for r in conn.execute(select(AccessGroupMember.__table__.c.group_id, AccessGroupMember.__table__.c.user_id))}
    pairs |= {(r.group_id, r.manager_user_id) for r in conn.execute(select(DepartmentManager.__table__.c.group_id, DepartmentManager.__table__.c.manager_user_id))}
    for group_id, user_id in pairs:
        counts["groups"] = counts.get("groups", 0) + (reconcile_group_member(conn, group_id, user_id)[1] != "unchanged")
    for module, spec in MODULES.items():
        tenants = table(spec.tenant_table, column("id"))
        for (tenant_id,) in conn.execute(select(tenants.c.id)).all():
            module_scope_sync(conn, module, tenant_id)
        member = _member_table(spec)
        for row in conn.execute(select(member.c.tenant_id, member.c.user_id)).all():
            counts[module] = counts.get(module, 0) + (reconcile_module_member(conn, module, row.tenant_id, row.user_id)[1] != "unchanged")
    return counts


# ------------------------------------------------------------------------------------------- ORM mirroring

def _watched(obj) -> tuple[str, tuple] | None:
    table_name = getattr(getattr(obj, "__table__", None), "name", None)
    if table_name in BY_MEMBER_TABLE:
        return "module", (BY_MEMBER_TABLE[table_name].module, obj.tenant_id, obj.user_id)
    if table_name == "access_group_members":
        return "group", (obj.group_id, obj.user_id)
    if table_name == "department_managers":
        return "group", (obj.group_id, obj.manager_user_id)
    if table_name == "users":
        state = inspect(obj)
        if obj in state.session.new or any(state.attrs[name].history.has_changes() for name in ("is_active", "role")):
            return "user", (obj.id,)
    return None


def _after_flush(session: Session, _context) -> None:
    pending = session.info.setdefault("platform_mirror", set())
    for obj in (*session.new, *session.dirty, *session.deleted):
        try:
            watched = _watched(obj)
        except Exception:  # unloaded attributes on a detached object: the access-time fallback covers it
            continue
        if watched and all(value is not None for value in watched[1]):
            pending.add(watched)
    if not pending:
        return
    session.info["platform_mirror"] = set()
    conn = session.connection()
    for kind, args in pending:
        if kind == "module":
            reconcile_module_member(conn, *args, explicit=True)
        elif kind == "group":
            reconcile_group_member(conn, *args, explicit=True)
        else:
            # Reactivating a login is not a membership grant: platform-revoked org roles stay revoked
            # until they are granted again deliberately (grant_membership or a module/group change).
            reconcile_user(conn, *args, explicit=False)


_installed = False


def install_orm_mirroring() -> None:
    global _installed
    if not _installed:
        event.listen(Session, "after_flush", _after_flush)
        _installed = True
