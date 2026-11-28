"""Decision memory API (Step M, slice 3): decision records per scope.

Authorisation goes through ``app/platform`` on the scope in the path,
never through the legacy role: ``decision.record`` records / supersedes /
revokes (and requests the approval a sensitive decision needs),
``decision.read`` reads. A user who is not a member of the scope gets
404; a member without the permission gets 403. Unauthenticated callers
get 401 from the session dependency.

Evidence is references only, never copied text. Every read filters the
evidence through ``readable_evidence``: a reader sees only the evidence
they may read, plus a count of how many references were hidden — the
label of a hidden reference is never revealed. Write responses echo the
record exactly as submitted (the writer already knows what they cited);
per-reader filtering applies on GET.

Decisions are records, never permissions: no endpoint here grants
access, changes an approval, moves money or changes a payee. Sensitive
kinds (finance, supplier, people) need a consumed platform approval
bound to the exact action — request one first at
``POST /scopes/{scope_id}/approvals`` (as one person), have someone else
approve it through the platform approvals API, then record with the
approval id. History is append-only: there is no update or delete.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentUser, DbSession, audit_context
from app.core.errors import InvalidInputError
from app.platform.models import PlatformScope
from app.decision_memory.capture import (
    list_decisions_for_drill,
    record_from_drill,
    record_from_governance,
    record_from_simulation,
)
from app.decision_memory.readable import readable_evidence
from app.decision_memory.records import DECISION_KINDS, EVIDENCE_KINDS, EvidenceRef
from app.decision_memory.review import (
    REVIEW_SOON_DAYS,
    list_due_for_review,
    review_decision,
    review_digest,
)
from app.decision_memory.service import (
    PERMISSION_READ,
    PERMISSION_RECORD,
    DecisionView,
    decision_dependencies,
    decision_view,
    get_decision,
    list_decisions,
    record_decision,
    request_decision_approval,
    revoke_decision,
    search_decisions,
    supersede_decision,
    verify_fingerprint,
)
from app.platform.access import ensure_role, principal_for, require, visible_scopes
from app.services.audit import record

router = APIRouter(prefix="/api/decisions", tags=["decisions"])

DECISION_ROLES = {
    "decisions.admin": {"decision.record", "decision.read", "decision.approve"},
    "decisions.recorder": {"decision.record", "decision.read"},
    "decisions.reader": {"decision.read"},
}


async def ensure_decision_roles(db) -> None:
    """Create the decision roles if they are missing (grants nothing by itself).

    Called once at API startup (see the lifespan in ``app/main.py``) so the
    roles exist for administrators to grant; memberships are granted
    separately, e.g. by an administrator through the platform admin API.
    Separation of duties is enforced at decision time (an approver cannot
    approve their own request), not by these roles.
    """
    for key, permissions in DECISION_ROLES.items():
        await ensure_role(db, key, permissions)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceIn(Strict):
    kind: Literal["document", "governance_proposal", "governance_decision", "simulation",
                  "twin_snapshot", "continuity_drill", "recovery_plan", "audit_event", "external"]
    ref: str = Field(min_length=1, max_length=512)
    label: str = Field(min_length=1, max_length=200)


class DecisionIn(Strict):
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=4000)
    kind: Literal["policy", "finance", "operations", "supplier", "people", "technology", "other"] = "other"
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    alternatives_considered: list[str] = Field(default=[])
    evidence: list[EvidenceIn] = Field(default=[])
    depends_on: list[UUID] = Field(default=[])
    review_at: date | None = None
    decision_id: UUID | None = None
    approval_id: UUID | None = None


class RevokeIn(Strict):
    reason: str = Field(min_length=1, max_length=500)
    approval_id: UUID | None = None


class GovernanceCaptureIn(DecisionIn):
    """Record a decision executed by an approved governance decision."""

    request_id: UUID


class DrillCaptureIn(DecisionIn):
    """Record a decision taken during a continuity drill run."""

    run_id: UUID


class SimulationCaptureIn(DecisionIn):
    """Record a decision citing a what-if simulation result as evidence."""

    simulation_id: str = Field(min_length=1, max_length=512)
    simulation_label: str | None = Field(default=None, max_length=200)


class ReviewIn(Strict):
    """Review an active decision: confirm it, expire it, or delegate to supersede/revoke.

    ``reason`` is the reviewer's note (1–500 chars) and is stored on the
    lifecycle event. A superseding review also needs the new decision's
    title and rationale; an expiring review of a sensitive kind needs a
    consumed approval id for the exact ``decision.expire`` action.
    """

    outcome: Literal["confirm", "expire", "supersede", "revoke"]
    reason: str = Field(min_length=1, max_length=500)
    review_at: date | None = None
    title: str | None = Field(default=None, max_length=200)
    rationale: str | None = Field(default=None, max_length=4000)
    kind: Literal["policy", "finance", "operations", "supplier", "people", "technology", "other"] = "other"
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    alternatives_considered: list[str] = Field(default=[])
    evidence: list[EvidenceIn] = Field(default=[])
    depends_on: list[UUID] = Field(default=[])
    decision_id: UUID | None = None
    approval_id: UUID | None = None


class ApprovalRequestIn(Strict):
    action_type: Literal["decision.record", "decision.supersede", "decision.revoke", "decision.expire"]
    decision_id: UUID
    kind: Literal["policy", "finance", "operations", "supplier", "people", "technology", "other"]
    superseded_id: UUID | None = None
    ttl_seconds: int | None = Field(default=None, ge=60, le=604800)


def _evidence_refs(body: list[EvidenceIn]) -> tuple[EvidenceRef, ...]:
    return tuple(EvidenceRef(kind=item.kind, ref=item.ref, label=item.label) for item in body)


def _evidence_out(item) -> dict:
    if isinstance(item, dict):
        return {"kind": item["kind"], "ref": item["ref"], "label": item["label"]}
    return {"kind": item.kind, "ref": item.ref, "label": item.label}


def _decision_out(row, *, evidence, hidden_evidence: int) -> dict:
    return {
        "id": str(row.id),
        "scope_id": str(row.scope_id),
        "title": row.title,
        "rationale": row.rationale,
        "kind": row.kind,
        "status": row.status,
        "decided_by": str(row.decided_by),
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "alternatives_considered": list(row.alternatives_considered or []),
        "evidence": [_evidence_out(item) for item in evidence],
        "hidden_evidence": hidden_evidence,
        "supersedes": [str(item) for item in (row.supersedes or [])],
        "depends_on": [str(item) for item in (row.depends_on or [])],
        "review_at": row.review_at.isoformat() if row.review_at else None,
        "superseded_by": str(row.superseded_by) if row.superseded_by else None,
        "superseded_at": row.superseded_at.isoformat() if row.superseded_at else None,
        "needs_review": bool(row.needs_review),
        "fingerprint": row.fingerprint,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _view_out(view: DecisionView) -> dict:
    return _decision_out(view.row, evidence=view.visible_evidence, hidden_evidence=view.hidden_evidence)


async def _readable_for(db, user, rows) -> frozenset:
    """The readable evidence set for the union of refs on these rows."""
    refs: list[EvidenceRef] = []
    for row in rows:
        for item in (row.evidence or []):
            if isinstance(item, dict) and item.get("kind") in EVIDENCE_KINDS:
                refs.append(EvidenceRef(kind=item["kind"], ref=item["ref"], label=item["label"]))
    return await readable_evidence(db, user=user, refs=refs)


async def _view_for(db, user, principal, *, scope_id: UUID, decision_id: UUID) -> DecisionView:
    """One decision with evidence filtered for this reader.

    Loads the record first (enforcing ``decision.read`` and 404), then
    computes what the reader may read and loads it again filtered.
    """
    first = await get_decision(db, principal, scope_id=scope_id, decision_id=str(decision_id))
    if not first.record.evidence:
        return first
    readable = await readable_evidence(db, user=user, refs=first.record.evidence)
    return await get_decision(db, principal, scope_id=scope_id, decision_id=str(decision_id),
                              readable_evidence=readable)


async def _record_decision(db, principal, *, scope_id: UUID, body: DecisionIn):
    return await record_decision(
        db, principal, scope_id=scope_id, title=body.title, rationale=body.rationale, kind=body.kind,
        decided_by=body.decided_by, decided_at=body.decided_at,
        alternatives_considered=tuple(body.alternatives_considered),
        evidence=_evidence_refs(body.evidence),
        depends_on=tuple(str(item) for item in body.depends_on),
        review_at=body.review_at,
        decision_id=str(body.decision_id) if body.decision_id else None,
        approval_id=body.approval_id,
    )


@router.get("/scopes")
async def list_readable_scopes(db: DbSession, user: CurrentUser):
    """Scopes where the caller holds ``decision.read`` — the scope picker for the UI.

    Only scopes the caller already holds the permission on are returned
    (org first, then by name); a caller with no decision scope gets an
    empty list, never a 404. The caller learns nothing about scopes
    they cannot read.
    """
    from sqlalchemy import select

    principal = principal_for(user)
    visible = await visible_scopes(db, principal, PERMISSION_READ)
    if not visible.scope_ids:
        return {"scopes": []}
    rows = await db.scalars(
        select(PlatformScope)
        .where(PlatformScope.id.in_(visible.scope_ids), PlatformScope.active.is_(True))
        .order_by(PlatformScope.kind != "org", PlatformScope.name))
    return {"scopes": [{"id": str(row.id), "kind": row.kind, "name": row.name} for row in rows]}


@router.post("/scopes/{scope_id}", status_code=201)
async def create_decision(db: DbSession, user: CurrentUser,
                          scope_id: UUID, body: DecisionIn):
    """Record a decision. Sensitive kinds need a consumed approval id."""
    principal = principal_for(user)
    row = await _record_decision(db, principal, scope_id=scope_id, body=body)
    # The service already writes the one audit row for this lifecycle
    # event (decisions.recorded); the route adds none.
    await db.commit()
    await db.refresh(row)
    # Echo the record as submitted; per-reader filtering applies on GET.
    return _decision_out(row, evidence=row.evidence or [], hidden_evidence=0)


@router.post("/scopes/{scope_id}/capture/governance", status_code=201)
async def capture_from_governance(db: DbSession, user: CurrentUser,
                                  scope_id: UUID, body: GovernanceCaptureIn):
    """Record a decision executed by an approved governance decision.

    The request must have been approved (status ready/executed with an
    approve decision) and the caller must belong to its governance
    tenant. Sensitive kinds need a consumed approval id, as usual.
    Nothing here is automatic — the capture is an explicit call.
    """
    principal = principal_for(user)
    row = await record_from_governance(
        db, principal, scope_id=scope_id, request_id=body.request_id,
        title=body.title, rationale=body.rationale, kind=body.kind,
        decided_by=body.decided_by, decided_at=body.decided_at,
        alternatives_considered=tuple(body.alternatives_considered),
        evidence=_evidence_refs(body.evidence),
        depends_on=tuple(str(item) for item in body.depends_on),
        review_at=body.review_at,
        decision_id=str(body.decision_id) if body.decision_id else None,
        approval_id=body.approval_id,
    )
    await db.commit()
    await db.refresh(row)
    return _decision_out(row, evidence=row.evidence or [], hidden_evidence=0)


@router.post("/scopes/{scope_id}/capture/drill", status_code=201)
async def capture_from_drill(db: DbSession, user: CurrentUser,
                             scope_id: UUID, body: DrillCaptureIn):
    """Record a decision taken during a continuity drill run.

    The run must be a drill that has started, and the caller must be
    able to see its tenant. Sensitive kinds need a consumed approval
    id, as usual.
    """
    principal = principal_for(user)
    row = await record_from_drill(
        db, principal, scope_id=scope_id, run_id=body.run_id,
        title=body.title, rationale=body.rationale, kind=body.kind,
        decided_by=body.decided_by, decided_at=body.decided_at,
        alternatives_considered=tuple(body.alternatives_considered),
        evidence=_evidence_refs(body.evidence),
        depends_on=tuple(str(item) for item in body.depends_on),
        review_at=body.review_at,
        decision_id=str(body.decision_id) if body.decision_id else None,
        approval_id=body.approval_id,
    )
    await db.commit()
    await db.refresh(row)
    return _decision_out(row, evidence=row.evidence or [], hidden_evidence=0)


@router.post("/scopes/{scope_id}/capture/simulation", status_code=201)
async def capture_from_simulation(db: DbSession, user: CurrentUser,
                                  scope_id: UUID, body: SimulationCaptureIn):
    """Record a decision citing a what-if simulation result as evidence.

    The simulation id cannot be verified here (Step AL is unmerged);
    the ref is stored as cited and stays hidden from readers until
    AL merges. Sensitive kinds need a consumed approval id, as usual.
    """
    principal = principal_for(user)
    row = await record_from_simulation(
        db, principal, scope_id=scope_id, simulation_id=body.simulation_id,
        simulation_label=body.simulation_label,
        title=body.title, rationale=body.rationale, kind=body.kind,
        decided_by=body.decided_by, decided_at=body.decided_at,
        alternatives_considered=tuple(body.alternatives_considered),
        evidence=_evidence_refs(body.evidence),
        depends_on=tuple(str(item) for item in body.depends_on),
        review_at=body.review_at,
        decision_id=str(body.decision_id) if body.decision_id else None,
        approval_id=body.approval_id,
    )
    await db.commit()
    await db.refresh(row)
    return _decision_out(row, evidence=row.evidence or [], hidden_evidence=0)


@router.get("/scopes/{scope_id}/drills/{run_id}/decisions")
async def list_drill_decisions(db: DbSession, user: CurrentUser,
                               scope_id: UUID, run_id: UUID,
                               include_archived: bool = False,
                               limit: int = Query(default=50, ge=1, le=200)):
    """Decisions in a scope that cite a drill run, newest first."""
    principal = principal_for(user)
    rows = await list_decisions_for_drill(db, principal, scope_id=scope_id, run_id=run_id,
                                          include_archived=include_archived, limit=limit)
    readable = await _readable_for(db, user, rows)
    return {"run_id": str(run_id),
            "decisions": [_view_out(decision_view(row, readable)) for row in rows]}


@router.get("/scopes/{scope_id}/reviews/due")
async def list_due_reviews(db: DbSession, user: CurrentUser, scope_id: UUID,
                           as_of: date | None = None,
                           days_ahead: int = Query(default=REVIEW_SOON_DAYS, ge=0, le=365),
                           limit: int = Query(default=50, ge=1, le=200)):
    """Active decisions whose review date falls within the window, most overdue first."""
    principal = principal_for(user)
    rows = await list_due_for_review(db, principal, scope_id=scope_id, as_of=as_of,
                                     days_ahead=days_ahead, limit=limit)
    readable = await _readable_for(db, user, rows)
    anchor = as_of or datetime.now(UTC).date()
    return {"scope_id": str(scope_id), "as_of": anchor.isoformat(), "days_ahead": days_ahead,
            "due": [_view_out(decision_view(row, readable)) for row in rows]}


@router.get("/scopes/{scope_id}/reviews/digest")
async def get_review_digest(db: DbSession, user: CurrentUser, scope_id: UUID,
                            as_of: date | None = None,
                            days_ahead: int = Query(default=REVIEW_SOON_DAYS, ge=0, le=365),
                            limit: int = Query(default=50, ge=1, le=200)):
    """Review counts + due rows for the scope — the summary a scheduled report consumes."""
    principal = principal_for(user)
    digest = await review_digest(db, principal, scope_id=scope_id, as_of=as_of,
                                 days_ahead=days_ahead, limit=limit)
    readable = await _readable_for(db, user, digest.due)
    return {"scope_id": str(digest.scope_id), "as_of": digest.as_of.isoformat(),
            "days_ahead": digest.days_ahead, "overdue_count": digest.overdue_count,
            "due_soon_count": digest.due_soon_count, "total_active": digest.total_active,
            "needs_review_count": digest.needs_review_count,
            "due": [_view_out(decision_view(row, readable)) for row in digest.due]}


@router.post("/scopes/{scope_id}/approvals", status_code=201)
async def request_approval(request: Request, db: DbSession, user: CurrentUser,
                           scope_id: UUID, body: ApprovalRequestIn):
    """Request the owner approval a sensitive decision needs.

    The requester needs ``decision.record`` on the scope; the approver
    (someone else, holding ``decision.approve``) decides through the
    platform approvals API. The returned ``action_hash`` is what the
    approver must be shown. This call commits by design, like the
    platform's own approval request.
    """
    principal = principal_for(user)
    await require(db, principal, PERMISSION_RECORD, scope_id)
    req = await request_decision_approval(
        db, principal, action_type=body.action_type, scope_id=scope_id,
        decision_id=str(body.decision_id), kind=body.kind,
        superseded_id=str(body.superseded_id) if body.superseded_id else None,
        ttl=timedelta(seconds=body.ttl_seconds) if body.ttl_seconds is not None else None,
    )
    record(db, audit_context(request, user), "decisions.approval_requested", target=str(req.id),
           details={"action_type": body.action_type, "decision_id": str(body.decision_id)})
    return {
        "approval_id": str(req.id),
        "action_hash": req.action_hash,
        "status": req.status,
        "expires_at": req.expires_at.isoformat() if req.expires_at else None,
    }


@router.get("/scopes/{scope_id}")
async def list_scope_decisions(db: DbSession, user: CurrentUser, scope_id: UUID,
                               kind: str | None = None,
                               status: str | None = None,
                               decided_by: UUID | None = None,
                               decided_after: datetime | None = None,
                               decided_before: datetime | None = None,
                               include_archived: bool = False,
                               limit: int = Query(default=50, ge=1, le=200)):
    """List decisions in a scope, newest first, evidence filtered per reader."""
    principal = principal_for(user)
    if kind is not None and kind not in DECISION_KINDS:
        raise InvalidInputError(f"unknown decision kind: {kind}")
    if status is not None and status not in ("active", "superseded", "revoked", "expired"):
        raise InvalidInputError(f"unknown decision status: {status}")
    rows = await list_decisions(db, principal, scope_id=scope_id, kind=kind, status=status,
                                decided_by=decided_by, decided_after=decided_after,
                                decided_before=decided_before, include_archived=include_archived,
                                limit=limit)
    readable = await _readable_for(db, user, rows)
    return {"decisions": [_view_out(decision_view(row, readable)) for row in rows]}


@router.get("/scopes/{scope_id}/why")
async def why_decisions(db: DbSession, user: CurrentUser, scope_id: UUID,
                        subject: str = Query(min_length=1, max_length=200),
                        include_archived: bool = False,
                        limit: int = Query(default=50, ge=1, le=200)):
    """Decisions mentioning a subject — "why did we decide this?"."""
    principal = principal_for(user)
    rows = await search_decisions(db, principal, scope_id=scope_id, subject=subject,
                                  include_archived=include_archived, limit=limit)
    readable = await _readable_for(db, user, rows)
    return {"subject": subject, "decisions": [_view_out(decision_view(row, readable)) for row in rows]}


@router.get("/scopes/{scope_id}/{decision_id}")
async def get_scope_decision(db: DbSession, user: CurrentUser, scope_id: UUID, decision_id: UUID):
    """One decision, evidence filtered for this reader."""
    principal = principal_for(user)
    return _view_out(await _view_for(db, user, principal, scope_id=scope_id, decision_id=decision_id))


@router.get("/scopes/{scope_id}/{decision_id}/dependencies")
async def get_dependencies(db: DbSession, user: CurrentUser, scope_id: UUID, decision_id: UUID):
    """What this decision depends on, what depends on it, supersession links."""
    principal = principal_for(user)
    deps = await decision_dependencies(db, principal, scope_id=scope_id, decision_id=str(decision_id))
    rows = [deps["record"], *deps["depends_on"], *deps["depended_on_by"], *deps["supersedes"]]
    if deps["superseded_by"] is not None:
        rows.append(deps["superseded_by"])
    readable = await _readable_for(db, user, rows)
    out = {
        "decision": _view_out(decision_view(deps["record"], readable)),
        "depends_on": [_view_out(decision_view(row, readable)) for row in deps["depends_on"]],
        "depended_on_by": [_view_out(decision_view(row, readable)) for row in deps["depended_on_by"]],
        "supersedes": [_view_out(decision_view(row, readable)) for row in deps["supersedes"]],
        "superseded_by": (_view_out(decision_view(deps["superseded_by"], readable))
                          if deps["superseded_by"] is not None else None),
    }
    return out


@router.get("/scopes/{scope_id}/{decision_id}/verify")
async def verify_scope_decision(db: DbSession, user: CurrentUser, scope_id: UUID, decision_id: UUID):
    """Prove the stored fingerprint matches the record as decided."""
    principal = principal_for(user)
    view = await get_decision(db, principal, scope_id=scope_id, decision_id=str(decision_id))
    valid = await verify_fingerprint(db, decision_id=str(decision_id))
    return {"decision_id": str(decision_id), "fingerprint": view.row.fingerprint, "valid": valid}


@router.post("/scopes/{scope_id}/{decision_id}/supersede", status_code=201)
async def supersede_scope_decision(db: DbSession, user: CurrentUser,
                                   scope_id: UUID, decision_id: UUID, body: DecisionIn):
    """Supersede an active decision with a new one, atomically."""
    principal = principal_for(user)
    new_row, _old_row = await supersede_decision(
        db, principal, scope_id=scope_id, superseded_id=str(decision_id), title=body.title,
        rationale=body.rationale, kind=body.kind, decided_by=body.decided_by,
        decided_at=body.decided_at, alternatives_considered=tuple(body.alternatives_considered),
        evidence=_evidence_refs(body.evidence),
        depends_on=tuple(str(item) for item in body.depends_on),
        review_at=body.review_at,
        decision_id=str(body.decision_id) if body.decision_id else None,
        approval_id=body.approval_id,
    )
    # The service already writes the audit rows for this lifecycle event
    # (decisions.recorded + decisions.superseded); the route adds none.
    await db.commit()
    await db.refresh(new_row)
    # Echo the new record as submitted; per-reader filtering applies on GET.
    return _decision_out(new_row, evidence=new_row.evidence or [], hidden_evidence=0)


@router.post("/scopes/{scope_id}/{decision_id}/revoke")
async def revoke_scope_decision(db: DbSession, user: CurrentUser,
                                scope_id: UUID, decision_id: UUID, body: RevokeIn):
    """Revoke an active decision. It stays readable with status ``revoked``."""
    principal = principal_for(user)
    row = await revoke_decision(db, principal, scope_id=scope_id, decision_id=str(decision_id),
                                reason=body.reason, approval_id=body.approval_id)
    # The service already writes the audit row for this lifecycle event
    # (decisions.revoked); the route adds none.
    await db.commit()
    await db.refresh(row)
    # Echo the record as stored; per-reader filtering applies on GET.
    return _decision_out(row, evidence=row.evidence or [], hidden_evidence=0)


@router.post("/scopes/{scope_id}/{decision_id}/review")
async def review_scope_decision(db: DbSession, user: CurrentUser,
                                scope_id: UUID, decision_id: UUID, body: ReviewIn):
    """Review an active decision: confirm it, expire it, supersede it, or revoke it.

    Confirm keeps the decision active with a new review date; expire
    retires it with status ``expired`` (a sensitive kind needs a consumed
    approval for the exact ``decision.expire`` action); supersede and
    revoke delegate to the existing flows with all of their approval
    rules intact. Returns the row the review produced.
    """
    principal = principal_for(user)
    row = await review_decision(
        db, principal, scope_id=scope_id, decision_id=str(decision_id),
        outcome=body.outcome, reason=body.reason, review_at=body.review_at,
        title=body.title, rationale=body.rationale, kind=body.kind,
        decided_by=body.decided_by, decided_at=body.decided_at,
        alternatives_considered=tuple(body.alternatives_considered),
        evidence=_evidence_refs(body.evidence),
        depends_on=tuple(str(item) for item in body.depends_on),
        decision_id_new=str(body.decision_id) if body.decision_id else None,
        approval_id=body.approval_id,
    )
    # The review service already writes the audit row(s) for this
    # lifecycle event; the route adds none.
    await db.commit()
    await db.refresh(row)
    # Echo the record as stored; per-reader filtering applies on GET.
    return _decision_out(row, evidence=row.evidence or [], hidden_evidence=0)
