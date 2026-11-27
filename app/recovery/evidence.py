"""Encrypted, authenticated hash chain; incident row must be locked before append."""
import json
import uuid

from sqlalchemy import select

from app.core.crypto import EncryptedValue
from app.core.errors import ConflictError
from app.recovery.models import Timeline
from app.recovery.policy import bounded_evidence, canonical, digest, now
from app.services import audit


def aad(row):
    return f"recovery:v1:{row.tenant_id}:{row.incident_id}:{row.id}:{row.sequence}".encode()


async def append(db, cipher, ctx, incident, kind, payload):
    row = Timeline(id=uuid.uuid4(), tenant_id=incident.tenant_id, incident_id=incident.id,
                   sequence=incident.audit_seq + 1, kind=kind, actor_id=ctx.actor_user_id,
                   created_at=now(), previous_hash=incident.audit_head)
    body = {"data": bounded_evidence(payload), "kind": kind, "actor": str(row.actor_id),
            "at": row.created_at.isoformat(), "previous": row.previous_hash, "sequence": row.sequence}
    row.digest = digest(body)
    value = cipher.encrypt(canonical(body), associated_data=aad(row))
    row.ciphertext, row.nonce, row.key_id = value.ciphertext, value.nonce, value.key_id
    incident.audit_head, incident.audit_seq = row.digest, row.sequence
    incident.version += 1
    db.add(row)
    audit.record(db, ctx, f"recovery.{kind}", target=str(incident.id), details={"tenant": str(incident.tenant_id), "sequence": row.sequence, "digest": row.digest})
    await db.flush()


async def read(db, cipher, incident):
    rows = list(await db.scalars(select(Timeline).where(Timeline.tenant_id == incident.tenant_id,
                    Timeline.incident_id == incident.id).order_by(Timeline.sequence)))
    previous, result = "0" * 64, []
    for seq, row in enumerate(rows, 1):
        try:
            body = json.loads(cipher.decrypt(EncryptedValue(row.ciphertext, row.nonce, row.key_id), associated_data=aad(row)))
        except Exception:
            raise ConflictError("Incident evidence failed authentication.") from None
        if (row.sequence != seq or previous != row.previous_hash or digest(body) != row.digest
                or body["previous"] != previous or body["sequence"] != seq or body["kind"] != row.kind
                or body["actor"] != str(row.actor_id) or body["at"] != row.created_at.isoformat()):
            raise ConflictError("Incident audit chain verification failed.")
        previous = row.digest
        result.append({"sequence": seq, "kind": row.kind, "at": row.created_at.isoformat(), "actor": str(row.actor_id), "data": body["data"], "digest": row.digest})
    if previous != incident.audit_head or len(rows) != incident.audit_seq:
        raise ConflictError("Incident audit chain is incomplete.")
    return result
