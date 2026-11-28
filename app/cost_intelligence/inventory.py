"""Step N slice 4 — control-plane inventory (read-only).

A unified read model over the six things the admin control plane must
show in one place: identities/memberships, policies (roles), agents,
workflow templates, knowledge ACLs, and integration connections.

The pattern is Step AK's ``unify.py``: read-only views, a real control
record always wins, and a call-window budget is never mapped onto money.
The inventory grants nothing and changes nothing:

* Every section is built from SELECTs only. The API layer proves a GET
  writes nothing (zero-side-effect test).
* A real AK control record wins over a legacy agent view
  (``effective_agent_view``). Step AK is unmerged on this base, so no
  control record exists yet: the agents section is built from the two
  legacy islands (Agent Studio + governance) and the ``sources`` map
  records AK as ``unavailable: ...`` — never a silent gap.
* Anything a legacy island does not express is left empty (deny by
  default) and listed in the view's ``notes`` — never guessed.
* A governance call-window budget (``max_calls`` / ``calls_used`` /
  ``window_seconds``) is **not** money: it is shown as a call budget
  and never mapped onto ``spend_limit_usd`` / ``spend_used_usd``.
* Integration credentials and cursors are never listed: a connector
  appears with its name, type, visibility, activity and sync status
  only. ``credential_ciphertext``, ``credential_nonce``,
  ``credential_key_id`` and both cursors are never selected.
* Knowledge is aggregate counts only (documents by visibility /
  ``acl_status``); no document content, filename, or storage key is
  ever listed. Access groups are listed with member counts.
* Text is untrusted data: agent names, group names and connector names
  are compared and returned as data only; they change no decision.

Scoping: every section is scoped to one platform scope. Memberships,
policies, agents, access groups and documents follow the scope's own
active members; workflow templates come from the in-code catalog
(identical for every scope); connectors follow the sync-mirrored
department name (an AccessGroup department mirrors to a platform
department scope of the same name). A ``module_space`` has no control
view (``ValueError``, the API maps it to 404), mirroring the slice-2/3
rule. Money never appears in the inventory at all: cost figures live in
slices 1–3, and this slice deliberately adds no money field anywhere.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cost_intelligence.ledger import scope_member_user_ids
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import AccessGroup, AccessGroupMember, CloudConnectorConfig, Document, User
from app.governance.models import AgentIdentity
from app.platform.models import (
    PlatformChannelIdentity,
    PlatformMembership,
    PlatformRole,
    PlatformRolePermission,
    PlatformScope,
)
from app.workflows.templates import template_catalog

# The inventory is a read model: it never writes, so it needs no size cap
# of its own — but a single response must stay bounded. A scope with more
# than this many rows in any one section is refused rather than
# truncated, mirroring the slice-2/3 rule.
MAX_SECTION_ROWS = 5000

AK_SOURCE = "agent_controls.store (Step AK control records)"
AK_UNAVAILABLE = (
    "unavailable: Step AK is not merged on this base "
    "(app.agent_controls is absent); agent views come from the legacy "
    "Studio and governance islands only"
)


def _as_uuid(value: Any, field_name: str) -> UUID:
    """Parse a UUID, rejecting anything that is not one."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field_name} must be a UUID.") from exc


def _iso(moment: datetime | None) -> str | None:
    """An ISO-8601 timestamp, or ``None``; naive values are read as UTC."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _jsonable(value: Any) -> Any:
    """Recursively render a view value JSON-safe (ids and datetimes to str)."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (UUID, datetime)):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ----------------------------------------------------------------------------------------------- agent views (pure)

def _uuids(values: object) -> tuple[frozenset[UUID], tuple[str, ...]]:
    """Parse a JSONB id list, dropping self-references and unparseable entries."""
    parsed: set[UUID] = set()
    dropped = 0
    for raw in values or []:
        try:
            parsed.add(UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            dropped += 1
    notes: list[str] = []
    if dropped:
        notes.append(f"{dropped} delegate id(s) could not be parsed and were dropped.")
    return frozenset(parsed), tuple(notes)


def _strings(values: object) -> tuple[str, ...]:
    """Sorted, de-duplicated, non-blank strings from a JSONB list."""
    return tuple(sorted({str(item) for item in (values or []) if str(item).strip()}))


def studio_agent_view(agent: object) -> dict[str, Any]:
    """View an Agent Studio agent (``agent_studio_agents`` row) as a control item.

    Mirrors Step AK ``unify.control_from_studio``: Studio has no
    platform-permission or data-scope list, so both are empty (deny by
    default); ``rag_enabled`` grants no data scope on its own; ``max_steps``
    is clamped to 1–8. Studio carries no spend at all, so the spend
    fields are ``None`` / ``"0.0000"`` — never a mapped budget.
    """
    agent_id = UUID(str(getattr(agent, "id")))
    raw_delegates = getattr(agent, "delegate_agent_ids", None)
    parsed_delegates, delegate_notes = _uuids(raw_delegates)
    delegate_notes = list(delegate_notes)
    if agent_id in parsed_delegates:
        delegate_notes.append("Self-delegation was dropped.")
    delegate_to = sorted(str(d) for d in parsed_delegates if d != agent_id)
    enabled = bool(getattr(agent, "enabled", True))
    paused = bool(getattr(agent, "paused", False))
    status = "disabled" if not enabled else "paused" if paused else "active"
    max_steps = getattr(agent, "max_steps", 6)
    try:
        max_steps = int(max_steps)
    except (TypeError, ValueError):
        max_steps = 6
    clamped = max(1, min(8, max_steps))
    notes = [
        "Studio has no platform-permission list; permissions are empty (deny by default).",
        "Studio has no data-scope list; data scopes are empty even when rag_enabled.",
    ]
    if getattr(agent, "rag_enabled", False):
        notes.append(
            "rag_enabled is set, but it grants no data scope on its own — "
            "create a control record to scope reads."
        )
    if clamped != max_steps:
        notes.append(f"max_steps {max_steps} is outside 1–8 and was clamped to {clamped}.")
    notes.extend(delegate_notes)
    return {
        "agent_id": str(agent_id),
        "kind": "studio",
        "name": str(getattr(agent, "name", "")),
        "owner_id": str(getattr(agent, "owner_id")) if getattr(agent, "owner_id", None) is not None else None,
        "status": status,
        "allowed_actions": _strings(getattr(agent, "tool_permissions", None)),
        "approval_tools": _strings(getattr(agent, "approval_tools", None)),
        "delegate_to": delegate_to,
        "max_steps": clamped,
        "rag_enabled": bool(getattr(agent, "rag_enabled", False)),
        "spend_limit_usd": None,
        "spend_used_usd": "0.0000",
        "notes": tuple(notes),
    }


def governance_agent_view(identity: object) -> dict[str, Any]:
    """View a governance agent (``security_agents`` / AgentIdentity row) as a control item.

    Mirrors Step AK ``unify.control_from_governance``: governance has no
    platform-permission or data-scope list, so both are empty (deny by
    default); the call-window budget is shown as a call budget and is
    **not** mapped onto a spend limit.
    """
    agent_id = UUID(str(getattr(identity, "id")))
    max_calls = getattr(identity, "max_calls", None)
    calls_used = getattr(identity, "calls_used", None)
    window_seconds = getattr(identity, "window_seconds", None)
    enabled = bool(getattr(identity, "enabled", True))
    return {
        "agent_id": str(agent_id),
        "kind": "governance",
        "name": str(getattr(identity, "name", "")),
        "owner_id": str(getattr(identity, "owner_user_id")) if getattr(identity, "owner_user_id", None) is not None else None,
        "status": "disabled" if not enabled else "active",
        "allowed_actions": _strings(getattr(identity, "tools", None)),
        "approval_tools": (),
        "delegate_to": (),
        "max_steps": 6,
        "rag_enabled": False,
        "spend_limit_usd": None,
        "spend_used_usd": "0.0000",
        "call_budget": {
            "calls_used": calls_used,
            "max_calls": max_calls,
            "window_seconds": window_seconds,
        },
        "notes": (
            "Governance has no platform-permission or data-scope list; both are empty (deny by default).",
            "Governance has no step limit; max_steps defaults to 6.",
            f"Call-window budget ({calls_used}/{max_calls} calls in {window_seconds}s) "
            "is not money and was not mapped to a spend limit.",
        ),
    }


def ak_record_view(control: object) -> dict[str, Any]:
    """View a real Step AK control record (``AgentControl``) as a control item.

    The authoritative view: permissions, data scopes, spend limit and
    spend used come from the record itself. Only used when Step AK is
    merged; on this base it is exercised by the pure tests with a fake
    control object.
    """
    spend_limit = getattr(control, "spend_limit_usd", None)
    spend_used = getattr(control, "spend_used_usd", None)
    return {
        "agent_id": str(getattr(control, "agent_id")),
        "kind": f"control-record:{getattr(control, 'agent_kind', 'unknown')}",
        "name": None,
        "owner_id": str(getattr(control, "owner_id")) if getattr(control, "owner_id", None) is not None else None,
        "status": str(getattr(control, "status", "unknown")),
        "allowed_actions": tuple(sorted(str(a) for a in (getattr(control, "allowed_actions", None) or ()))),
        "approval_tools": (),
        "delegate_to": tuple(sorted(str(d) for d in (getattr(control, "delegate_to", None) or ()))),
        "max_steps": getattr(control, "max_steps", None),
        "rag_enabled": False,
        "spend_limit_usd": None if spend_limit is None else str(spend_limit),
        "spend_used_usd": "0.0000" if spend_used is None else str(spend_used),
        "permissions": tuple(sorted(str(p) for p in (getattr(control, "permissions", None) or ()))),
        "data_scopes": tuple(sorted(str(s) for s in (getattr(control, "data_scopes", None) or ()))),
        "notes": ("Authoritative control record.",),
    }


def effective_agent_view(
    record_view: dict[str, Any] | None, *legacy_views: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The view that governs an agent: its real control record when one
    exists, otherwise the first legacy view. A record always wins over
    legacy data — the AK ``unify.effective_control`` rule."""
    if record_view is not None:
        return record_view
    for view in legacy_views:
        if view is not None:
            return view
    return None


def inventory_fingerprint(sections: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the inventory sections.

    Deterministic: the same inventory always fingerprints the same way,
    and any change to any row changes the fingerprint.
    """
    canonical = json.dumps(_jsonable(sections), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------------------- inventory model

@dataclass(frozen=True)
class InventorySection:
    """One inventory section: what it lists, where it came from, and the rows."""

    key: str
    title: str
    source: str
    items: tuple[dict[str, Any], ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "source": self.source,
            "count": len(self.items),
            "items": [_jsonable(item) for item in self.items],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ControlInventory:
    """The whole control-plane inventory for one scope, with provenance."""

    scope_id: str
    scope_kind: str
    scope_name: str
    sections: tuple[InventorySection, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""

    @property
    def counts(self) -> dict[str, int]:
        return {section.key: len(section.items) for section in self.sections}

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "scope_kind": self.scope_kind,
            "scope_name": self.scope_name,
            "sections": [section.as_dict() for section in self.sections],
            "counts": self.counts,
            "sources": dict(self.sources),
            "fingerprint": self.fingerprint,
        }


# ----------------------------------------------------------------------------------------------- AK loader (degrades gracefully)

async def _ak_control_views(
    db: AsyncSession, agent_ids: list[UUID]
) -> tuple[dict[str, dict[str, Any]], str]:
    """Real AK control views for the given agents, when Step AK is merged.

    Returns ``(views_by_agent_id, source_note)``. On this base
    ``app.agent_controls`` does not exist, so this returns no views and
    the ``unavailable`` note — never a silent gap, never an exception
    that breaks the inventory.
    """
    if not agent_ids:
        return {}, AK_UNAVAILABLE
    try:
        from app.agent_controls.store import get_control_record, record_to_control
    except Exception:
        return {}, AK_UNAVAILABLE
    views: dict[str, dict[str, Any]] = {}
    try:
        for agent_id in agent_ids:
            row = await get_control_record(db, agent_id)
            if row is not None:
                views[str(agent_id)] = ak_record_view(record_to_control(row))
    except Exception:
        return {}, (
            "unavailable: Step AK control records could not be read "
            "(store call failed); agent views come from the legacy "
            "Studio and governance islands only"
        )
    return views, AK_SOURCE


async def _nearest_department(db: AsyncSession, scope: PlatformScope) -> PlatformScope | None:
    """Nearest department ancestor of a scope (itself when it is one)."""
    current: PlatformScope | None = scope
    seen: set[UUID] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.kind == "department":
            return current
        if current.parent_id is None:
            return None
        current = await db.get(PlatformScope, current.parent_id)
    return None


def _cap(items: list[dict[str, Any]], section: str) -> tuple[dict[str, Any], ...]:
    """Refuse a silent truncation: a section over the cap is an error."""
    if len(items) > MAX_SECTION_ROWS:
        raise ValueError(
            f"refusing to truncate inventory section {section}: "
            f"{len(items)} rows over the {MAX_SECTION_ROWS} cap"
        )
    return tuple(items)


async def control_inventory(db: AsyncSession, scope_id: UUID) -> ControlInventory:
    """The control-plane inventory for one scope (read-only, SELECTs only).

    Raises ``ValueError`` for an unknown or inactive scope and for a
    ``module_space`` (no control view there), mirroring the slice-2/3
    rule; the API layer maps it to 404.
    """
    scope_id = _as_uuid(scope_id, "scope_id")
    scope = await db.get(PlatformScope, scope_id)
    if scope is None or not scope.active:
        raise ValueError(f"unknown or inactive scope: {scope_id}")
    if scope.kind == "module_space":
        raise ValueError(
            f"scope {scope_id} is a module_space; the control plane covers "
            "departments, workspaces, and the org"
        )
    member_ids = await scope_member_user_ids(db, scope_id)
    if scope.kind == "org":
        # The org roll-up is the whole company: every active user is a
        # member of the org inventory, and every membership is listed.
        member_ids = frozenset(
            await db.scalars(select(User.id).where(User.is_active.is_(True)))
        )
    org_wide = scope.kind == "org"
    now = datetime.now(UTC)
    sources: dict[str, str] = {}

    # --- identities / memberships: the scope's own rows (every row for org).
    membership_stmt = (
        select(PlatformMembership, PlatformRole.key, User.email, User.role, User.is_active)
        .join(PlatformRole, PlatformRole.id == PlatformMembership.role_id)
        .join(User, User.id == PlatformMembership.user_id)
        .order_by(User.email, PlatformRole.key)
    )
    if not org_wide:
        membership_stmt = membership_stmt.where(PlatformMembership.scope_id == scope_id)
    membership_rows = (await db.execute(membership_stmt)).all()
    identities: list[dict[str, Any]] = []
    for membership, role_key, email, legacy_role, is_active in membership_rows:
        identities.append({
            "membership_id": str(membership.id),
            "user_id": str(membership.user_id),
            "email": email,
            "legacy_role": legacy_role,
            "is_active": bool(is_active),
            "role_key": role_key,
            "valid_from": _iso(membership.valid_from),
            "valid_to": _iso(membership.valid_to),
            "revoked_at": _iso(membership.revoked_at),
            "source": membership.source,
        })
    channel_rows = (
        await db.execute(
            select(PlatformChannelIdentity)
            .where(PlatformChannelIdentity.user_id.in_(member_ids))
            .order_by(PlatformChannelIdentity.channel, PlatformChannelIdentity.external_id)
        )
    ).scalars().all() if member_ids else []
    channels: list[dict[str, Any]] = [
        {
            "user_id": str(row.user_id),
            "channel": row.channel,
            "external_id": row.external_id,
            "verified_at": _iso(row.verified_at),
            "verification_method": row.verification_method,
            "revoked_at": _iso(row.revoked_at),
        }
        for row in channel_rows
    ]
    sources["identities"] = (
        "platform_memberships + platform_channel_identities "
        "(every membership company-wide for the org scope; the scope's "
        "own memberships otherwise; channel identities of its members)"
    )
    sources["channel_identities"] = sources["identities"]

    # --- policies: roles holding permissions on this scope, with member counts.
    role_stmt = (
        select(
            PlatformRole.key,
            PlatformRole.name,
            PlatformRole.system,
            func.count(PlatformMembership.id),
        )
        .join(PlatformMembership, PlatformMembership.role_id == PlatformRole.id)
        .group_by(PlatformRole.key, PlatformRole.name, PlatformRole.system)
        .order_by(PlatformRole.key)
    )
    if not org_wide:
        role_stmt = role_stmt.where(PlatformMembership.scope_id == scope_id)
    role_rows = (await db.execute(role_stmt)).all()
    role_keys = [row[0] for row in role_rows]
    perms_by_role: dict[str, list[str]] = {key: [] for key in role_keys}
    if role_keys:
        perm_rows = (
            await db.execute(
                select(PlatformRole.key, PlatformRolePermission.permission)
                .join(PlatformRolePermission, PlatformRolePermission.role_id == PlatformRole.id)
                .where(PlatformRole.key.in_(role_keys))
                .order_by(PlatformRole.key, PlatformRolePermission.permission)
            )
        ).all()
        for key, permission in perm_rows:
            perms_by_role.setdefault(key, []).append(permission)
    policies: list[dict[str, Any]] = [
        {
            "role_key": key,
            "name": name,
            "system": bool(system),
            "permissions": perms_by_role.get(key, []),
            "member_count": int(count),
        }
        for key, name, system, count in role_rows
    ]
    sources["policies"] = (
        "platform_roles + platform_role_permissions "
        "(roles with at least one membership in this scope)"
    )

    # --- agents: unified views; a real AK record always wins.
    studio_rows = (
        await db.execute(
            select(AgentStudioAgent)
            .where(AgentStudioAgent.owner_id.in_(member_ids))
            .order_by(AgentStudioAgent.name, AgentStudioAgent.id)
        )
    ).scalars().all() if member_ids else []
    governance_rows = (
        await db.execute(
            select(AgentIdentity)
            .where(AgentIdentity.owner_user_id.in_(member_ids))
            .order_by(AgentIdentity.name, AgentIdentity.id)
        )
    ).scalars().all() if member_ids else []
    legacy_views: dict[str, dict[str, Any]] = {}
    for row in studio_rows:
        view = studio_agent_view(row)
        legacy_views.setdefault(view["agent_id"], view)
    for row in governance_rows:
        view = governance_agent_view(row)
        legacy_views.setdefault(view["agent_id"], view)
    ak_views, ak_source = await _ak_control_views(
        db, [UUID(view["agent_id"]) for view in legacy_views.values()]
    )
    agents: list[dict[str, Any]] = []
    for agent_id in sorted(legacy_views, key=lambda a: (legacy_views[a]["kind"], legacy_views[a]["name"] or "", a)):
        agents.append(
            effective_agent_view(ak_views.get(agent_id), legacy_views[agent_id])
            or legacy_views[agent_id]
        )
    sources["agents"] = "agent_studio_agents + security_agents (legacy island views)"
    sources["ak_controls"] = ak_source

    # --- workflow templates: the in-code catalog, identical for every scope.
    templates: list[dict[str, Any]] = []
    for template in template_catalog():
        spec = template.get("spec", {}) or {}
        templates.append({
            "key": template.get("key"),
            "name": template.get("name"),
            "description": template.get("description"),
            "node_count": len(spec.get("nodes", []) or []),
            "edge_count": len(spec.get("edges", []) or []),
        })
    templates.sort(key=lambda item: str(item.get("key")))
    sources["workflow_templates"] = (
        "app.workflows.templates (in-code catalog, identical for every scope)"
    )

    # --- knowledge ACLs: access groups with members in this scope + document counts.
    group_rows = (
        await db.execute(
            select(
                AccessGroup.id,
                AccessGroup.name,
                AccessGroup.kind,
                AccessGroup.source,
                AccessGroup.lifecycle_status,
                func.count(AccessGroupMember.user_id),
            )
            .join(AccessGroupMember, AccessGroupMember.group_id == AccessGroup.id)
            .where(AccessGroupMember.user_id.in_(member_ids))
            .group_by(
                AccessGroup.id,
                AccessGroup.name,
                AccessGroup.kind,
                AccessGroup.source,
                AccessGroup.lifecycle_status,
            )
            .order_by(AccessGroup.kind, AccessGroup.name)
        )
    ).all() if member_ids else []
    groups: list[dict[str, Any]] = [
        {
            "kind": "access_group",
            "group_id": str(group_id),
            "name": name,
            "kind_name": kind,
            "source": source,
            "lifecycle_status": lifecycle_status,
            "member_count": int(count),
        }
        for group_id, name, kind, source, lifecycle_status, count in group_rows
    ]
    doc_rows = (
        await db.execute(
            select(Document.visibility, Document.acl_status, func.count(Document.id))
            .where(Document.owner_id.in_(member_ids))
            .group_by(Document.visibility, Document.acl_status)
            .order_by(Document.visibility, Document.acl_status)
        )
    ).all() if member_ids else []
    documents: list[dict[str, Any]] = [
        {
            "kind": "document_count",
            "visibility": visibility,
            "acl_status": acl_status,
            "count": int(count),
        }
        for visibility, acl_status, count in doc_rows
    ]
    sources["knowledge_acls"] = (
        "access_groups + access_group_members (groups with members in "
        "this scope) + documents (aggregate counts by visibility / "
        "acl_status only; no document content)"
    )

    # --- integration connections: scoped by the sync-mirrored department name.
    department = await _nearest_department(db, scope)
    connector_rows = (await db.execute(select(CloudConnectorConfig).order_by(CloudConnectorConfig.name))).scalars().all()
    group_names: dict[UUID, str] = {}
    if connector_rows:
        group_ids = {row.department_group_id for row in connector_rows if row.department_group_id is not None}
        if group_ids:
            for group in (
                await db.execute(
                    select(AccessGroup).where(AccessGroup.id.in_(group_ids))
                )
            ).scalars().all():
                group_names[group.id] = group.name
    integrations: list[dict[str, Any]] = []
    for row in connector_rows:
        if scope.kind == "org":
            in_scope = True
        elif row.department_group_id is None:
            in_scope = False  # org-level connector; not visible from a department
        else:
            in_scope = (
                department is not None
                and group_names.get(row.department_group_id) == department.name
            )
        if not in_scope:
            continue
        integrations.append({
            "connector_id": str(row.id),
            "name": row.name,
            "connector_type": row.connector_type,
            "visibility": row.visibility,
            "is_active": bool(row.is_active),
            "sync_interval_minutes": row.sync_interval_minutes,
            "last_sync_status": row.last_sync_status,
            "last_sync_error_count": row.last_sync_error_count,
            "last_synced_at": _iso(row.last_synced_at),
            "department_group": group_names.get(row.department_group_id),
        })
    sources["integrations"] = (
        "cloud_connector_configs (credentials and cursors are never "
        "selected; department connectors follow the sync-mirrored "
        "department name)"
    )

    sections = (
        InventorySection(
            key="identities",
            title="Identities and memberships",
            source=sources["identities"],
            items=_cap(identities, "identities"),
        ),
        InventorySection(
            key="channel_identities",
            title="Verified channel identities",
            source=sources["channel_identities"],
            items=_cap(channels, "channel_identities"),
        ),
        InventorySection(
            key="policies",
            title="Roles and permissions",
            source=sources["policies"],
            items=_cap(policies, "policies"),
        ),
        InventorySection(
            key="agents",
            title="Agents (unified control views)",
            source=sources["agents"],
            items=_cap(agents, "agents"),
            notes=(
                "A real Step AK control record always wins over a legacy "
                "view; AK is unmerged on this base, so these are legacy "
                "island views (see sources.ak_controls)."
                if ak_source.startswith("unavailable")
                else ("A real Step AK control record governs each agent shown.",)
            ),
        ),
        InventorySection(
            key="workflow_templates",
            title="Workflow templates",
            source=sources["workflow_templates"],
            items=_cap(templates, "workflow_templates"),
        ),
        InventorySection(
            key="knowledge_acls",
            title="Knowledge access groups and document ACL counts",
            source=sources["knowledge_acls"],
            items=_cap(groups + documents, "knowledge_acls"),
            notes=(
                "Document rows are aggregate counts only; no document "
                "content, filename, or storage key is listed.",
            ),
        ),
        InventorySection(
            key="integrations",
            title="Integration connections",
            source=sources["integrations"],
            items=_cap(integrations, "integrations"),
            notes=(
                "Connector credentials and sync cursors are never listed.",
            ),
        ),
    )
    fingerprint = inventory_fingerprint(
        {section.key: section.as_dict()["items"] for section in sections}
    )
    return ControlInventory(
        scope_id=str(scope_id),
        scope_kind=scope.kind,
        scope_name=scope.name,
        sections=sections,
        sources=sources,
        fingerprint=fingerprint,
    )
