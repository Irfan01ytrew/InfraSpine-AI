"""Effective-permission handover checks and time-limited handover grants (Step G slice 5).

A recorded ``backup_for`` link and matching skills are not enough: the
replacement must actually hold the platform permissions the work needs on the
resource's department scope. The check itself is read-only. A grant is issued
only after the check passes AND a second person approves it; the grant is
time-limited (``valid_to``) and revocable. After revoke or expiry the
platform membership is gone, so the holder can no longer open the pack or
resume the work.

Permissions are checked with ``app.platform.access.effective_permissions``
against the department scope a resource belongs to (its department node's
``access_group_id``). A resource whose department binds no access group has
no platform scope to check: the permission part is reported as
``not_applicable`` and eligibility rests on the backup relationship and
skills alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.continuity import journal
from app.continuity.access import node_for
from app.continuity.models import HandoverGrant, Link, Node
from app.continuity.schemas import PERMISSION_PATTERN
from app.core.errors import ConflictError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.platform.access import effective_permissions, ensure_role, grant_membership, principal_for
from app.platform.models import PlatformMembership, PlatformRole, PlatformScope
from app.platform.sync import group_scope_sync

# What a replacement must actually hold to do the work, per resource kind.
# These are the permissions the existing gates enforce for the work itself:
# workflow runs are gated on ``workflow.access`` and operating documentation
# on ``documents.read``; department membership is ``group.access``. A resource
# may record its own list in ``node.data["required_permissions"]`` instead.
REQUIRED_PERMISSIONS_BY_KIND: dict[str, frozenset[str]] = {
    "process": frozenset({"workflow.access", "documents.read"}),
    "service": frozenset({"workflow.access", "documents.read"}),
    "system": frozenset({"workflow.access", "documents.read"}),
    "project": frozenset({"workflow.access", "documents.read"}),
    "approval": frozenset({"workflow.access", "documents.read"}),
    "department": frozenset({"group.access"}),
    "knowledge": frozenset({"documents.read"}),
}

HANDOVER_ROLE_KEY = "continuity.handover"
DECISION_WINDOW = timedelta(hours=1)


def required_permissions(node_data, kind: str) -> frozenset[str]:
    """Recorded per-resource permissions win; otherwise the per-kind default."""
    recorded = node_data.get("required_permissions") if isinstance(node_data, dict) else None
    if recorded:
        cleaned = {str(item) for item in recorded
                   if isinstance(item, str) and PERMISSION_PATTERN.match(item) and len(item) <= 80}
        if cleaned:
            return frozenset(cleaned)
    return REQUIRED_PERMISSIONS_BY_KIND.get(kind, frozenset())


def check_candidate(*, required, held, skills, required_skills, backup_ok, available,
                    has_user: bool) -> dict:
    """One candidate against the recorded backup, skills and permission evidence."""
    gaps = []
    if not has_user:
        gaps.append({"code": "no_user_binding",
                     "detail": "Candidate employee is not bound to a user, so no platform access can be checked."})
    missing_permissions = sorted(set(required) - set(held))
    for permission in missing_permissions:
        gaps.append({"code": "missing_permission",
                     "detail": f"Replacement does not hold {permission} on the resource scope."})
    missing_skills = sorted({s.casefold() for s in required_skills} - {s.casefold() for s in skills})
    for skill in missing_skills:
        gaps.append({"code": "missing_skill",
                     "detail": f"Replacement does not record required skill {skill}."})
    if not backup_ok:
        gaps.append({"code": "missing_backup",
                     "detail": "No evidenced backup relationship to this resource."})
    if not available:
        gaps.append({"code": "candidate_unavailable",
                     "detail": "Replacement is not marked available."})
    return {"eligible": not gaps, "gaps": gaps, "missing_permissions": missing_permissions,
            "missing_skills": missing_skills, "backup_ok": backup_ok}


def _group_steps(draft) -> dict:
    """Group handover-draft steps by resource, keeping candidate owners in order."""
    grouped: dict[str, list[str]] = {}
    for step in draft.get("steps", []):
        resource_id = str(step["resource_id"])
        owner_id = step.get("owner_id")
        owners = grouped.setdefault(resource_id, [])
        if owner_id is not None and str(owner_id) not in owners:
            owners.append(str(owner_id))
    return grouped


def handover_check(draft, nodes, links, held_by_user_id, scope_by_resource_id) -> dict:
    """Enrich a handover draft with an effective-permission check per candidate.

    ``nodes`` and ``links`` are plain mappings (id/kind/name/data/user_id and
    source_id/target_id/kind/basis). ``held_by_user_id`` maps a user id to the
    permissions that user holds on the resource scope; ``scope_by_resource_id``
    maps a resource id to its platform scope id (or None when unbound).
    Pure and deterministic: no I/O, no clock.
    """
    by_id = {str(n["id"]): n for n in nodes}
    verified_backup = {(str(link["source_id"]), str(link["target_id"]))
                       for link in links
                       if link["kind"] == "backup_for" and link.get("basis") == "verified_fact"}
    resources = []
    for resource_id, candidate_ids in _group_steps(draft).items():
        resource = by_id.get(resource_id)
        if resource is None:
            continue
        required = required_permissions(resource.get("data") or {}, resource["kind"])
        scope_id = scope_by_resource_id.get(resource_id)
        candidates = []
        for candidate_id in candidate_ids:
            candidate = by_id.get(str(candidate_id))
            if candidate is None or candidate["kind"] != "employee":
                continue
            user_id = candidate.get("user_id")
            held = held_by_user_id.get(str(user_id), set()) if user_id else set()
            result = check_candidate(
                required=required if scope_id is not None else frozenset(),
                held=held,
                skills=(candidate.get("data") or {}).get("skills", []),
                required_skills=(resource.get("data") or {}).get("required_skills", []),
                backup_ok=(str(candidate_id), resource_id) in verified_backup,
                available=bool((candidate.get("data") or {}).get("available", False)),
                has_user=user_id is not None,
            )
            candidates.append({"node_id": str(candidate_id), "user_id": str(user_id) if user_id else None,
                               **result})
        resources.append({
            "resource_id": resource_id,
            "resource_name": resource["name"],
            "resource_kind": resource["kind"],
            "scope_id": str(scope_id) if scope_id else None,
            "scope_bound": scope_id is not None,
            "permission_check": "checked" if scope_id is not None else "not_applicable",
            "permission_note": (None if scope_id is not None else
                                "Resource department binds no access group, so no platform scope exists to check."),
            "required_permissions": sorted(required),
            "candidates": candidates,
            "eligible_candidates": [c["node_id"] for c in candidates if c["eligible"]],
        })
    return {"draft": draft, "resources": resources,
            "eligible": bool(resources) and all(r["eligible_candidates"] for r in resources),
            "unresolved_resource_ids": draft.get("unresolved_resource_ids", [])}


# ------------------------------------------------------------------------------------------- service

async def resolve_department_scope(db, resource) -> tuple[uuid.UUID | None, str]:
    """The platform scope for a resource's department, or (None, reason)."""
    department_id = resource.department_id
    if department_id is None:
        return None, "no_department"
    department = await db.get(Node, department_id)
    if department is None or not department.access_group_id:
        return None, "no_access_group"
    scope_id = await db.scalar(select(PlatformScope.id).where(
        PlatformScope.legacy_table == "access_groups",
        PlatformScope.legacy_id == department.access_group_id))
    if scope_id is None:
        scope_id = await db.run_sync(
            lambda s: group_scope_sync(s.connection(), department.access_group_id))
    return scope_id, "bound"


async def _pair_check(db, scope, resource, candidate) -> dict:
    """The check for one (resource, candidate) pair, via the platform."""
    scope_id, _reason = await resolve_department_scope(db, resource)
    held: set[str] = set()
    if scope_id is not None and candidate.user_id is not None:
        held = await effective_permissions(db, candidate.user_id, scope_id)
    required = required_permissions(resource.data or {}, resource.kind)
    links = await db.scalars(select(Link).where(Link.tenant_id == scope.tenant_id))
    backup_ok = any(link.kind == "backup_for" and link.source_id == candidate.id
                    and link.target_id == resource.id and (link.data or {}).get("basis") == "verified_fact"
                    for link in links)
    return check_candidate(
        required=required if scope_id is not None else frozenset(),
        held=held,
        skills=(candidate.data or {}).get("skills", []),
        required_skills=(resource.data or {}).get("required_skills", []),
        backup_ok=backup_ok,
        available=bool((candidate.data or {}).get("available", False)),
        has_user=candidate.user_id is not None,
    )


async def check_handover(db, scope, scenario_id: uuid.UUID) -> dict:
    """The handover draft for a scenario, with every candidate permission-checked."""
    from app.continuity import intelligence, service

    scenario = await service.scenario_for(db, scope, scenario_id)
    nodes, links, _assessments = await service.graph(db, scope)
    draft = intelligence.handover_draft(scenario, nodes)
    node_views = [{"id": str(n.id), "kind": n.kind, "name": n.name,
                   "data": n.data or {}, "user_id": str(n.user_id) if n.user_id else None} for n in nodes]
    link_views = [{"source_id": str(link.source_id), "target_id": str(link.target_id),
                   "kind": link.kind, "basis": (link.data or {}).get("basis")} for link in links]
    by_id = {str(n.id): n for n in nodes}
    scope_by_resource: dict[str, uuid.UUID | None] = {}
    held_by_user: dict[str, set[str]] = {}
    for resource_id, candidate_ids in _group_steps(draft).items():
        resource = by_id.get(resource_id)
        if resource is None:
            continue
        scope_id, _reason = await resolve_department_scope(db, resource)
        scope_by_resource[str(resource.id)] = scope_id
        if scope_id is None:
            continue
        for candidate_id in candidate_ids:
            candidate = by_id.get(str(candidate_id))
            if candidate is None or candidate.user_id is None:
                continue
            key = str(candidate.user_id)
            if key not in held_by_user:
                held_by_user[key] = await effective_permissions(db, candidate.user_id, scope_id)
    return handover_check(draft, node_views, link_views, held_by_user, scope_by_resource)


async def propose_handover_grant(db, scope, data, ctx):
    """Request a time-limited handover grant. Only an eligible candidate may be proposed."""
    from app.continuity import service

    scope.require("owner", "planner")
    resource = await node_for(db, scope, data.resource_id)
    candidate = await node_for(db, scope, data.candidate_node_id)
    if candidate.kind != "employee":
        raise InvalidInputError("A handover grant names an employee replacement.")
    if data.scenario_id is not None:
        await service.scenario_for(db, scope, data.scenario_id)
    if data.plan_id is not None:
        await service.plan_for(db, scope, data.plan_id)
    result = await _pair_check(db, scope, resource, candidate)
    if not result["eligible"]:
        raise ConflictError("Handover candidate is not eligible: "
                            + "; ".join(gap["detail"] for gap in result["gaps"]))
    scope_id, _reason = await resolve_department_scope(db, resource)
    now = datetime.now(UTC)
    payload = {"scenario_id": str(data.scenario_id) if data.scenario_id else None,
               "plan_id": str(data.plan_id) if data.plan_id else None,
               "resource_id": str(resource.id), "candidate_node_id": str(candidate.id),
               "user_id": str(candidate.user_id), "scope_id": str(scope_id) if scope_id else None,
               "permissions": sorted(required_permissions(resource.data or {}, resource.kind)),
               "valid_days": data.valid_days}
    row = HandoverGrant(
        tenant_id=scope.tenant_id, scenario_id=data.scenario_id, plan_id=data.plan_id,
        resource_id=resource.id, candidate_node_id=candidate.id, user_id=candidate.user_id,
        scope_id=scope_id, permissions=payload["permissions"], status="pending",
        valid_to=now + timedelta(days=data.valid_days), requested_by=scope.user_id,
        digest=journal.digest(payload), reason=data.reason,
        decision_expires_at=now + DECISION_WINDOW)
    db.add(row)
    await db.flush()
    await journal.record(db, scope, ctx, "handover_grant_requested", row.id, {"digest": row.digest})
    return service.public(row)


async def list_handover_grants(db, scope):
    from app.continuity import service

    rows = await db.scalars(select(HandoverGrant).where(HandoverGrant.tenant_id == scope.tenant_id)
                            .order_by(HandoverGrant.created_at.desc()).limit(100))
    return [service.public(row) for row in rows]


async def _grant_for(db, scope, grant_id: uuid.UUID) -> HandoverGrant:
    row = await db.get(HandoverGrant, grant_id)
    if row is None or row.tenant_id != scope.tenant_id:
        raise NotFoundError("Handover grant not found.")
    return row


async def decide_handover_grant(db, scope, grant_id: uuid.UUID, data, ctx, actor):
    """Second-person decision. On approval the time-limited membership is created."""
    from app.continuity import service

    scope.require("owner", "approver")
    row = await _grant_for(db, scope, grant_id)
    if scope.user_id == row.requested_by or scope.user_id == row.user_id:
        raise PermissionDeniedError("Requester and beneficiary cannot decide a handover grant.")
    if row.status != "pending" or row.decision_expires_at <= datetime.now(UTC) or data.digest != row.digest:
        raise ConflictError("The grant decision is stale, expired or does not match the requested action.")
    now = datetime.now(UTC)
    if data.approved:
        if row.scope_id is None:
            raise ConflictError("This resource binds no platform scope, so no membership can be granted.")
        principal = principal_for(actor)
        await ensure_role(db, HANDOVER_ROLE_KEY, set(row.permissions or []))
        role_id = await db.scalar(select(PlatformRole.id).where(PlatformRole.key == HANDOVER_ROLE_KEY))
        superseded = await db.scalars(select(PlatformMembership).where(
            PlatformMembership.scope_id == row.scope_id, PlatformMembership.user_id == row.user_id,
            PlatformMembership.role_id == role_id, PlatformMembership.revoked_at.is_(None)))
        for membership in superseded:
            membership.revoked_at = now
            membership.revoke_reason = "continuity:handover_superseded"
        await grant_membership(db, principal, user_id=row.user_id, scope_id=row.scope_id,
                               role_key=HANDOVER_ROLE_KEY, valid_to=row.valid_to)
        row.status, row.valid_from = "active", now
    else:
        row.status = "rejected"
    row.decided_by = scope.user_id
    await journal.record(db, scope, ctx, "handover_grant_decided", row.id,
                         {"decision": row.status, "digest": row.digest, "reason": data.reason})
    return service.public(row)


async def revoke_handover_grant(db, scope, grant_id: uuid.UUID, data, ctx):
    """Revoke an active grant: the platform membership ends at once."""
    from app.continuity import service

    scope.require("owner", "approver")
    row = await _grant_for(db, scope, grant_id)
    if row.status != "active":
        raise ConflictError("Only an active handover grant can be revoked.")
    now = datetime.now(UTC)
    role_id = await db.scalar(select(PlatformRole.id).where(PlatformRole.key == HANDOVER_ROLE_KEY))
    if role_id is not None and row.scope_id is not None:
        memberships = await db.scalars(select(PlatformMembership).where(
            PlatformMembership.scope_id == row.scope_id, PlatformMembership.user_id == row.user_id,
            PlatformMembership.role_id == role_id, PlatformMembership.revoked_at.is_(None)))
        for membership in memberships:
            membership.revoked_at = now
            membership.revoke_reason = "continuity:handover_revoked"
    row.status = "revoked"
    await journal.record(db, scope, ctx, "handover_grant_revoked", row.id, {"reason": data.reason})
    return service.public(row)


async def handover_access_active(db, grant: HandoverGrant) -> bool:
    """True only while the grant is active, unexpired, and its membership stands."""
    if grant.status != "active" or grant.scope_id is None:
        return False
    now = datetime.now(UTC)
    if grant.valid_to is not None and grant.valid_to <= now:
        return False
    role_id = await db.scalar(select(PlatformRole.id).where(PlatformRole.key == HANDOVER_ROLE_KEY))
    if role_id is None:
        return False
    membership = await db.scalar(select(PlatformMembership).where(
        PlatformMembership.scope_id == grant.scope_id, PlatformMembership.user_id == grant.user_id,
        PlatformMembership.role_id == role_id, PlatformMembership.revoked_at.is_(None),
        (PlatformMembership.valid_to.is_(None)) | (PlatformMembership.valid_to > now)).limit(1))
    return membership is not None
