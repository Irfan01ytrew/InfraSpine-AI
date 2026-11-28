"""Forecast-record + forecast-proposal API (Step L slices 3 + 6).

``GET /api/predictive-continuity/{tenant_id}/forecasts`` lists the
stored forecasts for a graph tenant (newest first, current records
only unless ``include_superseded=true``); ``GET .../forecasts/{id}``
returns one record. Both are pure reads: no writes, no approvals, no
side effects of any kind. Access is graph membership first (404 for
non-members) then the org-scope ``forecasts.read`` permission (403
for members without it), and every forecast body is filtered to what
the caller may see on the graph.

``POST /api/predictive-continuity/{tenant_id}/proposals`` turns one
forecast finding into an approval-gated action proposal: the
finding comes either from a stored forecast record
(``forecast_record_id`` + ``finding_code`` + ``resource_id`` — the
server extracts the finding, so it cannot be fabricated) or as a
caller-built finding dict (``finding`` — validated fail-closed,
exactly like the twin's simulation findings). The proposal requests
a platform approval bound to the exact action (24 h lifetime,
single-use); ``POST .../proposals/{id}/decide`` approves or
rejects it, ``POST .../proposals/{id}/execute`` dispatches it
through ``execute_once`` with read-back verification, and
``POST .../proposals/{id}/cancel`` cancels it. A proposal is a
suggestion until a human approves it — nothing here writes the
graph, grants access, or pays. The finding's money at risk is
shown only to the tenant owner; other members get the same
proposal with the money hidden.

``POST /api/predictive-continuity/{tenant_id}/reconciliations``
compares one stored forecast record with one real Step G drill run
(``forecast_record_id`` + the plain ``drill_comparison`` dict Step G
returns) and stores the report in the review queue; ``GET
.../reconciliations`` lists the queue, ``GET .../reconciliations/counts``
counts it, ``GET .../reconciliations/{id}`` returns one report, and
``POST .../reconciliations/{id}/acknowledge`` marks a report as
seen. A reconciliation never rewrites a drill record and never
auto-applies anything — the only forecast write is the miss
penalty raising the record's drift score. Finding money at risk is
shown only to the tenant owner.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, get_current_user
from app.core.errors import InvalidInputError, NotFoundError
from app.company_graph.access import scope_for
from app.predictive_continuity import proposals, proposals_records, reconciliation_records
from app.predictive_continuity.models import ForecastRecord
from app.predictive_continuity.records import _require_read
from app.predictive_continuity.records import get_forecast as _get_forecast
from app.predictive_continuity.records import list_forecasts as _list_forecasts
from app.platform.access import principal_for

router = APIRouter(
    prefix="/api/predictive-continuity",
    tags=["predictive-continuity"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/{tenant_id}/forecasts")
async def list_forecasts_route(
    tenant_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    include_superseded: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    items = await _list_forecasts(
        db, tenant_id, user,
        include_superseded=include_superseded, limit=limit, offset=offset)
    return {"tenant_id": str(tenant_id), "forecasts": items, "count": len(items)}


@router.get("/{tenant_id}/forecasts/{record_id}")
async def get_forecast_route(
    tenant_id: uuid.UUID,
    record_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    return await _get_forecast(db, tenant_id, record_id, user)


class ProposalCreateBody(BaseModel):
    """Create a proposal from exactly one finding source.

    ``forecast_record_id`` (+ ``finding_code`` + ``resource_id``)
    names a finding extracted from a stored forecast record;
    ``finding`` is a caller-built forecast/calendar finding dict
    (validated fail-closed). Exactly one source must be given.
    """

    forecast_record_id: uuid.UUID | None = None
    finding_code: str | None = None
    resource_id: str | None = None
    finding: dict | None = None
    action_kind: str


class ProposalDecideBody(BaseModel):
    """Decide a pending proposal.

    ``presented_hash`` is the action hash the approver reviewed;
    the decision applies only to that exact action.
    """

    decision: str
    presented_hash: str
    note: str = ""


def _redact_proposal(scope, summary):
    return summary if scope.role == "owner" else proposals_records.redact_money_for_viewer(summary)


@router.post("/{tenant_id}/proposals")
async def create_forecast_proposal(
    tenant_id: uuid.UUID,
    body: ProposalCreateBody,
    db: DbSession,
    user: CurrentUser,
):
    """Propose an action for one forecast finding.

    Creates the proposal row and requests the platform approval
    bound to the exact action (24 h lifetime, single-use). The
    proposal is pending until a holder of the required
    permission approves it. Re-proposing the same finding for
    the same action returns the existing proposal.
    """
    scope = await scope_for(db, tenant_id, user)
    from_record = body.forecast_record_id is not None
    from_finding = body.finding is not None
    if from_record == from_finding:
        raise InvalidInputError("Give exactly one of forecast_record_id or finding.")
    if from_record:
        if not body.finding_code or not body.resource_id:
            raise InvalidInputError(
                "forecast_record_id needs finding_code and resource_id.")
        record = await db.scalar(select(ForecastRecord).where(
            ForecastRecord.tenant_id == scope.tenant_id,
            ForecastRecord.id == body.forecast_record_id,
        ))
        if record is None:
            raise NotFoundError("Forecast record not found.")
        try:
            extracted = proposals.findings_from_forecast(
                record.forecast, tenant_id=scope.tenant_id)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        finding = next(
            (item for item in extracted
             if item["code"] == body.finding_code
             and item["resource_id"] == body.resource_id),
            None,
        )
        if finding is None:
            raise NotFoundError("Finding not found in this forecast record.")
    else:
        try:
            finding = proposals.validate_finding(body.finding)
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
async def list_forecast_proposals(
    tenant_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=30, ge=1, le=proposals_records.MAX_LIST_LIMIT),
    status: str | None = Query(default=None),
    action_type: str | None = Query(default=None),
):
    """Stored proposals, newest first.

    The finding's money at risk is shown only to the tenant
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
async def get_forecast_proposal_counts(
    tenant_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Proposal inbox counts for one tenant."""
    scope = await scope_for(db, tenant_id, user)
    counts = await proposals_records.proposal_counts(db, scope.tenant_id)
    return {"tenant_id": str(scope.tenant_id), "counts": counts}


@router.get("/{tenant_id}/proposals/{proposal_id}")
async def get_forecast_proposal(
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
):
    """One stored proposal."""
    scope = await scope_for(db, tenant_id, user)
    summary = await proposals_records.get_proposal(db, scope.tenant_id, proposal_id)
    if summary is None:
        raise NotFoundError("Forecast proposal not found.")
    return {"tenant_id": str(scope.tenant_id),
            "proposal": _redact_proposal(scope, summary)}


@router.post("/{tenant_id}/proposals/{proposal_id}/decide")
async def decide_forecast_proposal(
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
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
        raise NotFoundError("Forecast proposal not found.")
    return {"tenant_id": str(scope.tenant_id),
            "proposal": _redact_proposal(scope, summary)}


@router.post("/{tenant_id}/proposals/{proposal_id}/execute")
async def execute_forecast_proposal(
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
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
        raise NotFoundError("Forecast proposal not found.")
    result["proposal"] = _redact_proposal(scope, result["proposal"])
    result["tenant_id"] = str(scope.tenant_id)
    return result


@router.post("/{tenant_id}/proposals/{proposal_id}/cancel")
async def cancel_forecast_proposal(
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Cancel a draft / pending proposal (proposer only)."""
    scope = await scope_for(db, tenant_id, user)
    summary = await proposals_records.cancel_proposal(
        db, principal_for(user), scope.tenant_id, proposal_id)
    if summary is None:
        raise NotFoundError("Forecast proposal not found.")
    return {"tenant_id": str(scope.tenant_id),
            "proposal": _redact_proposal(scope, summary)}


# -------------------------------------------------------------------------------- reconciliations

class ReconciliationCreateBody(BaseModel):
    """Reconcile one stored forecast with one Step G drill run.

    ``drill_comparison`` is the plain dict Step G's
    ``drill_comparison`` returns for the run (``run_id``,
    ``drill`` with ``affected_ids``/``recovered_ids``, and the
    ``differences`` list). ``drill_kind`` is the disruption kind
    (``resignation`` / ``absence`` / ``server_outage`` /
    ``database_failure`` / ``supplier_disruption``) when the caller
    knows it; it is display-only.
    """

    forecast_record_id: uuid.UUID
    drill_comparison: dict
    drill_kind: str | None = None


def _redact_reconciliation(scope, summary):
    return summary if scope.role == "owner" else reconciliation_records.redact_money_for_viewer(summary)


@router.post("/{tenant_id}/reconciliations")
async def create_forecast_reconciliation(
    tenant_id: uuid.UUID,
    body: ReconciliationCreateBody,
    db: DbSession,
    user: CurrentUser,
):
    """Compare a stored forecast with a real drill run.

    Runs the pure reconciliation over the stored forecast record
    and the supplied Step G drill comparison, stores the report in
    the review queue (idempotent per drill run + forecast record),
    and folds the miss penalty into the forecast record's drift
    score when the forecast was wrong. Nothing here rewrites a
    drill record or auto-applies a difference.
    """
    scope, _visible_ids = await _require_read(db, tenant_id, user)
    record = await db.scalar(select(ForecastRecord).where(
        ForecastRecord.tenant_id == scope.tenant_id,
        ForecastRecord.id == body.forecast_record_id,
    ))
    if record is None:
        raise NotFoundError("Forecast record not found.")
    try:
        result = await reconciliation_records.evaluate_reconciliation(
            db, scope.tenant_id, forecast_record=record,
            g_comparison=body.drill_comparison, drill_kind=body.drill_kind)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    return {
        "tenant_id": str(scope.tenant_id),
        "reconciliation": _redact_reconciliation(scope, result["recorded"]["reconciliation"]),
        "created": result["recorded"]["created"],
        "matches": result["matches"],
        "counts": result["counts"],
        "adjusted_drift_score": result["adjusted_drift_score"],
        "summary": result["summary"],
    }


@router.get("/{tenant_id}/reconciliations")
async def list_forecast_reconciliations(
    tenant_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=30, ge=1, le=reconciliation_records.MAX_LIST_LIMIT),
    acknowledged: bool | None = Query(default=None),
    severity: str | None = Query(default=None),
    drill_run_id: uuid.UUID | None = Query(default=None),
):
    """Stored reconciliation reports, newest first.

    Finding money at risk is shown only to the tenant owner;
    other members get the same reports with the money hidden.
    """
    scope, _visible_ids = await _require_read(db, tenant_id, user)
    try:
        items = await reconciliation_records.list_reconciliations(
            db, scope.tenant_id, limit=limit, acknowledged=acknowledged,
            severity=severity, drill_run_id=drill_run_id)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    items = [_redact_reconciliation(scope, item) for item in items]
    return {"tenant_id": str(scope.tenant_id), "reconciliations": items, "count": len(items)}


@router.get("/{tenant_id}/reconciliations/counts")
async def get_forecast_reconciliation_counts(
    tenant_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Reconciliation review-queue counts for one tenant."""
    scope, _visible_ids = await _require_read(db, tenant_id, user)
    counts = await reconciliation_records.reconciliation_counts(db, scope.tenant_id)
    return {"tenant_id": str(scope.tenant_id), "counts": counts}


@router.get("/{tenant_id}/reconciliations/{reconciliation_id}")
async def get_forecast_reconciliation(
    tenant_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
):
    """One stored reconciliation report."""
    scope, _visible_ids = await _require_read(db, tenant_id, user)
    summary = await reconciliation_records.get_reconciliation(
        db, scope.tenant_id, reconciliation_id)
    if summary is None:
        raise NotFoundError("Forecast reconciliation not found.")
    return {"tenant_id": str(scope.tenant_id),
            "reconciliation": _redact_reconciliation(scope, summary)}


@router.post("/{tenant_id}/reconciliations/{reconciliation_id}/acknowledge")
async def acknowledge_forecast_reconciliation(
    tenant_id: uuid.UUID,
    reconciliation_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
):
    """Mark one reconciliation report as seen.

    Only the queue row changes (who + when) — the report, the
    forecast, and the drill record are untouched.
    """
    scope, _visible_ids = await _require_read(db, tenant_id, user)
    summary = await reconciliation_records.acknowledge_reconciliation(
        db, scope.tenant_id, reconciliation_id, by=user.id)
    if summary is None:
        raise NotFoundError("Forecast reconciliation not found.")
    return {"tenant_id": str(scope.tenant_id),
            "reconciliation": _redact_reconciliation(scope, summary)}
