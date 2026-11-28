"""Persistence and drill-compare wiring for what-if rehearsals.

Thin async layer over :mod:`app.whatif.rehearsal` (pure) and the
``whatif_rehearsals`` table. The service validates every record
through :func:`app.whatif.rehearsal.validate_rehearsal` before it
touches the database, so a tampered record can never be saved.

Tenancy is enforced by the caller (the API routes resolve both the
company-graph scope and, for drill comparison, the continuity scope
before calling here). A rehearsal row belongs to exactly one
company-graph tenant; loading by id always filters on that tenant,
so one tenant's rehearsals never leak to another.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.company_graph import service as graph_service
from app.continuity import service as bc_service
from app.continuity.access import scope_for as bc_scope_for
from app.core.errors import NotFoundError
from app.whatif import adapter, drills, rehearsal, simulator
from app.whatif.models import WhatIfRehearsal


def _parse_ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def row_to_record(row):
    """One ``WhatIfRehearsal`` row -> the plain-dict record shape."""
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "title": row.title,
        "scenario": {
            "kind": row.scenario_kind,
            "name": simulator.template_name(row.template_id) or row.scenario_kind,
            "template_id": row.template_id,
            "seed_ids": list(row.seed_ids or []),
            "seed_names": list(row.seed_names or []),
            "horizon_days": row.horizon_days,
        },
        "status": row.status,
        "dry_run": True,
        "side_effects": [],
        "practice_only": True,
        "steps": list(row.steps or []),
        "started_by": str(row.rehearsed_by),
        "started_by_name": row.rehearsed_by_name,
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
        "completed_by": str(row.completed_by) if row.completed_by else None,
        "completed_by_name": row.completed_by_name,
        "summary": row.summary,
        "findings": list(row.findings or []),
    }


def _row_fields(record, tenant_id):
    scenario = record["scenario"]
    return {
        "id": UUID(record["id"]),
        "tenant_id": tenant_id,
        "title": record["title"],
        "scenario_kind": scenario["kind"],
        "template_id": scenario.get("template_id"),
        "seed_ids": list(scenario.get("seed_ids") or []),
        "seed_names": list(scenario.get("seed_names") or []),
        "horizon_days": scenario.get("horizon_days"),
        "status": record["status"],
        "steps": list(record["steps"]),
        "findings": list(record.get("findings") or []),
        "rehearsed_by": UUID(record["started_by"]),
        "rehearsed_by_name": record.get("started_by_name"),
        "started_at": _parse_ts(record.get("started_at")),
        "completed_at": _parse_ts(record.get("completed_at")),
        "completed_by": UUID(record["completed_by"]) if record.get("completed_by") else None,
        "completed_by_name": record.get("completed_by_name"),
        "summary": record.get("summary"),
    }


async def save_rehearsal(db, tenant_id, record):
    """Insert a new rehearsal record (validated first)."""
    rehearsal.validate_rehearsal(record)
    row = WhatIfRehearsal(**_row_fields(record, tenant_id))
    db.add(row)
    await db.flush()
    saved = dict(record)
    saved["tenant_id"] = str(tenant_id)
    return saved


async def get_rehearsal(db, tenant_id, rehearsal_id):
    """Load one rehearsal record for this tenant (404 otherwise)."""
    row = await db.scalar(select(WhatIfRehearsal).where(
        WhatIfRehearsal.id == rehearsal_id,
        WhatIfRehearsal.tenant_id == tenant_id))
    if row is None:
        raise NotFoundError("Rehearsal not found.")
    return row_to_record(row)


async def list_rehearsals(db, tenant_id, *, limit=50):
    """Newest-first rehearsal records for this tenant."""
    rows = await db.scalars(select(WhatIfRehearsal).where(
        WhatIfRehearsal.tenant_id == tenant_id
    ).order_by(WhatIfRehearsal.created_at.desc(), WhatIfRehearsal.id.desc()).limit(limit))
    return [row_to_record(row) for row in rows]


async def update_rehearsal(db, tenant_id, rehearsal_id, record):
    """Replace a rehearsal record's stored fields (validated first)."""
    rehearsal.validate_rehearsal(record)
    row = await db.scalar(select(WhatIfRehearsal).where(
        WhatIfRehearsal.id == rehearsal_id,
        WhatIfRehearsal.tenant_id == tenant_id))
    if row is None:
        raise NotFoundError("Rehearsal not found.")
    for key, value in _row_fields(record, tenant_id).items():
        if key in ("id", "tenant_id", "rehearsed_by", "started_at"):
            continue
        setattr(row, key, value)
    await db.flush()
    saved = dict(record)
    saved["tenant_id"] = str(tenant_id)
    return saved


async def compare_drill_run(db, graph_scope, user, *, continuity_tenant_id, run_id,
                            scenario, depth=simulator.DEFAULT_DEPTH):
    """Compare a Step G drill run with a fresh simulation.

    Builds the snapshot from the caller's authorized company-graph
    projection, runs the simulation, then loads the drill report
    through the continuity scope (404 when the caller has no
    continuity membership for that tenant). Read-only on both sides:
    no drill record is modified and the simulation writes nothing.
    Returns the comparison dict from :func:`app.whatif.drills.
    compare_drill_to_simulation`.
    """
    bc_scope = await bc_scope_for(db, continuity_tenant_id, user)
    projection = await graph_service.projection(db, graph_scope, limit=500)
    snapshot = adapter.snapshot_from_projection(projection)
    result = simulator.simulate(snapshot, scenario, depth=depth)
    report = await bc_service.report(db, bc_scope, run_id)
    return drills.compare_drill_to_simulation(result, report)
