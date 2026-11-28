"""Live authorization shared by API, background jobs and integration adapters."""
from uuid import UUID

from sqlalchemy import exists, or_, select

from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models import Document, User
from app.rag.access import readable_by
from .models import Membership, Process, Source, SourceGrant

ROLES = {"viewer": 0, "analyst": 1, "steward": 2}


async def membership(db, user: User, tenant_id: UUID, minimum="viewer") -> Membership:
    if not user.is_active:
        raise PermissionDeniedError()
    member = await db.get(Membership, (tenant_id, user.id))
    if member is None:
        raise NotFoundError()
    if ROLES[member.role] < ROLES[minimum]:
        raise PermissionDeniedError()
    return member


def source_scope(user, tenant_id):
    return (
        Source.tenant_id == tenant_id,
        Source.active.is_(True),
        or_(Source.owner_id == user.id, exists().where(SourceGrant.tenant_id == tenant_id, SourceGrant.source_id == Source.id, SourceGrant.user_id == user.id)),
        or_(Source.document_id.is_(None), Source.document_id.in_(select(Document.id).where(readable_by(user)))),
    )


async def sources(db, user, tenant_id, process_id=None):
    await membership(db, user, tenant_id)
    query = select(Source).where(*source_scope(user, tenant_id))
    if process_id:
        query = query.where(Source.process_id == process_id)
    return list((await db.scalars(query.order_by(Source.created_at, Source.id))).all())


async def source(db, user, tenant_id, process_id, source_id, *, write=False):
    await membership(db, user, tenant_id, "analyst" if write else "viewer")
    row = await db.scalar(select(Source).where(*source_scope(user, tenant_id), Source.id == source_id, Source.process_id == process_id))
    if row is None:
        raise NotFoundError()
    if write and row.owner_id != user.id:
        raise PermissionDeniedError("Only the source owner may synchronize or change this source.")
    return row


async def process(db, user, tenant_id, process_id, *, write=False, lock=False):
    await membership(db, user, tenant_id, "analyst" if write else "viewer")
    query = select(Process).where(Process.tenant_id == tenant_id, Process.id == process_id)
    if lock:
        query = query.with_for_update()
    row = await db.scalar(query)
    if row is None:
        raise NotFoundError()
    if row.owner_id != user.id and not await db.scalar(select(Source.id).where(*source_scope(user, tenant_id), Source.process_id == process_id).limit(1)):
        raise NotFoundError()
    return row
