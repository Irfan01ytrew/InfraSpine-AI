"""Step S (slices 5–6): read-only digital-twin API.

Tenant-addressed, like the company-graph API: the caller must be a
member of the graph tenant (``scope_for`` → 404 for unknown tenant or
non-member). The twin is a projection of authorized data — these
endpoints only ever read. No twin endpoint approves, dispatches,
grants access, or pays anything.

Step K v2 (slice 2): read-only twin version + drift history API.

Tenant-addressed, like the Step S twin API: the caller must be a
member of the graph tenant (``scope_for`` → 404 for unknown tenant
or non-member; 401 unauthenticated). These endpoints only ever read
— they approve, dispatch, grant, and pay nothing, and they never
return a stored snapshot: the list returns bounded summaries, and
the drift endpoint returns the stored drift report.

Money is owner-only (plan §4): the drift report's per-currency money
deltas are shown only when the caller's graph role is ``owner``;
other members get the same report with ``money: null`` and
``money_redacted: true``. The version list never carries amounts.

Slice 3 adds the health endpoints below: the latest aggregate
health score, the per-version health history, and the stored watch
alerts. Health assessments and alert detail carry score points and
counts only — no money amounts, no entity names — so they are safe
to show to any tenant member.

Slice 6 adds the twin-driven proposal endpoints below: create a
proposal from a reconciliation finding or a simulation finding,
list / count / read proposals, decide (approve / reject) the
bound action, execute an approved proposal through
``execute_once``, and cancel a pending one. A proposal is a
suggestion until a human approves it; the twin never approves,
dispatches, grants, or pays anything itself.

Slice 7 adds the multi-source trust endpoints (score + band
computed on read from the latest version's source freshness and
drift verdict; per-version trust history) and the conflict
detection endpoint (per-source entity views in, both sides of each
disagreement out; money-kind values redacted for non-owners).
Trust and conflicts carry scores, counts and names only — no money
amounts — so any tenant member may see them.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.company_graph.access import scope_for
from app.core.errors import InvalidInputError, NotFoundError
from app.db.twin_reconciliation_models import TwinReconciliationFinding
from app.digital_twin import conflicts, health_records, proposals, proposals_records, reconciliation_records, trust, versions
from app.digital_twin.service import predict_for_scope, run_whatif_for_scope, twin_overview
from app.digital_twin.whatif import SCENARIO_KINDS, build_scenario
from app.platform.access import principal_for


router = APIRouter(prefix="/api/digital-twin", tags=["digital-twin"])

class WhatIfRequest(BaseModel):
    template_id: str | None = None
    kind: str | None = None
    seed_ids: list[str] = Field(min_length=1)
    horizon_days: int | None = Field(default=None, ge=1)
    rehearse: bool = False


class PredictRequest(BaseModel):
    absent_ids: list[str] = Field(min_length=1)
    horizon_days: int = Field(default=7, ge=1)
    depth: int = Field(default=6, ge=1)


def _scenario_from_request(payload: WhatIfRequest) -> dict:
    """Exactly one of template_id / kind; both missing or both given is
    a 422 from pydantic's point of view, so this raises InvalidInputError
    (also 422) for the ambiguous cases."""
    if bool(payload.template_id) == bool(payload.kind):
        raise InvalidInputError(
            "Provide exactly one of template_id or kind.")
    if payload.template_id is not None:
        try:
            return build_scenario(payload.template_id, payload.seed_ids,
                                  horizon_days=payload.horizon_days)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
    if payload.kind not in SCENARIO_KINDS:
        raise InvalidInputError(
            f"Unknown scenario kind {payload.kind!r}; expected one of {sorted(SCENARIO_KINDS)}.")
    horizon_days = payload.horizon_days if payload.horizon_days is not None else 7
    return {
        "template_id": None,
        "kind": payload.kind,
        "name": payload.kind,
        "seed_ids": list(payload.seed_ids),
        "horizon_days": horizon_days,
    }


@router.get("/{tenant_id}/overview")
async def get_twin_overview(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    continuity_tenant_id: UUID | None = Query(default=None),
):
    """Read-only summary of the live twin for one graph tenant."""
    scope = await scope_for(db, tenant_id, user)
    return await twin_overview(db, scope, continuity_tenant_id=continuity_tenant_id,
                               group_id=group_id)


@router.post("/{tenant_id}/what-if")
async def post_twin_what_if(
    tenant_id: UUID,
    payload: WhatIfRequest,
    db: DbSession,
    user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    continuity_tenant_id: UUID | None = Query(default=None),
    depth: int = Query(default=6, ge=1),
):
    """Run a what-if scenario against the live twin (dry run, read-only).

    The simulation result is deterministic for the same twin
    fingerprint + scenario. Nothing is written, approved, or paid.
    """
    scenario = _scenario_from_request(payload)
    scope = await scope_for(db, tenant_id, user)
    try:
        return await run_whatif_for_scope(
            db, scope, scenario, continuity_tenant_id=continuity_tenant_id,
            group_id=group_id, depth=depth, rehearse=payload.rehearse)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc


@router.post("/{tenant_id}/predict-absence")
async def post_twin_predict_absence(
    tenant_id: UUID,
    payload: PredictRequest,
    db: DbSession,
    user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    continuity_tenant_id: UUID | None = Query(default=None),
):
    """Run a horizon absence prediction against the live twin (read-only)."""
    scope = await scope_for(db, tenant_id, user)
    try:
        return await predict_for_scope(
            db, scope, payload.absent_ids, continuity_tenant_id=continuity_tenant_id,
            group_id=group_id, horizon_days=payload.horizon_days, depth=payload.depth)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc


@router.get("/{tenant_id}/versions")
async def list_twin_versions(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=versions.DEFAULT_RETENTION_VERSIONS, ge=1, le=versions.MAX_LIST_LIMIT),
):
    """Newest-first summaries of recorded twin versions for one tenant.

    Summaries only — no snapshot JSON, no money amounts. Use the
    drift endpoint for the stored drift report of one version.
    """
    scope = await scope_for(db, tenant_id, user)
    rows = await versions.list_versions(db, scope.tenant_id, limit=limit)
    return {"tenant_id": str(scope.tenant_id), "versions": rows, "count": len(rows)}


@router.get("/{tenant_id}/versions/{fingerprint}/drift")
async def get_twin_version_drift(
    tenant_id: UUID,
    fingerprint: str,
    db: DbSession,
    user: CurrentUser,
):
    """Stored drift report for one recorded twin version.

    404 when the tenant is unknown, the caller is not a member, or
    no version with that fingerprint exists for the tenant. The first
    version recorded has no drift (nothing to diff against) and
    returns ``drift: null`` with ``is_first_version: true``. Money
    amounts are shown only to the tenant owner.
    """
    scope = await scope_for(db, tenant_id, user)
    try:
        result = await versions.get_drift(db, scope.tenant_id, fingerprint)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    if result is None:
        raise NotFoundError("Twin version not found.")
    drift_report = result["drift"]
    if scope.role != "owner":
        drift_report = versions.redact_money_for_viewer(drift_report)
    return {
        "tenant_id": str(scope.tenant_id),
        "fingerprint": fingerprint,
        "version": result["version"],
        "drift": drift_report,
        "is_first_version": result["is_first_version"],
    }


@router.get("/{tenant_id}/health")
async def get_twin_health(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Aggregate health score of the latest recorded twin version.

    404 when the tenant is unknown, the caller is not a member, or
    no version has been recorded yet. The assessment carries score
    points and counts only — no money amounts — so any member may
    see it.
    """
    scope = await scope_for(db, tenant_id, user)
    assessment = await health_records.latest_health(db, scope.tenant_id)
    if assessment is None:
        raise NotFoundError("No twin versions recorded for this tenant.")
    return {"tenant_id": str(scope.tenant_id), "health": assessment}


@router.get("/{tenant_id}/health/history")
async def get_twin_health_history(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=versions.DEFAULT_RETENTION_VERSIONS, ge=1, le=health_records.MAX_LIST_LIMIT),
):
    """Per-version health scores, newest first (bounded summaries)."""
    scope = await scope_for(db, tenant_id, user)
    history = await health_records.health_history(db, scope.tenant_id, limit=limit)
    return {"tenant_id": str(scope.tenant_id), "history": history, "count": len(history)}


@router.get("/{tenant_id}/health/alerts")
async def list_twin_health_alerts(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=versions.DEFAULT_RETENTION_VERSIONS, ge=1, le=health_records.MAX_LIST_LIMIT),
):
    """Stored health-watch alerts, newest first.

    Alert detail is score points and counts only — no money amounts,
    no entity names — so any member may see it.
    """
    scope = await scope_for(db, tenant_id, user)
    alerts = await health_records.list_health_alerts(db, scope.tenant_id, limit=limit)
    return {"tenant_id": str(scope.tenant_id), "alerts": alerts, "count": len(alerts)}


def _trust_from_summary(summary: dict) -> dict:
    """Trust report for one stored version summary.

    The summary already carries the version's source freshness and
    its drift verdict, so no snapshot JSON is ever read here.
    """
    drift_report = None
    if summary.get("drift_score") is not None:
        drift_report = {"drift_score": summary["drift_score"], "trust": summary.get("trust")}
    return trust.compute_trust(summary.get("source_freshness") or [], drift_report)


@router.get("/{tenant_id}/trust")
async def get_twin_trust(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Multi-source trust score of the latest recorded twin version.

    Computed on read from the latest version's source freshness
    (weighted per source) and its drift verdict; the band is the
    worse of the two. 404 when the tenant is unknown, the caller is
    not a member, or no version has been recorded yet. Scores and
    counts only — no money amounts — so any member may see it.
    """
    scope = await scope_for(db, tenant_id, user)
    rows = await versions.list_versions(db, scope.tenant_id, limit=1)
    if not rows:
        raise NotFoundError("No twin versions recorded for this tenant.")
    report = _trust_from_summary(rows[0])
    report["fingerprint"] = rows[0]["fingerprint"]
    return {"tenant_id": str(scope.tenant_id), "trust": report}


@router.get("/{tenant_id}/trust/history")
async def get_twin_trust_history(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=versions.DEFAULT_RETENTION_VERSIONS, ge=1, le=versions.MAX_LIST_LIMIT),
):
    """Per-version trust scores, newest first (bounded summaries)."""
    scope = await scope_for(db, tenant_id, user)
    rows = await versions.list_versions(db, scope.tenant_id, limit=limit)
    history = []
    for row in rows:
        report = _trust_from_summary(row)
        report["fingerprint"] = row["fingerprint"]
        report["as_of"] = row["as_of"]
        history.append(report)
    return {"tenant_id": str(scope.tenant_id), "history": history, "count": len(history)}


class ConflictDetectBody(BaseModel):
    source_views: list


@router.post("/{tenant_id}/conflicts/detect")
async def detect_twin_conflicts(
    tenant_id: UUID,
    body: ConflictDetectBody,
    db: DbSession,
    user: CurrentUser,
):
    """Detect cross-source disagreements about the same entity field.

    The caller supplies per-source entity views; the response lists
    each disagreement with both sides. An entity present in one
    source only is not a conflict. Money-kind values are shown only
    to the tenant owner; other members see the disagreement with the
    values redacted.
    """
    scope = await scope_for(db, tenant_id, user)
    try:
        result = conflicts.detect_conflicts(body.source_views)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    result = conflicts.redact_conflicts_for_viewer(result, is_owner=scope.role == "owner")
    return {"tenant_id": str(scope.tenant_id), "conflicts": result}


@router.get("/{tenant_id}/reconciliation/findings")
async def list_twin_reconciliation_findings(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=30, ge=1, le=reconciliation_records.MAX_LIST_LIMIT),
    acknowledged: bool | None = Query(default=None),
    severity: str | None = Query(default=None),
):
    """Stored drill-reconciliation findings (the review queue), newest first.

    A finding records one disagreement between a twin simulation
    and a real Step G drill. Acknowledging a finding only marks
    the queue row as seen — it never changes the twin or rewrites
    a drill record. Money differences are shown only to the tenant
    owner; other members get the same finding with
    ``money_difference: null`` and ``money_redacted: true``.
    """
    scope = await scope_for(db, tenant_id, user)
    try:
        findings = await reconciliation_records.list_findings(
            db, scope.tenant_id, limit=limit,
            acknowledged=acknowledged, severity=severity)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    if scope.role != "owner":
        findings = [reconciliation_records.redact_money_for_viewer(item) for item in findings]
    return {"tenant_id": str(scope.tenant_id), "findings": findings, "count": len(findings)}


@router.get("/{tenant_id}/reconciliation/counts")
async def get_twin_reconciliation_counts(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Review-queue counts for one tenant (for the queue badge)."""
    scope = await scope_for(db, tenant_id, user)
    counts = await reconciliation_records.reconciliation_counts(db, scope.tenant_id)
    return {"tenant_id": str(scope.tenant_id), "counts": counts}


@router.post("/{tenant_id}/reconciliation/findings/{finding_id}/acknowledge")
async def acknowledge_twin_reconciliation_finding(
    tenant_id: UUID,
    finding_id: UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Mark one review-queue finding as seen.

    Only the queue row changes (acknowledged / when / by whom).
    The finding, the twin, and the drill record are untouched, and
    acknowledging approves, dispatches, or applies nothing.
    """
    scope = await scope_for(db, tenant_id, user)
    summary = await reconciliation_records.acknowledge_finding(
        db, scope.tenant_id, finding_id, by=user.id)
    if summary is None:
        raise NotFoundError("Reconciliation finding not found.")
    await db.commit()
    if scope.role != "owner":
        summary = reconciliation_records.redact_money_for_viewer(summary)
    return {"tenant_id": str(scope.tenant_id), "finding": summary}


class ProposalCreateBody(BaseModel):
    """Create a proposal from exactly one finding.

    ``finding_id`` names a stored reconciliation finding;
    ``simulation_finding`` is a caller-built simulation finding
    (today only ``backup_permission_gap`` — see
    ``validate_simulation_finding``). Exactly one must be given.
    """

    finding_id: UUID | None = None
    simulation_finding: dict | None = None
    action_kind: str


class ProposalDecideBody(BaseModel):
    """Decide a pending proposal.

    ``presented_hash`` is the action hash the approver reviewed;
    the decision applies only to that exact action.
    """

    decision: str
    presented_hash: str
    note: str = ""


def _finding_from_row(row):
    """A stored reconciliation finding as a proposal finding dict."""
    return {
        "source": "reconciliation",
        "code": row.code,
        "resource_id": row.resource_id,
        "severity": row.severity,
        "fingerprint": row.fingerprint,
        "money": row.money_difference,
        "detail": row.detail,
    }


def _redact_proposal(scope, summary):
    return summary if scope.role == "owner" else proposals_records.redact_money_for_viewer(summary)


@router.post("/{tenant_id}/proposals")
async def create_twin_proposal(
    tenant_id: UUID,
    body: ProposalCreateBody,
    db: DbSession,
    user: CurrentUser,
):
    """Propose an action for one twin finding.

    Creates the proposal row and requests the platform approval
    bound to the exact action (24 h lifetime, single-use). The
    proposal is pending until a holder of the required
    permission approves it. Re-proposing the same finding for
    the same action returns the existing proposal.
    """
    scope = await scope_for(db, tenant_id, user)
    if (body.finding_id is None) == (body.simulation_finding is None):
        raise InvalidInputError("Give exactly one of finding_id or simulation_finding.")
    if body.finding_id is not None:
        row = await db.scalar(
            select(TwinReconciliationFinding).where(
                TwinReconciliationFinding.tenant_id == scope.tenant_id,
                TwinReconciliationFinding.id == body.finding_id,
            )
        )
        if row is None:
            raise NotFoundError("Reconciliation finding not found.")
        finding = _finding_from_row(row)
    else:
        try:
            finding = proposals.validate_simulation_finding(body.simulation_finding)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
    try:
        result = await proposals_records.create_proposal(
            db, principal_for(user), scope.tenant_id,
            finding=finding, action_kind=body.action_kind)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    return {
        "tenant_id": str(scope.tenant_id),
        "proposal": _redact_proposal(scope, result["proposal"]),
        "created": result["created"],
        "approval_id": result["approval_id"],
    }


@router.get("/{tenant_id}/proposals")
async def list_twin_proposals(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=30, ge=1, le=proposals_records.MAX_LIST_LIMIT),
    status: str | None = Query(default=None),
    action_type: str | None = Query(default=None),
):
    """Stored proposals, newest first.

    The finding's money difference is shown only to the tenant
    owner; other members get the same proposal with the money
    hidden.
    """
    scope = await scope_for(db, tenant_id, user)
    try:
        items = await proposals_records.list_proposals(
            db, scope.tenant_id, limit=limit, status=status, action_type=action_type)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    items = [_redact_proposal(scope, item) for item in items]
    return {"tenant_id": str(scope.tenant_id), "proposals": items, "count": len(items)}


@router.get("/{tenant_id}/proposals/counts")
async def get_twin_proposal_counts(
    tenant_id: UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Proposal inbox counts for one tenant."""
    scope = await scope_for(db, tenant_id, user)
    counts = await proposals_records.proposal_counts(db, scope.tenant_id)
    return {"tenant_id": str(scope.tenant_id), "counts": counts}


@router.get("/{tenant_id}/proposals/{proposal_id}")
async def get_twin_proposal(
    tenant_id: UUID,
    proposal_id: UUID,
    db: DbSession,
    user: CurrentUser,
):
    """One stored proposal."""
    scope = await scope_for(db, tenant_id, user)
    summary = await proposals_records.get_proposal(db, scope.tenant_id, proposal_id)
    if summary is None:
        raise NotFoundError("Twin proposal not found.")
    return {"tenant_id": str(scope.tenant_id),
            "proposal": _redact_proposal(scope, summary)}


@router.post("/{tenant_id}/proposals/{proposal_id}/decide")
async def decide_twin_proposal(
    tenant_id: UUID,
    proposal_id: UUID,
    body: ProposalDecideBody,
    db: DbSession,
    user: CurrentUser,
):
    """Approve or reject a pending proposal.

    The approver must hold the proposal's required permission
    and may not be the proposer; the decision applies only to
    the presented action hash.
    """
    scope = await scope_for(db, tenant_id, user)
    if body.decision not in ("approve", "reject"):
        raise InvalidInputError("Decision must be approve or reject.")
    try:
        summary = await proposals_records.decide_proposal(
            db, principal_for(user), scope.tenant_id, proposal_id,
            decision=body.decision, presented_hash=body.presented_hash, note=body.note)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    if summary is None:
        raise NotFoundError("Twin proposal not found.")
    return {"tenant_id": str(scope.tenant_id),
            "proposal": _redact_proposal(scope, summary)}


@router.post("/{tenant_id}/proposals/{proposal_id}/execute")
async def execute_twin_proposal(
    tenant_id: UUID,
    proposal_id: UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Dispatch an approved proposal through ``execute_once``.

    The executor may be neither the proposer nor the approver.
    A re-execute replays the stored result instead of
    dispatching again.
    """
    scope = await scope_for(db, tenant_id, user)
    try:
        result = await proposals_records.execute_proposal(
            db, principal_for(user), scope.tenant_id, proposal_id)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    if result is None:
        raise NotFoundError("Twin proposal not found.")
    result["proposal"] = _redact_proposal(scope, result["proposal"])
    result["tenant_id"] = str(scope.tenant_id)
    return result


@router.post("/{tenant_id}/proposals/{proposal_id}/cancel")
async def cancel_twin_proposal(
    tenant_id: UUID,
    proposal_id: UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Cancel a draft / pending proposal (proposer only)."""
    scope = await scope_for(db, tenant_id, user)
    summary = await proposals_records.cancel_proposal(
        db, principal_for(user), scope.tenant_id, proposal_id)
    if summary is None:
        raise NotFoundError("Twin proposal not found.")
    return {"tenant_id": str(scope.tenant_id),
            "proposal": _redact_proposal(scope, summary)}
