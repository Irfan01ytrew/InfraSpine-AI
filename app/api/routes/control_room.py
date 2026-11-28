"""Control Room live dashboard API (Step AF slices 2, 4, 5). Read-only charts,
alert configuration (admin) and delivery history.

``GET /api/control-room/scopes/{scope_id}/charts`` returns the whole
dashboard payload (spend series, invoice status counts, run counts, health
strip, live alert preview). Scope access is enforced by ``require_scope``
(404 for a non-member, 403 for a member without ``workflow.access``);
finance figures are additionally filtered by ``FinanceService`` to the
groups the caller may read with ``finance.read``.

Alert configuration (slice 5) is an admin action: ``GET/PUT
.../config`` require a platform admin (``platform.manage`` on the org
scope, audited as ``control_room.alert_config_changed``). Delivery history
(``GET .../deliveries``) is read-only for scope members via
``require_scope`` — it shows what was sent, to which channel, and what
failed; it never exposes a credential.

The Step Q overview (run counts, agent registry, blockers) lives on branch
``agent/control-room`` and is NOT re-implemented here; this endpoint covers
the chart data AF owns, and the dashboard page consumes Q's overview once
it merges.
"""

from datetime import time
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import desc, select

from app.api.deps import AdminUser, AppSettings, CurrentUser, DbSession, audit_context
from app.control_room.charts import build_charts
from app.control_room.config import (
    config_defaults,
    config_to_dict,
    effective_rules_list,
    parse_quiet,
    validate_config_payload,
)
from app.control_room.dispatch import _load_config
from app.core.errors import InvalidInputError, NotFoundError
from app.db.control_room_models import ControlRoomAlertConfig, ControlRoomAlertDelivery
from app.db.models import AccessGroup
from app.platform import access as platform_access
from app.services import audit
from app.workflows.security import require_scope

router = APIRouter(prefix="/api/control-room", tags=["control-room"])


@router.get("/scopes/{scope_id}/charts")
async def charts(
    scope_id: UUID,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    settings: AppSettings,
):
    await require_scope(db, user, scope_id)
    return await build_charts(db, user, scope_id, settings, request.app.state.cipher)


async def _require_alert_admin(db, admin):
    p = platform_access.principal_for(admin)
    org = await platform_access.ensure_org_scope(db)
    await platform_access.require(db, p, "platform.manage", org.id)


async def _require_scope_exists(db, scope_id: UUID):
    """Alert config lives on a workflow scope (an AccessGroup). An admin
    configuring a scope that does not exist gets 404, not a config row
    minted for a phantom scope. This is existence only — membership is
    not required, because platform.manage is the gate for config."""
    group = await db.get(AccessGroup, scope_id)
    if group is None:
        raise NotFoundError("Workflow scope not found.")


@router.get("/scopes/{scope_id}/config")
async def get_config(scope_id: UUID, db: DbSession, admin: AdminUser):
    """Alert configuration for a scope (admin). Missing row = defaults."""
    await _require_alert_admin(db, admin)
    await _require_scope_exists(db, scope_id)
    return _config_response(scope_id, await db.get(ControlRoomAlertConfig, scope_id))


def _config_response(scope_id: UUID, row) -> dict:
    """Stored config plus the merged rule catalogue the admin UI renders."""
    data = config_to_dict(row)
    data["scope_id"] = str(scope_id)
    data["effective_rules"] = effective_rules_list(data)
    return data


@router.put("/scopes/{scope_id}/config")
async def put_config(scope_id: UUID, body: dict, request: Request, db: DbSession, admin: AdminUser):
    """Save alert configuration for a scope (admin, audited).

    The payload is validated fail-closed: unknown rule keys, negative
    thresholds, non-positive cooldowns, unknown channels, malformed
    recipients or unreadable quiet-hour times are rejected with 422 and
    nothing is stored.
    """
    await _require_alert_admin(db, admin)
    await _require_scope_exists(db, scope_id)
    problems = validate_config_payload(body)
    if problems:
        raise InvalidInputError(f"Invalid alert configuration: {'; '.join(problems)}")
    defaults = config_defaults()
    quiet_start, _ = parse_quiet(body.get("quiet_start", defaults["quiet_start"]), "quiet_start")
    quiet_end, _ = parse_quiet(body.get("quiet_end", defaults["quiet_end"]), "quiet_end")
    row = await db.get(ControlRoomAlertConfig, scope_id)
    if row is None:
        row = ControlRoomAlertConfig(scope_id=scope_id)
        db.add(row)
    row.enabled = bool(body.get("enabled", True))
    row.quiet_start = quiet_start if isinstance(quiet_start, time) else defaults["quiet_start"]
    row.quiet_end = quiet_end if isinstance(quiet_end, time) else defaults["quiet_end"]
    row.recipients = list(body.get("recipients", []))
    row.rules = dict(body.get("rules", {}))
    audit.record(
        db,
        audit_context(request, admin),
        "control_room.alert_config_changed",
        target=f"control_room:{scope_id}",
        details={"enabled": row.enabled, "recipients": len(row.recipients), "rules": sorted(row.rules)},
    )
    await db.commit()
    return _config_response(scope_id, row)


@router.get("/scopes/{scope_id}/deliveries")
async def list_deliveries(
    scope_id: UUID,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
):
    """Delivery history for a scope (newest first). Scope members only."""
    await require_scope(db, user, scope_id)
    rows = (
        await db.execute(
            select(ControlRoomAlertDelivery)
            .where(ControlRoomAlertDelivery.scope_id == scope_id)
            .order_by(desc(ControlRoomAlertDelivery.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return {
        "scope_id": str(scope_id),
        "deliveries": [
            {
                "id": str(row.id),
                "rule_key": row.rule_key,
                "severity": row.severity,
                "title": row.title,
                "detail": row.detail,
                "channel": row.channel,
                "recipient": row.recipient,
                "status": row.status,
                "evaluated_at": row.evaluated_at.isoformat(),
                "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                "error": row.error,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
    }
