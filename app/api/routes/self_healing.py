"""Step W slices 4–5 — self-healing API: incidents, escalations, overview,
and auto-test run history.

Admin-only, gated by the explicit ``self_healing.read`` permission on the
organisation scope (``platform.require``: 404 for non-members, 403 without
the permission). Read-only, except acknowledgement, which is an explicit
human act recorded in the audit log. Nothing here changes data, permissions,
approvals, or payments.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.core.errors import InvalidInputError, NotFoundError
from app.core.permission_catalog import PERMISSION_SELF_HEALING_READ
from app.platform import access as platform_access
from app.self_healing.detector import Incident, summarize_incidents
from app.self_healing.escalation import acknowledge_escalation, list_escalations
from app.self_healing.models import (
    SelfHealingEscalation,
    SelfHealingIncident,
    SelfHealingProbe,
    SelfHealingTestRun,
)
from app.self_healing.recovery_hook import continuity_options, file_recovery_event

router = APIRouter(prefix="/api/self-healing", tags=["self-healing"])

INCIDENT_STATUSES = ("open", "remedied", "escalated")


async def _require_read(db, admin: AdminUser):
    """The ops reader: an admin holding ``self_healing.read`` on the org scope."""
    principal = platform_access.principal_for(admin)
    org = await platform_access.ensure_org_scope(db)
    await platform_access.require(db, principal, PERMISSION_SELF_HEALING_READ, org.id)
    return principal


def _incident_out(row: SelfHealingIncident) -> dict:
    return {
        "id": str(row.id),
        "key": row.key,
        "kind": row.kind,
        "severity": row.severity,
        "component": row.component,
        "summary": row.summary,
        "evidence": row.evidence,
        "status": row.status,
        "attempts": row.attempts,
        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else None,
        "first_seen_at": row.first_seen_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _escalation_out(row: SelfHealingEscalation) -> dict:
    return {
        "id": str(row.id),
        "incident_key": row.incident_key,
        "kind": row.kind,
        "severity": row.severity,
        "component": row.component,
        "summary": row.summary,
        "evidence": row.evidence,
        "attempts": row.attempts,
        "channel": row.channel,
        "delivered_at": row.delivered_at.isoformat(),
        "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "acknowledged_by": str(row.acknowledged_by) if row.acknowledged_by else None,
    }


def _check_page(limit: int, offset: int) -> None:
    if limit < 1 or limit > 200:
        raise InvalidInputError("limit must be between 1 and 200")
    if offset < 0:
        raise InvalidInputError("offset must not be negative")


@router.get("/overview")
async def overview(db: DbSession, admin: AdminUser) -> dict:
    await _require_read(db, admin)
    incidents = list(await db.scalars(select(SelfHealingIncident)))
    open_rows = [row for row in incidents if row.status == "open"]
    summary = summarize_incidents(open_rows)
    unacknowledged = int(
        await db.scalar(
            select(func.count()).where(SelfHealingEscalation.acknowledged_at.is_(None))
        )
        or 0
    )
    latest: dict[str, dict] = {}
    probe_rows = await db.scalars(
        select(SelfHealingProbe).order_by(SelfHealingProbe.recorded_at.desc()).limit(60)
    )
    for row in probe_rows:
        if row.component not in latest:
            latest[row.component] = {
                "status": row.status,
                "age_seconds": row.age_seconds,
                "recorded_at": row.recorded_at.isoformat(),
            }
    return {
        "incidents": summary,
        "open_incident_count": len(open_rows),
        "unacknowledged_escalations": unacknowledged,
        "latest_probes": latest,
    }


@router.get("/incidents")
async def list_incidents(
    db: DbSession,
    admin: AdminUser,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    await _require_read(db, admin)
    _check_page(limit, offset)
    stmt = select(SelfHealingIncident).order_by(SelfHealingIncident.updated_at.desc())
    count_stmt = select(func.count()).select_from(SelfHealingIncident)
    if status is not None:
        if status not in INCIDENT_STATUSES:
            raise InvalidInputError(f"unknown incident status: {status!r}")
        stmt = stmt.where(SelfHealingIncident.status == status)
        count_stmt = count_stmt.where(SelfHealingIncident.status == status)
    total = int(await db.scalar(count_stmt) or 0)
    rows = list(await db.scalars(stmt.limit(limit).offset(offset)))
    return {"items": [_incident_out(row) for row in rows], "total": total}


@router.get("/escalations")
async def list_escalation_rows(
    db: DbSession,
    admin: AdminUser,
    acknowledged: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    await _require_read(db, admin)
    _check_page(limit, offset)
    rows = await list_escalations(db, acknowledged=acknowledged, limit=limit, offset=offset)
    stmt = select(func.count()).select_from(SelfHealingEscalation)
    if acknowledged is True:
        stmt = stmt.where(SelfHealingEscalation.acknowledged_at.is_not(None))
    elif acknowledged is False:
        stmt = stmt.where(SelfHealingEscalation.acknowledged_at.is_(None))
    total = int(await db.scalar(stmt) or 0)
    return {"items": [_escalation_out(row) for row in rows], "total": total}


@router.post("/escalations/{incident_key}/acknowledge")
async def acknowledge(db: DbSession, admin: AdminUser, incident_key: str) -> dict:
    principal = await _require_read(db, admin)
    row = await acknowledge_escalation(db, principal, incident_key)
    return _escalation_out(row)

def _test_run_out(row: SelfHealingTestRun) -> dict:
    return {
        "id": str(row.id),
        "suite": row.suite,
        "status": row.status,
        "passed": row.passed,
        "failed": row.failed,
        "errors": row.errors,
        "skipped": row.skipped,
        "duration_seconds": row.duration_seconds,
        "failure_names": row.failure_names,
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


@router.get("/test-runs")
async def list_test_runs(
    db: DbSession,
    admin: AdminUser,
    suite: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    await _require_read(db, admin)
    _check_page(limit, offset)
    stmt = select(SelfHealingTestRun).order_by(SelfHealingTestRun.started_at.desc())
    count_stmt = select(func.count()).select_from(SelfHealingTestRun)
    if suite is not None:
        if suite not in ("focused", "full"):
            raise InvalidInputError(f"unknown suite: {suite!r}")
        stmt = stmt.where(SelfHealingTestRun.suite == suite)
        count_stmt = count_stmt.where(SelfHealingTestRun.suite == suite)
    total = int(await db.scalar(count_stmt) or 0)
    rows = list(await db.scalars(stmt.limit(limit).offset(offset)))
    return {"items": [_test_run_out(row) for row in rows], "total": total}


class _FileEventBody(BaseModel):
    tenant_id: UUID
    incident_key: str = Field(min_length=1, max_length=80)


@router.post("/recovery/file-event")
async def file_recovery_event_route(
    body: _FileEventBody, request: Request, db: DbSession, admin: AdminUser
) -> dict:
    """File a stored self-healing incident as a recovery event.

    Admin-only. The gate is the recovery workspace itself: 404 when the
    caller is not a member of that workspace, 403 when they are only an
    observer. The recovery service's threshold/duplicate rules apply; no
    playbook is proposed or decided here — that stays approval-gated in
    the recovery UI.
    """
    principal = platform_access.principal_for(admin)
    row = await db.scalar(
        select(SelfHealingIncident).where(SelfHealingIncident.key == body.incident_key)
    )
    if row is None:
        raise NotFoundError("Self-healing incident not found.")
    incident = Incident(
        key=row.key, kind=row.kind, severity=row.severity, component=row.component,
        summary=row.summary, evidence=row.evidence,
    )
    return await file_recovery_event(
        db, principal=principal, tenant_id=body.tenant_id, incident=incident,
        cipher=request.app.state.cipher,
    )


@router.get("/continuity-options")
async def list_continuity_options(db: DbSession, admin: AdminUser) -> dict:
    """The caller's continuity workspaces with an activatable plan.

    Admin-only. Returns only the caller's own workspaces; nothing is
    activated here — activation needs a human to pick the disrupted
    graph nodes and the plan.
    """
    principal = platform_access.principal_for(admin)
    return {"items": await continuity_options(db, principal=principal)}
