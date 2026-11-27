"""Governed writes and authorized graph projections. Callers commit atomically."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.company_graph.access import Scope, get_record, payload_visible, references_visible, scope_for, visible
from app.company_graph.models import GraphChange, GraphEvent, GraphMember, GraphRecord, GraphTenant
from app.company_graph.schemas import MemberInput, RecordInput, RecordProposal
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.models import User
from app.services import audit
from app.services.audit import AuditContext

ENTITY_TYPES = ("organization", "business_unit", "department", "employee", "position", "role", "responsibility", "process", "workflow", "system", "asset", "project", "supplier", "customer", "approval", "dependency")
RELATION_TYPES = ("part_of", "reports_to", "holds_position", "has_role", "owns", "responsible_for", "depends_on", "operates", "supplies", "backup_for", "delegates_to", "approves")


def public_record(row: GraphRecord):
    data = {name: getattr(row, name) for name in RecordInput.model_fields}
    data = RecordInput.model_construct(**data).model_dump(mode="json")
    return {"id": str(row.id), **data, "version": row.version, "validation_status": "validated", "updated_at": row.updated_at.isoformat()}


def public_change(row: GraphChange):
    return {"id": str(row.id), "change_type": row.change_type, "subject_id": str(row.subject_id),
            "payload": row.payload, "base_version": row.base_version, "status": row.status,
            "requested_by": str(row.requested_by), "expires_at": row.expires_at.isoformat(),
            "reason": row.reason, "decided_by": str(row.decided_by) if row.decided_by else None}


async def lock_tenant(db: AsyncSession, scope: Scope):
    return await db.scalar(select(GraphTenant).where(GraphTenant.id == scope.tenant_id).with_for_update().execution_options(populate_existing=True))


async def create_tenant(db: AsyncSession, user: User, name: str, ctx: AuditContext):
    if user.role != "admin":
        raise PermissionDeniedError("Only an administrator can create a company graph.")
    row = GraphTenant(name=name)
    db.add(row)
    await db.flush()
    db.add(GraphMember(tenant_id=row.id, user_id=user.id, role="owner", all_departments=True))
    audit.record(db, ctx, "company_graph.created", target=str(row.id))
    return {"id": str(row.id), "name": row.name, "role": "owner"}


async def list_tenants(db: AsyncSession, user: User):
    rows = (await db.execute(select(GraphTenant, GraphMember.role).join(GraphMember).where(
        GraphMember.user_id == user.id, GraphMember.active.is_(True),
    ).order_by(GraphTenant.name))).all()
    return [{"id": str(t.id), "name": t.name, "role": role} for t, role in rows]


async def validate_record(db: AsyncSession, scope: Scope, data: RecordInput, old: GraphRecord | None):
    payload = data.model_dump(mode="json")
    if not payload_visible(scope, payload):
        raise PermissionDeniedError("You must be able to inspect the proposed record.")
    await references_visible(db, scope, payload)
    if old:
        for name in ("record_type", "external_key", "kind", "source_id", "target_id"):
            if getattr(old, name) != getattr(data, name):
                raise InvalidInputError("Identity and relationship endpoints are immutable. Retire and replace the record.")
    if data.department_id and old and data.department_id == old.id:
        raise InvalidInputError("An entity cannot be its own department.")
    if data.readers:
        readers = set((await db.scalars(select(GraphMember.user_id).where(
            GraphMember.tenant_id == scope.tenant_id, GraphMember.active.is_(True), GraphMember.user_id.in_(data.readers),
        ))).all())
        if readers != set(data.readers):
            raise InvalidInputError("Readers must be active company graph members.")
    if data.record_type == "relationship":
        source = await get_record(db, scope, data.source_id)
        target = await get_record(db, scope, data.target_id)
        if data.kind in ("owns", "responsible_for", "backup_for", "delegates_to") and source.kind not in ("employee", "role", "position", "department"):
            raise InvalidInputError("Ownership and delegation require an employee, role, position or department source.")
        if data.kind == "backup_for" and source.kind != "employee":
            raise InvalidInputError("A backup must identify an authorized employee.")
        if data.kind == "delegates_to" and target.kind != "employee":
            raise InvalidInputError("Delegation must identify the receiving employee.")


async def propose_record(db: AsyncSession, scope: Scope, payload: RecordProposal, ctx: AuditContext):
    scope.require("owner", "approver", "editor")
    await lock_tenant(db, scope)
    old = await get_record(db, scope, payload.record_id) if payload.record_id else None
    if (old.version if old else 0) != payload.base_version:
        raise ConflictError()
    await validate_record(db, scope, payload.record, old)
    if not old and await db.scalar(select(GraphRecord.id).where(GraphRecord.tenant_id == scope.tenant_id, GraphRecord.external_key == payload.record.external_key)):
        # Do not reveal the existing record's ID, name or visibility.
        raise ConflictError("This external key cannot be used. Refresh or choose a new key.")
    row = GraphChange(tenant_id=scope.tenant_id, change_type="record", subject_id=old.id if old else uuid.uuid4(),
                      payload=payload.record.model_dump(mode="json"), base_version=payload.base_version,
                      requested_by=scope.user_id, expires_at=datetime.now(UTC) + timedelta(hours=24))
    db.add(row)
    await db.flush()
    audit.record(db, ctx, "company_graph.proposed", target=str(row.id), details={"tenant_id": str(scope.tenant_id), "type": "record"})
    return row


async def propose_member(db: AsyncSession, scope: Scope, data: MemberInput, ctx: AuditContext):
    scope.require("owner")
    await lock_tenant(db, scope)
    if await db.scalar(select(User.id).where(User.id == data.user_id, User.is_active.is_(True))) is None:
        raise NotFoundError("Active user not found.")
    for department in data.departments:
        row = await get_record(db, scope, department)
        if row.kind != "department" or row.record_type != "entity" or row.retired:
            raise InvalidInputError("Membership departments must be active department entities.")
    old = await db.get(GraphMember, (scope.tenant_id, data.user_id))
    if (old.version if old else 0) != data.base_version:
        raise ConflictError()
    row = GraphChange(tenant_id=scope.tenant_id, change_type="member", subject_id=data.user_id,
                      payload=data.model_dump(mode="json"), base_version=data.base_version,
                      requested_by=scope.user_id, expires_at=datetime.now(UTC) + timedelta(hours=1))
    db.add(row)
    await db.flush()
    audit.record(db, ctx, "company_graph.membership_proposed", target=str(row.id), details={"tenant_id": str(scope.tenant_id)})
    return row


async def change_visible(db: AsyncSession, scope: Scope, change: GraphChange):
    if change.change_type == "member":
        return scope.role == "owner"
    if not payload_visible(scope, change.payload):
        return False
    try:
        await references_visible(db, scope, change.payload)
        if change.base_version:
            await get_record(db, scope, change.subject_id)
    except (NotFoundError, PermissionDeniedError):
        return False
    return True


async def list_changes(db: AsyncSession, scope: Scope, *, after: uuid.UUID | None = None, limit=50):
    scope.require("owner", "approver", "editor")
    query = select(GraphChange).where(GraphChange.tenant_id == scope.tenant_id, GraphChange.status == "pending").order_by(GraphChange.id)
    if after:
        query = query.where(GraphChange.id > after)
    # Iterate in fixed batches; only authorized IDs and payloads are returned.
    result = []
    async for change in await db.stream_scalars(query.execution_options(yield_per=100)):
        if await change_visible(db, scope, change):
            result.append(public_change(change))
            if len(result) > limit:
                break
    return {"items": result[:limit], "next_cursor": result[limit-1]["id"] if len(result) > limit else None}


async def decide(db: AsyncSession, scope: Scope, change_id: uuid.UUID, approve: bool, reason: str, ctx: AuditContext):
    scope.require("owner", "approver")
    tenant = await lock_tenant(db, scope)
    # Re-read after the tenant lock: a membership revocation must fence waiting requests.
    user = await db.get(User, scope.user_id)
    scope = await scope_for(db, scope.tenant_id, user)
    scope.require("owner", "approver")
    change = await db.scalar(select(GraphChange).where(GraphChange.id == change_id, GraphChange.tenant_id == scope.tenant_id).with_for_update())
    if change is None or not await change_visible(db, scope, change):
        raise NotFoundError("Graph change not found.")
    if change.status != "pending" or change.expires_at <= datetime.now(UTC):
        raise ConflictError("This change is already decided or expired. Submit a new proposal.")
    snapshot = None
    if approve and change.change_type == "record":
        old = await get_record(db, scope, change.subject_id) if change.base_version else None
        if (old.version if old else 0) != change.base_version:
            raise ConflictError("The record changed after this proposal. Review a fresh proposal.")
        data = RecordInput.model_validate(change.payload)
        await validate_record(db, scope, data, old)
        if old and any(getattr(old, f) != getattr(data, f) for f in ("visibility", "readers", "department_id")):
            scope.require("owner")
        if not old:
            duplicate = await db.scalar(select(GraphRecord.id).where(GraphRecord.tenant_id == scope.tenant_id, GraphRecord.external_key == data.external_key))
            if duplicate:
                raise ConflictError("An approved record now uses this external key.")
            old = GraphRecord(id=change.subject_id, tenant_id=scope.tenant_id, version=0)
            db.add(old)
        for field, value in data.model_dump().items():
            if field in ("readers", "evidence"):
                value = data.model_dump(mode="json")[field]
            setattr(old, field, value)
        old.version += 1
        old.updated_at = datetime.now(UTC)
        await db.flush()
        snapshot = public_record(old)
    elif approve:
        scope.require("owner")
        data = MemberInput.model_validate(change.payload)
        member = await db.get(GraphMember, (scope.tenant_id, data.user_id))
        if (member.version if member else 0) != change.base_version:
            raise ConflictError()
        if await db.scalar(select(User.id).where(User.id == data.user_id, User.is_active.is_(True))) is None:
            raise NotFoundError("Active user not found.")
        for dept in data.departments:
            department = await get_record(db, scope, dept)
            if department.kind != "department" or department.retired:
                raise ConflictError("A membership department is no longer active.")
        if member and member.role == "owner" and (data.role != "owner" or not data.active):
            owners = await db.scalar(select(func.count()).select_from(GraphMember).join(User, User.id == GraphMember.user_id).where(
                GraphMember.tenant_id == scope.tenant_id, GraphMember.role == "owner", GraphMember.active.is_(True), User.is_active.is_(True)))
            if owners <= 1:
                raise ConflictError("The last active owner cannot be removed.")
        if not member:
            member = GraphMember(tenant_id=scope.tenant_id, user_id=data.user_id, version=0)
            db.add(member)
        for key in ("role", "departments", "all_departments", "active"):
            setattr(member, key, data.model_dump(mode="json")[key])
        member.version += 1
        snapshot = {**data.model_dump(mode="json"), "version": member.version}
    change.status = "approved" if approve else "rejected"
    change.decided_by, change.decided_at, change.reason = scope.user_id, datetime.now(UTC), reason
    if snapshot:
        tenant.revision += 1
        db.add(GraphEvent(tenant_id=scope.tenant_id, sequence=tenant.revision, subject_id=change.subject_id,
                          event_type=f"{change.change_type}.changed", snapshot=snapshot, actor_id=scope.user_id))
    audit.record(db, ctx, "company_graph." + change.status, target=str(change.id), details={"tenant_id": str(scope.tenant_id), "type": change.change_type})
    await db.flush()
    return public_change(change)


async def projection(db: AsyncSession, scope: Scope, *, search="", kind="", department_id=None, after=None, limit=500, moment=None):
    moment = moment or datetime.now(UTC)
    query = select(GraphRecord).where(visible(scope, moment=moment), GraphRecord.record_type == "entity")
    if search:
        query = query.where(GraphRecord.name.ilike("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"))
    if kind:
        query = query.where(GraphRecord.kind == kind)
    if department_id:
        query = query.where(GraphRecord.department_id == department_id)
    if after:
        query = query.where(GraphRecord.id > after)
    nodes = list((await db.scalars(query.order_by(GraphRecord.id).limit(limit + 1))).all())
    truncated = len(nodes) > limit
    nodes = nodes[:limit]
    ids = [r.id for r in nodes]
    edges = list((await db.scalars(select(GraphRecord).where(visible(scope, moment=moment),
        GraphRecord.record_type == "relationship", GraphRecord.source_id.in_(ids), GraphRecord.target_id.in_(ids),
    ).order_by(GraphRecord.id).limit(5001))).all())
    records = [public_record(r) for r in nodes + edges[:5000]]
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return {"entities": records[:len(nodes)], "relationships": records[len(nodes):], "as_of": moment.isoformat(),
            "next_cursor": str(nodes[-1].id) if truncated else None, "truncated": truncated or len(edges) > 5000,
            "fingerprint": fingerprint, "scope": "Authorized records only; hidden relationships are excluded."}


async def neighborhood(db: AsyncSession, scope: Scope, root: uuid.UUID, depth=1, moment=None):
    moment = moment or datetime.now(UTC)
    entity = await get_record(db, scope, root, moment=moment)
    if entity.record_type != "entity":
        raise InvalidInputError("Traversal requires an entity.")
    nodes, edges, frontier, truncated = {root: entity}, {}, {root}, False
    for _ in range(depth):
        if not frontier:
            break
        rows = list((await db.scalars(select(GraphRecord).where(visible(scope, moment=moment),
            GraphRecord.record_type == "relationship", (GraphRecord.source_id.in_(frontier) | GraphRecord.target_id.in_(frontier)),
        ).order_by(GraphRecord.id).limit(5001))).all())
        if len(rows) > 5000:
            truncated = True
        candidates = {v for r in rows[:5000] for v in (r.source_id, r.target_id)} - nodes.keys()
        room = max(0, 1000 - len(nodes))
        if len(candidates) > room:
            truncated = True
        frontier = set(sorted(candidates, key=str)[:room])
        if frontier:
            new = (await db.scalars(select(GraphRecord).where(visible(scope, moment=moment), GraphRecord.id.in_(frontier)))).all()
            nodes.update({r.id: r for r in new})
        for edge in rows[:5000]:
            if edge.source_id in nodes and edge.target_id in nodes:
                edges[edge.id] = edge
        if len(edges) > 5000:
            edges = dict(list(edges.items())[:5000])
            truncated = True
        if truncated:
            break
    return {"entities": [public_record(r) for r in nodes.values()], "relationships": [public_record(r) for r in edges.values()],
            "truncated": truncated, "as_of": moment.isoformat(), "scope": "Authorized neighborhood only."}


async def events(db: AsyncSession, scope: Scope, *, after=None, subject_id=None, limit=100):
    sequence = 0
    if after:
        previous = await db.scalar(select(GraphEvent).where(GraphEvent.tenant_id == scope.tenant_id, GraphEvent.id == after))
        if not previous or not await event_visible(db, scope, previous):
            raise NotFoundError("Event cursor unavailable; resynchronize your authorized snapshot.")
        sequence = previous.sequence
    query = select(GraphEvent).where(GraphEvent.tenant_id == scope.tenant_id, GraphEvent.sequence > sequence).order_by(GraphEvent.sequence)
    if subject_id:
        await get_record(db, scope, subject_id)
        query = query.where(GraphEvent.subject_id == subject_id)
    result = []
    async for row in await db.stream_scalars(query.execution_options(yield_per=100)):
        if await event_visible(db, scope, row):
            result.append({"id": str(row.id), "type": row.event_type, "subject_id": str(row.subject_id),
                           "record": row.snapshot, "occurred_at": row.created_at.isoformat(), "actor_id": str(row.actor_id)})
            if len(result) > limit:
                break
    return {"contract_version": 1, "events": result[:limit], "has_more": len(result) > limit,
            "next_cursor": result[min(len(result), limit)-1]["id"] if result else (str(after) if after else None),
            "resync_required_on_access_change": True}


async def event_visible(db: AsyncSession, scope: Scope, event: GraphEvent):
    if event.event_type == "member.changed":
        return scope.role == "owner"
    if not payload_visible(scope, event.snapshot):
        return False
    try:
        await get_record(db, scope, event.subject_id)
        await references_visible(db, scope, event.snapshot)
    except (NotFoundError, PermissionDeniedError):
        return False
    return True
