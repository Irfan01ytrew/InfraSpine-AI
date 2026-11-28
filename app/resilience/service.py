"""Slice 7: read-only resilience overview service (DB-backed).

Builds the caller's authorized projection of the dependency map for one
department scope:

* continuity nodes/links for the scope: the department-kind node
  carrying the scope's ``access_group_id``, plus the resources in the
  same tenant whose ``department_id`` is that department node,
* company-graph records the caller may read, under the graph's own
  visibility rules (tenant / department / restricted + readers).

Finance rows are NOT scope-linked in this build, so they are omitted and
the omission is reported in ``notes`` — never silently. Counts in the
response cover visible records only: hidden records are never counted,
named, or leaked through totals. The snapshot is advisory only; nothing
here writes, approves, or executes anything.
"""

from datetime import UTC, datetime

from sqlalchemy import select

from app.company_graph.access import scope_for, visible
from app.company_graph.models import GraphMember, GraphRecord
from app.continuity.models import Link as BcLink
from app.continuity.models import Node as BcNode
from app.core.errors import NotFoundError
from app.db.workflow_engine_models import WorkflowRun
from app.resilience.audit import resilience_audit_trail
from app.resilience.impact import rank_risks, score_disruption
from app.resilience.runtime import plan_progress
from app.resilience.snapshot import build_snapshot
from app.workflows.security import require_scope

TOP_RANKED = 25


def _risk_level(score):
    if score >= 80:
        return "critical"
    if score >= 60:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _graph_record_dict(row):
    return {
        "id": str(row.id),
        "record_type": row.record_type,
        "external_key": row.external_key,
        "kind": row.kind,
        "name": row.name,
        "source_id": str(row.source_id) if row.source_id else None,
        "target_id": str(row.target_id) if row.target_id else None,
        "visibility": row.visibility,
        "attributes": dict(row.attributes or {}),
        "confidence": row.confidence,
        "retired": row.retired,
        "valid_from": row.valid_from,
        "valid_to": row.valid_to,
    }


def _continuity_node_dict(row):
    return {
        "id": str(row.id),
        "external_key": row.external_key,
        "kind": row.kind,
        "name": row.name,
        "data": dict(row.data or {}),
        "user_id": str(row.user_id) if row.user_id else None,
        "department_id": str(row.department_id) if row.department_id else None,
    }


def _continuity_link_dict(row):
    return {
        "id": str(row.id),
        "source_id": str(row.source_id),
        "target_id": str(row.target_id),
        "kind": row.kind,
        "data": dict(row.data or {}),
    }


async def load_snapshot_for_scope(db, user, scope_id):
    """(snapshot, meta) for a department scope, caller's projection only.

    ``require_scope`` raises 404 for non-members, so a restricted user
    cannot even learn whether a scope exists.
    """
    group = await require_scope(db, user, scope_id)
    as_of = datetime.now(UTC)
    notes = []
    freshness = None

    # Continuity is tenant-scoped and department-assigned: exactly one
    # department-kind node carries an access_group_id (unique per node),
    # and the department's resources are the nodes in the same tenant
    # whose department_id is that department node. Loading by
    # access_group_id alone would return only the department node.
    dept_nodes = (
        await db.scalars(
            select(BcNode).where(
                BcNode.access_group_id == group.id, BcNode.kind == "department"
            )
        )
    ).all()
    tenant_ids = {row.tenant_id for row in dept_nodes}
    dept_ids = {row.id for row in dept_nodes}
    nodes = list(dept_nodes)
    if dept_ids:
        nodes.extend(
            (
                await db.scalars(
                    select(BcNode).where(
                        BcNode.tenant_id.in_(tenant_ids),
                        BcNode.department_id.in_(dept_ids),
                    )
                )
            ).all()
        )
    node_ids = {row.id for row in nodes}
    links = []
    if node_ids:
        links = (
            await db.scalars(
                select(BcLink).where(
                    BcLink.tenant_id.in_(tenant_ids),
                    BcLink.source_id.in_(node_ids),
                    BcLink.target_id.in_(node_ids),
                )
            )
        ).all()
    continuity = (
        [_continuity_node_dict(row) for row in nodes],
        [_continuity_link_dict(row) for row in links],
    )
    for row in nodes:
        if freshness is None or row.created_at > freshness:
            freshness = row.created_at

    records = []
    skipped_tenants = 0
    member_tenant_ids = (
        await db.scalars(
            select(GraphMember.tenant_id).where(
                GraphMember.user_id == user.id, GraphMember.active.is_(True)
            )
        )
    ).all()
    for tenant_id in member_tenant_ids:
        try:
            graph_scope = await scope_for(db, tenant_id, user)
        except NotFoundError:
            # The graph module gate is closed for this tenant: the caller
            # learns nothing about it — not even that it exists.
            skipped_tenants += 1
            continue
        rows = (
            await db.scalars(
                select(GraphRecord).where(visible(graph_scope))
            )
        ).all()
        records.extend(_graph_record_dict(row) for row in rows)
        for row in rows:
            if freshness is None or row.updated_at > freshness:
                freshness = row.updated_at
    if skipped_tenants:
        notes.append(
            f"{skipped_tenants} company-graph tenant(s) are not accessible "
            "to you and contribute nothing to this map."
        )
    notes.append(
        "Finance rows are not linked to department scopes yet, so invoice "
        "batches and payments are not in this map (owner decision)."
    )
    snapshot = build_snapshot(
        company_graph=records or None,
        continuity=continuity,
        as_of=as_of,
    )
    meta = {
        "scope_id": str(group.id),
        "scope_name": group.name,
        "as_of": as_of.isoformat(),
        "freshness": freshness.isoformat() if freshness else None,
        "sources": snapshot["sources"],
        "skipped": snapshot["skipped"],
        "notes": snapshot["notes"] + notes,
        "conflicts": snapshot["conflicts"],
    }
    return snapshot, meta


async def overview_for_scope(db, user, scope_id):
    """Ranked risks, recovery plans and recent audit events for a scope."""
    snapshot, meta = await load_snapshot_for_scope(db, user, scope_id)
    ranked = rank_risks(snapshot)[:TOP_RANKED]
    runs = (
        await db.scalars(
            select(WorkflowRun)
            .where(
                WorkflowRun.scope_id == scope_id,
                WorkflowRun.origin == "resilience",
            )
            .order_by(WorkflowRun.created_at.desc())
            .limit(10)
        )
    ).all()
    plans = []
    for run in runs:
        progress = await plan_progress(db, scope_id, run.origin_ref)
        plans.append(
            {
                "plan_id": run.origin_ref,
                "run_id": str(run.id),
                "status": run.status,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "progress": progress,
            }
        )
    trail = await resilience_audit_trail(db, scope_id=str(scope_id), limit=10)
    return {
        **meta,
        "entities": len(snapshot["entities"]),
        "relationships": len(snapshot["relationships"]),
        "ranked": [
            {
                "node_id": item["node_id"],
                "name": item["name"],
                "kind": item["kind"],
                "risk_score": item["risk_score"],
                "risk_level": _risk_level(item["risk_score"]),
                "factors": item["factors"],
            }
            for item in ranked
        ],
        "plans": plans,
        "recent_audit": trail,
    }


async def simulate_for_scope(db, user, scope_id, event):
    """Score one disruption event over the scope's snapshot (advisory)."""
    snapshot, meta = await load_snapshot_for_scope(db, user, scope_id)
    analysis = score_disruption(snapshot, event)
    return {**meta, "analysis": analysis}


async def plan_progress_for_scope(db, user, scope_id, plan_id):
    """Recovery progress for one plan in a scope."""
    await require_scope(db, user, scope_id)
    return await plan_progress(db, scope_id, plan_id)
