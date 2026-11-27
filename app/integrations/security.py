"""Shared scope, encryption and non-executable mapping helpers."""
import base64
import hashlib
import json
from uuid import UUID

from sqlalchemy import select

from app.company_graph.models import GraphMember
from app.core.crypto import EncryptedValue
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models import User
from app.platform.modules import module_gate


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def aad(kind, row):
    return f"infraspine:integrations:v1:{kind}:{row.tenant_id}:{row.owner_id}:{row.id}".encode()


def seal(cipher, kind, row, value):
    encrypted = cipher.encrypt(canonical(value), associated_data=aad(kind, row))
    return {"ct": base64.b64encode(encrypted.ciphertext).decode(), "iv": base64.b64encode(encrypted.nonce).decode(), "kid": encrypted.key_id}


def unseal(cipher, kind, row, value):
    encrypted = EncryptedValue(base64.b64decode(value["ct"]), base64.b64decode(value["iv"]), value["kid"])
    return json.loads(cipher.decrypt(encrypted, associated_data=aad(kind, row)))


async def authorize(db, tenant_id: UUID, user_id: UUID, *, write=False, lock=False):
    query = select(GraphMember).join(User, User.id == GraphMember.user_id).where(
        GraphMember.tenant_id == tenant_id, GraphMember.user_id == user_id,
        GraphMember.active.is_(True), User.is_active.is_(True),
    ).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update(of=(GraphMember, User))
    member = await db.scalar(query)
    user = await db.get(User, user_id)
    gate = await module_gate(db, "company_graph", tenant_id, user, legacy_allowed=member is not None,
                             permission="connector.write" if write else "connector.read") if user else None
    if member is None or gate is None or not (gate.allowed or (write and gate.legacy and gate.reason == "permission_missing")):
        raise NotFoundError("Integration workspace not found.")
    if write and not gate.allowed:
        raise PermissionDeniedError("An owner or editor workspace role is required.")
    if write and member.role not in ("owner", "editor"):
        raise PermissionDeniedError("An owner or editor workspace role is required.")
    return member


def scope(model, tenant_id, owner_id):
    return (model.tenant_id == tenant_id, model.owner_id == owner_id)
