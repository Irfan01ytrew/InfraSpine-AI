"""Step M (slice 3): per-reader evidence readability.

Given the evidence references attached to a decision, compute the subset
of ``(kind, ref)`` pairs the reader may read. Anything not in the set is
dropped by ``visible_evidence`` (deny by default) and counted as hidden.

Rules per kind (each reuses the owning module's own access check — this
module invents no access rule of its own):

- ``document`` — the document exists and the reader may read it under
  ``app.rag.access.readable_by`` (visibility / ACL / quarantine plus the
  live ``documents.read`` platform gate). A ref that is not a UUID, or a
  document that does not exist, is unreadable.
- ``governance_proposal`` — the ``ActionRequest`` exists and the reader
  is a member of its governance tenant, checked through
  ``app.platform.modules.module_gate`` (legacy membership AND the
  mirrored platform membership, enforce mode). The ref is the request id.
- ``governance_decision`` — the ref is the *request* id of the decision
  (a governance decision is keyed by ``(tenant_id, request_id,
  actor_id)``); readable when at least one decision exists for that
  request and the reader is a member of the request's tenant, same gate
  as proposals.
- ``continuity_drill`` — the drill run (``bc_runs``) exists and the
  reader may see its tenant, checked through
  ``app.continuity.access.scope_for`` (the continuity module's own
  check; a non-member is 404 there, hence unreadable here).
- ``audit_event`` — the audit row exists and the reader is a legacy
  administrator: audit logs are an admin-only surface in this codebase
  (``GET /api/admin/providers/audit-logs`` requires the admin role).
  The ref is the audit row id (an integer, as a string).
- ``external`` — a URL the decider deliberately cited as a public
  reference. Readable by anyone who may read the decision at all: the
  caller must have required ``decision.read`` on the decision's scope
  before calling this function, so the URL is never shown to a
  non-reader. The URL itself is the decider's own words, not company
  data fetched from elsewhere.
- ``simulation`` / ``twin_snapshot`` / ``recovery_plan`` — the owning
  modules (Step AL what-if, Step S digital twin, Step AH resilience)
  live on unmerged sibling branches and have no tables in this
  codebase, so a cited item cannot be verified as readable here.
  These refs are always hidden (deny by default). Slice 4 added the
  citation path (``record_from_simulation`` stores the ref as cited);
  the readability check itself is wired when the owning step merges.
  This is recorded, not silent: the hidden count still tells the
  reader something was withheld.

Callers must have required ``decision.read`` on the decision's scope
first (the service routes do this through ``require()``); this function
only refines *which evidence* inside an already-readable decision the
reader may see. It never grants access to the decision itself.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.continuity import access as continuity_access
from app.continuity.models import Run as ContinuityRun
from app.core.errors import NotFoundError
from app.db.models import ROLE_ADMIN, AuditLog, Document, User
from app.decision_memory.records import EvidenceRef
from app.governance import models as governance_models
from app.platform.modules import module_gate
from app.rag.access import readable_by

__all__ = ["readable_evidence"]

# Evidence kinds whose owning module has no tables in this codebase yet
# (unmerged sibling branches). Always hidden; see the module docstring.
UNWIRED_KINDS = frozenset({"simulation", "twin_snapshot", "recovery_plan"})


def _parse_uuids(refs: list[str]) -> dict[str, uuid.UUID]:
    """Refs that parse as UUIDs, keyed by the original ref string."""
    parsed: dict[str, uuid.UUID] = {}
    for ref in refs:
        try:
            parsed[ref] = uuid.UUID(ref)
        except (ValueError, AttributeError, TypeError):
            continue
    return parsed


async def _readable_documents(db: AsyncSession, user: User, refs: list[str]) -> set[str]:
    """Document refs the reader may read (RAG access rules)."""
    parsed = _parse_uuids(refs)
    if not parsed:
        return set()
    found = set(await db.scalars(
        select(Document.id).where(Document.id.in_(list(parsed.values())), readable_by(user))))
    return {ref for ref, doc_id in parsed.items() if doc_id in found}


async def _governance_tenant_allowed(db: AsyncSession, user: User, tenant_id: uuid.UUID) -> bool:
    """True when the reader is a member of a governance tenant.

    Legacy membership (a ``security_members`` row) AND the mirrored
    platform membership, via the module gate in enforce mode.
    """
    legacy = await db.scalar(select(1).where(
        governance_models.Member.tenant_id == tenant_id,
        governance_models.Member.user_id == user.id))
    if legacy is None:
        return False
    gate = await module_gate(db, "governance", tenant_id, user, legacy_allowed=True)
    return gate.allowed


async def _readable_governance_proposals(db: AsyncSession, user: User, refs: list[str]) -> set[str]:
    parsed = _parse_uuids(refs)
    if not parsed:
        return set()
    rows = await db.execute(select(
        governance_models.ActionRequest.id, governance_models.ActionRequest.tenant_id
    ).where(governance_models.ActionRequest.id.in_(list(parsed.values()))))
    by_tenant: dict[uuid.UUID, list[str]] = {}
    for request_id, tenant_id in rows:
        by_tenant.setdefault(tenant_id, []).append(str(request_id))
    readable: set[str] = set()
    for tenant_id, request_refs in by_tenant.items():
        if await _governance_tenant_allowed(db, user, tenant_id):
            readable.update(request_refs)
    return readable


async def _readable_governance_decisions(db: AsyncSession, user: User, refs: list[str]) -> set[str]:
    """Decision refs (request ids) with a decision row in a readable tenant."""
    parsed = _parse_uuids(refs)
    if not parsed:
        return set()
    rows = await db.execute(select(
        governance_models.Decision.request_id, governance_models.Decision.tenant_id
    ).where(governance_models.Decision.request_id.in_(list(parsed.values()))).distinct())
    by_tenant: dict[uuid.UUID, list[str]] = {}
    for request_id, tenant_id in rows:
        by_tenant.setdefault(tenant_id, []).append(str(request_id))
    readable: set[str] = set()
    for tenant_id, request_refs in by_tenant.items():
        if await _governance_tenant_allowed(db, user, tenant_id):
            readable.update(request_refs)
    return readable


async def _readable_drills(db: AsyncSession, user: User, refs: list[str]) -> set[str]:
    """Drill-run refs whose tenant the reader may see (continuity's own check)."""
    parsed = _parse_uuids(refs)
    if not parsed:
        return set()
    rows = await db.execute(select(
        ContinuityRun.id, ContinuityRun.tenant_id
    ).where(ContinuityRun.id.in_(list(parsed.values()))))
    by_tenant: dict[uuid.UUID, list[str]] = {}
    for run_id, tenant_id in rows:
        by_tenant.setdefault(tenant_id, []).append(str(run_id))
    readable: set[str] = set()
    for tenant_id, run_refs in by_tenant.items():
        try:
            await continuity_access.scope_for(db, tenant_id, user)
        except NotFoundError:
            continue
        readable.update(run_refs)
    return readable


async def _readable_audit_events(db: AsyncSession, user: User, refs: list[str]) -> set[str]:
    """Audit-row refs, readable only by legacy administrators."""
    if user.role != ROLE_ADMIN:
        return set()
    ids: set[int] = set()
    for ref in refs:
        if ref.isdigit():
            ids.add(int(ref))
    if not ids:
        return set()
    found = set(await db.scalars(select(AuditLog.id).where(AuditLog.id.in_(sorted(ids)))))
    return {str(row_id) for row_id in found}


async def readable_evidence(
    db: AsyncSession, *, user: User, refs,
) -> frozenset[tuple[str, str]]:
    """The ``(kind, ref)`` pairs among ``refs`` this reader may read.

    ``refs`` is an iterable of ``EvidenceRef``. The caller must already
    have required ``decision.read`` on the decision's scope; this only
    refines which evidence inside the decision is visible. Unknown
    kinds, unparseable refs and refs that do not resolve are denied.
    """
    by_kind: dict[str, list[str]] = {}
    for ref in refs:
        if isinstance(ref, EvidenceRef):
            by_kind.setdefault(ref.kind, []).append(ref.ref)
    readable: set[tuple[str, str]] = set()
    if "document" in by_kind:
        for ref in await _readable_documents(db, user, by_kind["document"]):
            readable.add(("document", ref))
    if "governance_proposal" in by_kind:
        for ref in await _readable_governance_proposals(db, user, by_kind["governance_proposal"]):
            readable.add(("governance_proposal", ref))
    if "governance_decision" in by_kind:
        for ref in await _readable_governance_decisions(db, user, by_kind["governance_decision"]):
            readable.add(("governance_decision", ref))
    if "continuity_drill" in by_kind:
        for ref in await _readable_drills(db, user, by_kind["continuity_drill"]):
            readable.add(("continuity_drill", ref))
    if "audit_event" in by_kind:
        for ref in await _readable_audit_events(db, user, by_kind["audit_event"]):
            readable.add(("audit_event", ref))
    if "external" in by_kind:
        # A deliberately cited public reference; the caller already
        # required decision.read on the scope (see module docstring).
        for ref in by_kind["external"]:
            readable.add(("external", ref))
    # simulation / twin_snapshot / recovery_plan: unwired kinds stay hidden.
    return frozenset(readable)
