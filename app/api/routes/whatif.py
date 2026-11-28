"""Read-only what-if simulation API (Step AL slice 3).

``POST /api/whatif/{tenant_id}/simulate`` runs a hypothetical disruption
against the caller's authorized company-graph projection and returns
impact + a recovery plan. Nothing is executed, dispatched, granted or
paid: the engine is pure functions over a snapshot, and the endpoint
writes nothing except one audit row (the same convention as the
company-graph ``simulate`` endpoint).

Permission projection is enforced by construction. The snapshot is
built from :func:`app.company_graph.access.scope_for` (404 for
non-members, 403 where the module gate denies) plus
``app.company_graph.service.projection`` — the caller's authorized
projection only. A restricted caller therefore only ever simulates over
entities they may see, and hidden nodes never leak, not even as counts
in the money-at-risk totals.
"""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession, audit_context
from app.company_graph import service as graph_service
from app.company_graph.access import scope_for
from app.core.errors import InvalidInputError
from app.services import audit
from app.whatif import adapter, rehearsal, service, simulator

router = APIRouter(prefix="/api/whatif", tags=["whatif"])


class SimulateRequest(BaseModel):
    """Run a scenario from the library (``template_id``) or by kind.

    Exactly one of ``template_id`` / ``kind`` must be given. ``seed_ids``
    are entity ids from the caller's authorized projection. ``rehearse``
    rewrites the recovery plan as numbered practice-only steps.
    """

    template_id: str | None = None
    kind: str | None = None
    seed_ids: list[str] = Field(min_length=1, max_length=20)
    horizon_days: int | None = Field(default=None, ge=1, le=365)
    depth: int = Field(default=6, ge=1, le=12)
    rehearse: bool = False


def _build_scenario(payload: SimulateRequest) -> dict:
    if payload.template_id is not None and payload.kind is not None:
        raise InvalidInputError("Give either template_id or kind, not both.")
    if payload.template_id is not None:
        try:
            return simulator.build_scenario(
                payload.template_id, list(payload.seed_ids),
                horizon_days=payload.horizon_days)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
    if payload.kind is None:
        raise InvalidInputError("Give either template_id or kind.")
    return {
        "kind": payload.kind,
        "seed_ids": list(payload.seed_ids),
        "horizon_days": payload.horizon_days or simulator.DEFAULT_HORIZON_DAYS,
    }


@router.get("/scenarios")
async def list_scenarios():
    """The scenario library: what can be simulated, in plain language.

    No tenant scope: the library itself contains no company data.
    """
    return {"scenarios": simulator.list_templates()}


class StartRehearsalRequest(BaseModel):
    """Start a practice run of a simulated recovery plan.

    Exactly one of ``template_id`` / ``kind`` must be given (same
    rules as simulate). ``title`` is the human label shown in the
    twin panel; ``rehearsed_by_name`` is the optional display name
    recorded next to the user id.
    """

    template_id: str | None = None
    kind: str | None = None
    seed_ids: list[str] = Field(min_length=1, max_length=20)
    horizon_days: int | None = Field(default=None, ge=1, le=365)
    depth: int = Field(default=6, ge=1, le=12)
    title: str | None = Field(default=None, max_length=180)
    rehearsed_by_name: str | None = Field(default=None, max_length=180)


class CheckOffRequest(BaseModel):
    step_number: int = Field(ge=1)
    finding: str | None = Field(default=None, max_length=1000)
    by_name: str | None = Field(default=None, max_length=180)


class SkipRequest(BaseModel):
    step_number: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    by_name: str | None = Field(default=None, max_length=180)


class CompleteRequest(BaseModel):
    summary: str | None = Field(default=None, max_length=2000)
    by_name: str | None = Field(default=None, max_length=180)


class DrillCompareRequest(BaseModel):
    """Compare a Step G drill run with a fresh simulation.

    ``continuity_tenant_id`` is the business-continuity tenant that
    owns the drill run; it may differ from the company-graph tenant
    in the path. Exactly one of ``template_id`` / ``kind`` must be
    given (same rules as simulate).
    """

    continuity_tenant_id: UUID
    template_id: str | None = None
    kind: str | None = None
    seed_ids: list[str] = Field(min_length=1, max_length=20)
    horizon_days: int | None = Field(default=None, ge=1, le=365)
    depth: int = Field(default=6, ge=1, le=12)


@router.post("/{tenant_id}/simulate")
async def simulate_whatif(tenant_id: UUID, payload: SimulateRequest, request: Request,
                           db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    scenario = _build_scenario(payload)
    projection = await graph_service.projection(db, scope, limit=500)
    snapshot = adapter.snapshot_from_projection(projection)
    try:
        result = simulator.simulate(
            snapshot, scenario, depth=payload.depth, rehearse=payload.rehearse)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    audit.record(db, audit_context(request, user), "whatif.simulated", target=str(tenant_id),
                 details={"scenario": scenario.get("kind"), "template_id": scenario.get("template_id"),
                          "roots": len(scenario["seed_ids"]), "rehearse": payload.rehearse})
    await db.commit()
    return {**result, "snapshot": adapter.snapshot_summary(snapshot)}


@router.post("/{tenant_id}/rehearsals", status_code=201)
async def start_rehearsal_endpoint(tenant_id: UUID, payload: StartRehearsalRequest,
                                   request: Request, db: DbSession, user: CurrentUser):
    """Start a practice run of a simulated recovery plan.

    The simulation runs over the caller's authorized projection, the
    recovery plan becomes numbered practice-only steps, and the
    record is stored. Nothing is executed: ``dry_run`` is True and
    ``side_effects`` is empty by construction.
    """
    scope = await scope_for(db, tenant_id, user)
    scenario = _build_scenario(payload)
    projection = await graph_service.projection(db, scope, limit=500)
    snapshot = adapter.snapshot_from_projection(projection)
    try:
        result = simulator.simulate(
            snapshot, scenario, depth=payload.depth, rehearse=True)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    record = rehearsal.start_rehearsal(
        result, rehearsed_by=str(user.id), rehearsed_by_name=payload.rehearsed_by_name,
        title=payload.title)
    saved = await service.save_rehearsal(db, tenant_id, record)
    audit.record(db, audit_context(request, user), "whatif.rehearsal_started",
                 target=str(tenant_id),
                 details={"rehearsal_id": saved["id"], "scenario": scenario.get("kind"),
                          "template_id": scenario.get("template_id"),
                          "steps": len(saved["steps"])})
    await db.commit()
    return saved


@router.get("/{tenant_id}/rehearsals")
async def list_rehearsals_endpoint(tenant_id: UUID, db: DbSession, user: CurrentUser):
    """Newest-first rehearsal records for this tenant."""
    scope = await scope_for(db, tenant_id, user)
    return {"rehearsals": await service.list_rehearsals(db, scope.tenant_id)}


@router.get("/{tenant_id}/rehearsals/{rehearsal_id}")
async def get_rehearsal_endpoint(tenant_id: UUID, rehearsal_id: UUID,
                                 db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    return await service.get_rehearsal(db, scope.tenant_id, rehearsal_id)


@router.post("/{tenant_id}/rehearsals/{rehearsal_id}/check-off")
async def check_off_endpoint(tenant_id: UUID, rehearsal_id: UUID, payload: CheckOffRequest,
                             request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    record = await service.get_rehearsal(db, scope.tenant_id, rehearsal_id)
    try:
        updated = rehearsal.check_off_step(
            record, payload.step_number, by=str(user.id), by_name=payload.by_name,
            finding=payload.finding)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    saved = await service.update_rehearsal(db, scope.tenant_id, rehearsal_id, updated)
    audit.record(db, audit_context(request, user), "whatif.rehearsal_step_checked_off",
                 target=str(tenant_id),
                 details={"rehearsal_id": str(rehearsal_id), "step": payload.step_number})
    await db.commit()
    return saved


@router.post("/{tenant_id}/rehearsals/{rehearsal_id}/skip")
async def skip_endpoint(tenant_id: UUID, rehearsal_id: UUID, payload: SkipRequest,
                        request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    record = await service.get_rehearsal(db, scope.tenant_id, rehearsal_id)
    try:
        updated = rehearsal.skip_step(
            record, payload.step_number, by=str(user.id), by_name=payload.by_name,
            reason=payload.reason)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    saved = await service.update_rehearsal(db, scope.tenant_id, rehearsal_id, updated)
    audit.record(db, audit_context(request, user), "whatif.rehearsal_step_skipped",
                 target=str(tenant_id),
                 details={"rehearsal_id": str(rehearsal_id), "step": payload.step_number})
    await db.commit()
    return saved


@router.post("/{tenant_id}/rehearsals/{rehearsal_id}/complete")
async def complete_endpoint(tenant_id: UUID, rehearsal_id: UUID, payload: CompleteRequest,
                            request: Request, db: DbSession, user: CurrentUser):
    scope = await scope_for(db, tenant_id, user)
    record = await service.get_rehearsal(db, scope.tenant_id, rehearsal_id)
    try:
        updated = rehearsal.complete_rehearsal(
            record, by=str(user.id), by_name=payload.by_name, summary=payload.summary)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    saved = await service.update_rehearsal(db, scope.tenant_id, rehearsal_id, updated)
    audit.record(db, audit_context(request, user), "whatif.rehearsal_completed",
                 target=str(tenant_id),
                 details={"rehearsal_id": str(rehearsal_id),
                          "findings": len(saved["findings"])})
    await db.commit()
    return saved


@router.post("/{tenant_id}/drills/{run_id}/compare")
async def compare_drill_endpoint(tenant_id: UUID, run_id: UUID, payload: DrillCompareRequest,
                                 request: Request, db: DbSession, user: CurrentUser):
    """Compare a Step G drill run with a fresh simulation.

    Read-only on both sides: the drill report is only read, the
    simulation writes nothing, and the comparison writes nothing
    except one audit row.
    """
    scope = await scope_for(db, tenant_id, user)
    scenario = _build_scenario(payload)
    comparison = await service.compare_drill_run(
        db, scope, user, continuity_tenant_id=payload.continuity_tenant_id,
        run_id=run_id, scenario=scenario, depth=payload.depth)
    audit.record(db, audit_context(request, user), "whatif.drill_compared",
                 target=str(tenant_id),
                 details={"run_id": str(run_id), "scenario": scenario.get("kind"),
                          "coverage": comparison["summary"].get("coverage")})
    await db.commit()
    return comparison
