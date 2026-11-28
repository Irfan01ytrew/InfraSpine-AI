"""Twin service (Step S slices 5–6): read-only twin views over live data.

Thin async adapters over the pure modules: build the authorized twin
snapshot (slice 2, read-only) and run horizon predictions (slice 4) or
what-if simulations (slice 5) against it. These functions only ever
read — they create no tables, write no rows, approve nothing, grant
nothing, and pay nothing. The zero-side-effect guarantee is proven by
``tests/test_digital_twin_service.py``, which counts production rows
before and after a service call.
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from app.company_graph.access import Scope
from app.digital_twin.horizon import predict_absence
from app.digital_twin.impact import money_at_risk
from app.digital_twin.snapshot import build_twin_snapshot
from app.digital_twin.whatif import run_whatif


async def twin_overview(db, graph_scope: Scope, *, continuity_tenant_id=None,
                        group_id=None, limit=500):
    """Read-only summary of the live twin for one graph tenant.

    Returns entity/relationship counts by kind, money at risk, source
    freshness, the snapshot fingerprint, and whether the snapshot was
    truncated. Restricted callers only ever see their authorized
    projection — the snapshot builder enforces that upstream.
    """
    snapshot = await build_twin_snapshot(
        db, graph_scope, continuity_tenant_id=continuity_tenant_id,
        group_id=group_id, limit=limit)
    return {
        "tenant_id": str(graph_scope.tenant_id),
        "fingerprint": snapshot["fingerprint"],
        "as_of": snapshot["as_of"],
        "entity_count": len(snapshot["entities"]),
        "relationship_count": len(snapshot["relationships"]),
        "entities_by_kind": dict(Counter(e["kind"] for e in snapshot["entities"])),
        "money_at_risk": money_at_risk(snapshot["entities"]),
        "sources": snapshot["sources"],
        "stale_sources": snapshot["stale_sources"],
        "truncated": snapshot["truncated"],
    }


async def run_whatif_for_scope(db, graph_scope: Scope, scenario, *,
                               continuity_tenant_id=None, group_id=None,
                               depth=6, rehearse=False, limit=500):
    """Run a what-if scenario against the live twin (read-only).

    Builds the authorized snapshot, runs the pure
    :func:`app.digital_twin.whatif.run_whatif` against it, and returns
    the simulation with the snapshot fingerprint it was computed from.
    ``scenario`` is a dict from :func:`app.digital_twin.whatif.build_scenario`
    (or any dict with ``kind`` / ``seed_ids`` / ``horizon_days``).
    """
    snapshot = await build_twin_snapshot(
        db, graph_scope, continuity_tenant_id=continuity_tenant_id,
        group_id=group_id, limit=limit)
    simulation = run_whatif(snapshot, scenario, depth=depth, rehearse=rehearse)
    return {
        "tenant_id": str(graph_scope.tenant_id),
        "snapshot_fingerprint": snapshot["fingerprint"],
        "snapshot_as_of": snapshot["as_of"],
        "sources": snapshot["sources"],
        "stale_sources": snapshot["stale_sources"],
        "simulation": simulation,
    }


async def predict_for_scope(db, graph_scope: Scope, absent_ids, *,
                            continuity_tenant_id=None, group_id=None,
                            horizon_days=7, depth=6, limit=500):
    """Run a horizon absence prediction against the live twin (read-only)."""
    snapshot = await build_twin_snapshot(
        db, graph_scope, continuity_tenant_id=continuity_tenant_id,
        group_id=group_id, limit=limit)
    prediction = predict_absence(snapshot, absent_ids, horizon_days=horizon_days, depth=depth)
    return {
        "tenant_id": str(graph_scope.tenant_id),
        "snapshot_fingerprint": snapshot["fingerprint"],
        "snapshot_as_of": snapshot["as_of"],
        "sources": snapshot["sources"],
        "stale_sources": snapshot["stale_sources"],
        "prediction": prediction,
    }


__all__ = ["predict_for_scope", "run_whatif_for_scope", "twin_overview"]
