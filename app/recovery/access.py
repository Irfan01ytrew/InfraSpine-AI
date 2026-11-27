from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models import User
from app.platform.modules import module_gate
from app.recovery.models import Member


@dataclass(frozen=True)
class Scope:
    tenant_id: UUID
    user_id: UUID
    role: str

    def require_write(self):
        if self.role not in {"owner", "responder"}:
            raise PermissionDeniedError("Recovery responder permission is required.")

    def require_owner(self):
        if self.role != "owner":
            raise PermissionDeniedError("Recovery tenant owner permission is required.")


async def scope_for(db, tenant_id, user_id):
    row = await db.scalar(select(Member).join(User, User.id == Member.user_id).where(
        Member.tenant_id == tenant_id, Member.user_id == user_id, Member.active.is_(True), User.is_active.is_(True)))
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("Recovery workspace not found.")
    gate = await module_gate(db, "recovery", tenant_id, user, legacy_allowed=row is not None)
    if row is None or not gate.allowed:
        raise NotFoundError("Recovery workspace not found.")
    return Scope(tenant_id, user_id, row.role)


def scoped(model, scope):
    conditions = [model.tenant_id == scope.tenant_id]
    if hasattr(model, "owner_id") and scope.role != "owner":
        conditions.append(model.owner_id == scope.user_id)
    return conditions


async def get_scoped(db, model, resource_id, scope, *, lock=False):
    stmt = select(model).where(model.id == resource_id, *scoped(model, scope))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = await db.scalar(stmt)
    if row is None:
        raise NotFoundError("Recovery resource not found.")
    return row
