"""Deny by default, including for platform admins without a continuity membership."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.continuity.models import Member, Node, Tenant
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models import User


@dataclass(frozen=True)
class Scope:
    tenant_id: UUID
    user_id: UUID
    role: str
    departments: frozenset[str]
    all_departments: bool

    def require(self, *roles):
        if self.role not in roles:
            raise PermissionDeniedError("Your continuity role does not authorize this action.")

    def permits(self, department_id):
        return self.all_departments or (department_id is not None and str(department_id) in self.departments)


async def scope_for(db: AsyncSession, tenant_id: UUID, user: User, *, write=False):
    # Serialize a tenant's writes, decisions and dispatches. Read membership AFTER this lock.
    tenant = await db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update() if write else select(Tenant).where(Tenant.id == tenant_id))
    member = await db.scalar(select(Member).where(Member.tenant_id == tenant_id, Member.user_id == user.id, Member.active.is_(True)).execution_options(populate_existing=True))
    active = await db.scalar(select(User.is_active).where(User.id == user.id))
    if tenant is None or member is None or not active:
        raise NotFoundError("Continuity workspace not found.")
    return Scope(tenant_id, user.id, member.role, frozenset(member.departments), member.all_departments)


async def node_for(db, scope, node_id):
    node = await db.scalar(select(Node).where(Node.tenant_id == scope.tenant_id, Node.id == node_id))
    if node is None or not scope.permits(node.department_id):
        raise NotFoundError("Continuity resource not found.")
    return node


async def nodes_for(db, scope):
    query = select(Node).where(Node.tenant_id == scope.tenant_id)
    if not scope.all_departments:
        query = query.where(Node.department_id.in_([UUID(d) for d in scope.departments]))
    return list(await db.scalars(query.order_by(Node.name, Node.id)))
