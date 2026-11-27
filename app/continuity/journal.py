"""Atomic continuity events plus the existing central audit trail."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select

from app.continuity.models import Event, Tenant
from app.services import audit


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()).hexdigest()


def event_hash(tenant_id, sequence, actor_id, action, subject_id, data, previous_hash, created_at):
    return digest([str(tenant_id), sequence, str(actor_id), action, str(subject_id), data, previous_hash, created_at.isoformat()])


async def record(db, scope, ctx, action, subject_id, data=None):
    tenant = await db.scalar(select(Tenant).where(Tenant.id == scope.tenant_id).with_for_update().execution_options(populate_existing=True))
    now = datetime.now(UTC)
    sequence = tenant.revision + 1
    checksum = event_hash(tenant.id, sequence, scope.user_id, action, subject_id, data or {}, tenant.event_head, now)
    db.add(Event(tenant_id=tenant.id, sequence=sequence, actor_id=scope.user_id, action=action,
                 subject_id=subject_id, data=data or {}, previous_hash=tenant.event_head, digest=checksum, created_at=now))
    tenant.revision, tenant.event_head = sequence, checksum
    audit.record(db, ctx, "continuity." + action, target=str(subject_id), details={"tenant_id": str(tenant.id), "sequence": sequence, "digest": checksum})
    await db.flush()


async def verify(db, tenant_id):
    tenant = await db.get(Tenant, tenant_id)
    events = list(await db.scalars(select(Event).where(Event.tenant_id == tenant_id).order_by(Event.sequence)))
    previous = "0" * 64
    for expected, event in enumerate(events, 1):
        checksum = event_hash(event.tenant_id, event.sequence, event.actor_id, event.action, event.subject_id, event.data, event.previous_hash, event.created_at)
        if event.sequence != expected or event.previous_hash != previous or checksum != event.digest:
            return {"valid": False, "event_count": len(events)}
        previous = event.digest
    return {"valid": previous == tenant.event_head and len(events) == tenant.revision, "event_count": len(events), "head": previous}
