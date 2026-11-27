"""Source ACL synchronisation for documents synced from the company file server (ADR-004 R3.1).

This is the boundary a file-server connector calls. Rules:
* Only allow entries for known principals are representable. A deny entry, an unknown entry type or
  right, an unknown principal type, an unresolved principal or an empty ACL cannot be mapped without
  risking wider access, so the document is quarantined instead.
* Quarantine is fail-closed: the document's ACL rows are removed and nobody can read it, administrators
  included (`access.readable_by` requires `acl_status = 'active'`). There is no admin read path.
* A sync always replaces the previous ACL, so a narrowing at the source applies on the next request.
* InfraSpine never widens a synced ACL and has no identity system of its own for the source: principals
  are matched by the directory object ID stored on users and directory groups (`external_id`).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.models import (
    ACL_ACTIVE,
    ACL_QUARANTINED,
    GROUP_DIRECTORY,
    SOURCE_UPLOAD,
    AccessGroup,
    AccessGroupMember,
    Document,
    ResourceAcl,
    User,
)
from app.services import audit
from app.services.audit import AuditContext

# Every supported right includes read; InfraSpine only ever grants read.
SUPPORTED_RIGHTS = frozenset({"read", "read_execute", "list", "write", "modify", "full_control"})


@dataclass(frozen=True)
class SourceAce:
    principal_type: str  # "user" | "group"
    external_id: str  # directory object ID, e.g. an Active Directory SID
    access: str = "allow"  # "allow" | "deny"
    rights: str = "read"


@dataclass(frozen=True)
class AclSyncResult:
    acl_status: str
    reason: str | None
    users: int
    groups: int
    unresolved: int


def _unsupported(entries: list[SourceAce]) -> str | None:
    if not entries:
        return "empty_acl"
    for entry in entries:
        if entry.access == "deny":
            return "unsupported_deny_entry"  # dropping a deny could widen access
        if entry.access != "allow":
            return "unsupported_entry_type"
        if entry.principal_type not in ("user", "group"):
            return "unsupported_principal_type"
        if entry.rights not in SUPPORTED_RIGHTS:
            return "unsupported_right"
        if not entry.external_id:
            return "unresolved_principal"
    return None


async def sync_document_acl(
    db: AsyncSession, document_id: uuid.UUID, entries: list[SourceAce], ctx: AuditContext
) -> AclSyncResult:
    doc = (
        await db.execute(select(Document).where(Document.id == document_id).with_for_update())
    ).scalar_one_or_none()
    if doc is None:
        raise NotFoundError("Document not found.")
    if doc.source_system == SOURCE_UPLOAD:
        raise InvalidInputError("Only synced documents take their ACL from the source.")

    reason = _unsupported(entries)
    user_ids: set[uuid.UUID] = set()
    group_ids: set[uuid.UUID] = set()
    unresolved = 0
    if reason is None:
        wanted_users = {e.external_id for e in entries if e.principal_type == "user"}
        wanted_groups = {e.external_id for e in entries if e.principal_type == "group"}
        users = dict(
            (await db.execute(select(User.external_id, User.id).where(User.external_id.in_(wanted_users)))).all()
        ) if wanted_users else {}
        groups = dict(
            (
                await db.execute(
                    select(AccessGroup.external_id, AccessGroup.id).where(
                        AccessGroup.source == GROUP_DIRECTORY, AccessGroup.external_id.in_(wanted_groups)
                    )
                )
            ).all()
        ) if wanted_groups else {}
        unresolved = len(wanted_users - users.keys()) + len(wanted_groups - groups.keys())
        if unresolved:
            reason = "unresolved_principal"
        user_ids, group_ids = set(users.values()), set(groups.values())

    await db.execute(delete(ResourceAcl).where(ResourceAcl.document_id == doc.id))
    if reason is None:
        for principal in [{"principal_user_id": u} for u in user_ids] + [{"principal_group_id": g} for g in group_ids]:
            db.add(ResourceAcl(resource_type="document", document_id=doc.id, origin="source_sync", **principal))
        doc.acl_status, doc.acl_quarantine_reason = ACL_ACTIVE, None
    else:
        user_ids, group_ids = set(), set()
        doc.acl_status, doc.acl_quarantine_reason = ACL_QUARANTINED, reason
    doc.acl_synced_at = datetime.now(UTC)

    # Counts and reason codes only: no document names, paths or principal identifiers.
    audit.record(
        db, ctx, "document.acl_synced" if reason is None else "document.acl_quarantined", target=str(doc.id),
        details={"reason": reason, "entries": len(entries), "unresolved": unresolved},
    )
    await db.commit()
    return AclSyncResult(doc.acl_status, reason, len(user_ids), len(group_ids), unresolved)


async def sync_directory_group(
    db: AsyncSession,
    external_id: str,
    name: str,
    kind: str,
    member_external_ids: list[str],
    ctx: AuditContext,
    *,
    member_user_ids: set[uuid.UUID] | None = None,
) -> AccessGroup:
    """Create or update a directory-owned group and replace its membership. Members are matched by
    `users.external_id` unless the caller supplies already-resolved local SCIM user IDs. Unknown source
    members are skipped (they have no InfraSpine account to grant)."""
    if not external_id or not external_id.strip():
        raise InvalidInputError("A directory group requires a non-empty external ID.")
    external_id = external_id.strip()
    group = (
        await db.execute(select(AccessGroup).where(AccessGroup.external_id == external_id).with_for_update())
    ).scalar_one_or_none()
    if group is not None and group.source != GROUP_DIRECTORY:
        raise ConflictError("The directory group ID is already assigned to a locally managed group.")
    if group is None:
        group = AccessGroup(name=name, kind=kind, source=GROUP_DIRECTORY, external_id=external_id)
        db.add(group)
    else:
        group.name, group.kind = name, kind
    group.lifecycle_status = "active"
    try:
        await db.flush()
        if member_user_ids is None:
            members = set(
                (await db.execute(select(User.id).where(User.external_id.in_(set(member_external_ids))))).scalars()
            ) if member_external_ids else set()
        else:
            members = set(
                (await db.execute(select(User.id).where(User.id.in_(member_user_ids)))).scalars()
            ) if member_user_ids else set()
        await db.execute(delete(AccessGroupMember).where(AccessGroupMember.group_id == group.id))
        for user_id in members:
            db.add(AccessGroupMember(group_id=group.id, user_id=user_id))
        audit.record(
            db, ctx, "access.directory_group_synced", target=str(group.id),
            details={"members": len(members), "skipped": len(set(member_external_ids)) - len(members)},
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("A group with this name or directory ID already exists.") from None
    await db.refresh(group)
    return group
