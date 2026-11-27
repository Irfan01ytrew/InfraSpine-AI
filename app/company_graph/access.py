"""One authorization policy for maps, traversal, exports, history and consumers."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, exists, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.company_graph.models import GraphMember, GraphRecord
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models import User
from app.platform.modules import module_gate


@dataclass(frozen=True)
class Scope:
    tenant_id: UUID
    user_id: UUID
    role: str
    departments: tuple[str, ...]
    all_departments: bool

    def require(self, *roles):
        if self.role not in roles:
            raise PermissionDeniedError("Your company graph role does not allow this operation.")


async def scope_for(db: AsyncSession, tenant_id: UUID, user: User) -> Scope:
    member = await db.scalar(select(GraphMember).join(User, User.id == GraphMember.user_id).where(
        GraphMember.tenant_id == tenant_id, GraphMember.user_id == user.id,
        GraphMember.active.is_(True), User.is_active.is_(True),
    ).execution_options(populate_existing=True))
    gate = await module_gate(db, "company_graph", tenant_id, user, legacy_allowed=member is not None)
    if not gate.allowed:
        raise NotFoundError("Company graph not found.")
    return Scope(tenant_id, user.id, member.role, tuple(member.departments), member.all_departments)


def own_visibility(scope: Scope, row=GraphRecord):
    if scope.role == "owner":
        return row.tenant_id == scope.tenant_id
    department = true() if scope.all_departments else row.department_id.in_([UUID(x) for x in scope.departments])
    return and_(row.tenant_id == scope.tenant_id, or_(
        row.visibility == "tenant",
        and_(row.visibility == "department", department),
        and_(row.visibility == "restricted", row.readers.contains([str(scope.user_id)])),
    ))


def active_at(row, moment: datetime):
    return and_(row.retired.is_(False), row.valid_from <= moment, or_(row.valid_to.is_(None), row.valid_to > moment))


def visible(scope: Scope, *, moment: datetime | None = None):
    source, target, department = aliased(GraphRecord), aliased(GraphRecord), aliased(GraphRecord)
    def endpoint(row, field):
        conditions = [row.id == field, row.record_type == "entity", own_visibility(scope, row)]
        if moment:
            conditions.append(active_at(row, moment))
        return exists(select(row.id).where(*conditions))
    # Department IDs are sensitive references too; don't expose an inaccessible department ID.
    dept_visible = or_(GraphRecord.department_id.is_(None), endpoint(department, GraphRecord.department_id))
    conditions = [own_visibility(scope), dept_visible, or_(
        GraphRecord.record_type == "entity",
        and_(endpoint(source, GraphRecord.source_id), endpoint(target, GraphRecord.target_id)),
    )]
    if moment:
        conditions.append(active_at(GraphRecord, moment))
    return and_(*conditions)


def payload_visible(scope: Scope, payload: dict) -> bool:
    if scope.role == "owner":
        return True
    if payload.get("visibility") == "tenant":
        return True
    if payload.get("visibility") == "department":
        return scope.all_departments or payload.get("department_id") in scope.departments
    return str(scope.user_id) in payload.get("readers", [])


async def get_record(db: AsyncSession, scope: Scope, record_id: UUID, *, moment: datetime | None = None):
    row = await db.scalar(select(GraphRecord).where(GraphRecord.id == record_id, visible(scope, moment=moment)))
    if row is None:
        raise NotFoundError("Graph record not found.")
    return row


async def references_visible(db: AsyncSession, scope: Scope, payload: dict):
    for field in ("source_id", "target_id", "department_id"):
        if payload.get(field):
            row = await get_record(db, scope, UUID(str(payload[field])))
            if row.record_type != "entity" or row.retired:
                raise NotFoundError("Graph entity not found.")
            if field == "department_id" and row.kind != "department":
                raise PermissionDeniedError("The department reference must identify a department entity.")
