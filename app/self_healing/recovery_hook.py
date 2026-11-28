"""Step W slice 7 — recovery/continuity hookup.

A self-healing incident can be filed as a recovery event, and the caller's
continuity workspaces can be listed as human-activation candidates. Both
are explicit human acts through the admin API; nothing here runs by itself.

What is wired (it exists on this branch):

- ``file_recovery_event`` resolves the recovery workspace (404 if the
  caller is not a member), takes the first enabled target in the tenant,
  and files the incident through the core recovery service
  (``app.recovery.service.ingest``) as an ``EventInput``: event key
  ``self-healing:{incident.key}``, source mapped from the component,
  category mapped from the kind, condition ``failure``, evidence scrubbed.
  The recovery service's own rules apply unchanged — threshold counting,
  duplicate detection, incident creation — and its ``require_write`` rule
  means only a recovery owner/responder may file (observers get 403).
  No target -> outcome ``no_target`` and no writes at all.
- ``continuity_options`` lists the caller's continuity workspaces with
  their approved and in-review (pending) plans, as candidates a human may
  activate. It never calls ``activate`` or ``decide_activation``:
  activating a plan needs a human to pick the disrupted graph nodes and
  the plan; the hook only surfaces the choice.

What is NOT wired (honest boundaries — these live on other branches):

- Step G drills: no drill comparison import here; the hook files generic
  recovery events, never a G drill run. (Step G: agent/stepg-continuity.)
- Step AH resilience: ``app/resilience`` does not exist on this branch,
  so no AH prediction input reaches the hook. (Step AH:
  agent/resilience-ai.)
- Playbook execution stays approval-gated in the recovery UI: the hook
  never calls ``propose`` or ``decide``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.continuity.models import Member as ContinuityMember
from app.continuity.models import Plan as ContinuityPlan
from app.continuity.models import Tenant as ContinuityTenant
from app.db.models import User
from app.platform import Principal
from app.recovery import service as recovery_service
from app.recovery.access import scope_for as recovery_scope_for
from app.recovery.models import Target as RecoveryTarget
from app.recovery.schemas import EventInput
from app.self_healing.detector import Incident, scrub
from app.services.audit import AuditContext

#: Component -> recovery event source.
_SOURCE_BY_COMPONENT = {
    "api": "api",
    "worker": "worker",
    "database": "database",
    "ingestion": "workflow",
    "self_test": "worker",
}

#: Incident kind -> recovery event category.
_CATEGORY_BY_KIND = {
    "api_down": "unavailable",
    "database_unreachable": "unavailable",
    "worker_stalled": "stalled",
    "error_rate_spike": "anomaly",
    "stale_lock": "stalled",
    "ingestion_backlog": "sla",
    "self_test_failed": "anomaly",
    "unknown": "anomaly",
}


def recovery_event_for(
    incident: Incident, target_id: UUID, *, now: datetime | None = None
) -> EventInput:
    """Build the recovery event for an incident (pure; no I/O)."""
    moment = now or datetime.now(UTC)
    return EventInput(
        target_id=target_id,
        event_key=f"self-healing:{incident.key}",
        source=_SOURCE_BY_COMPONENT.get(incident.component, "infrastructure"),
        category=_CATEGORY_BY_KIND.get(incident.kind, "anomaly"),
        error_code=f"self_healing.{incident.kind}",
        title=f"Self-healing: {incident.summary}"[:200],
        severity=incident.severity,
        condition="failure",
        occurred_at=moment,
        evidence={
            "self_healing_incident": incident.key,
            "kind": incident.kind,
            "component": incident.component,
            "evidence": scrub(incident.evidence)[:1000],
        },
    )


async def file_recovery_event(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: UUID,
    incident: Incident,
    cipher,
    now: datetime | None = None,
) -> dict:
    """File a self-healing incident as a recovery event.

    Returns ``{"outcome", "event_id", "incident_id", "duplicate"}`` where
    outcome is ``incident`` (a recovery incident was created or correlated),
    ``pending_threshold`` (recorded, below the target's threshold),
    ``duplicate`` (this event key was already filed), or ``no_target``
    (the tenant has no enabled target; nothing was written).
    Raises ``NotFoundError`` (404) when the caller is not a member of the
    recovery workspace, and ``PermissionDeniedError`` (403) when they are
    only an observer — the recovery service's own gates.
    """
    moment = now or datetime.now(UTC)
    scope = await recovery_scope_for(db, tenant_id, principal.user_id)
    target = await db.scalar(
        select(RecoveryTarget)
        .where(RecoveryTarget.tenant_id == tenant_id, RecoveryTarget.enabled.is_(True))
        .order_by(RecoveryTarget.name, RecoveryTarget.id)
        .limit(1)
    )
    if target is None:
        return {"outcome": "no_target", "event_id": None, "incident_id": None, "duplicate": False}
    event = recovery_event_for(incident, target.id, now=moment)
    ctx = AuditContext(principal.user_id, principal.email, None, str(principal.trace_id))
    result = await recovery_service.ingest(db, scope, event, cipher, ctx)
    await db.commit()
    outcome = "duplicate" if result.get("duplicate") else result.get("disposition")
    return {
        "outcome": outcome,
        "event_id": result.get("event_id"),
        "incident_id": result.get("incident_id"),
        "duplicate": bool(result.get("duplicate")),
    }


async def continuity_options(db: AsyncSession, *, principal: Principal) -> list[dict]:
    """The caller's continuity workspaces that have an activatable plan.

    Only workspaces with at least one approved or in-review (pending) plan
    are returned — a workspace with no plan is not an activation candidate.
    Read-only; nothing is activated, proposed, or decided here.
    """
    user = await db.get(User, principal.user_id)
    if user is None:
        return []
    rows = (
        await db.execute(
            select(ContinuityTenant, ContinuityMember)
            .join(ContinuityMember)
            .where(
                ContinuityMember.user_id == user.id,
                ContinuityMember.active.is_(True),
            )
            .order_by(ContinuityTenant.name)
        )
    ).all()
    options: list[dict] = []
    for tenant, member in rows:
        plans = list(
            await db.scalars(
                select(ContinuityPlan)
                .where(
                    ContinuityPlan.tenant_id == tenant.id,
                    ContinuityPlan.status.in_(["approved", "pending"]),
                )
                .order_by(ContinuityPlan.title, ContinuityPlan.id)
            )
        )
        if not plans:
            continue
        options.append(
            {
                "tenant_id": str(tenant.id),
                "tenant_name": tenant.name,
                "role": member.role,
                "plans": [
                    {
                        "id": str(plan.id),
                        "title": plan.title,
                        "status": plan.status,
                        "version": plan.version,
                    }
                    for plan in plans
                ],
            }
        )
    return options
