"""Scheduled reports API (Step AA, slice 2): report schedules, delivery history
and per-user preferences.

Authorisation goes through ``app/platform`` on the organisation scope, never
through the legacy role: ``reports.manage`` creates/updates/deletes schedules
(administrators), ``reports.view`` reads deliveries (a user sees only their own
deliveries unless they also hold ``reports.manage``). A user who is not a
member of the organisation gets 404; a member without the permission gets 403.

Reports are read-only summaries: no endpoint here executes a payment, changes
an invoice, or approves anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, audit_context
from app.core.errors import InvalidInputError, NotFoundError
from app.db.models import User
from app.db.report_schedule_models import ReportDelivery, ReportPreference, ReportScheduleRow
from app.platform.access import authorize, ensure_org_scope, ensure_role, principal_for, require
from app.reports.persistence import apply_to_row, schedule_from_row
from app.reports.scheduled import (
    CADENCES,
    CHANNELS,
    DEFAULT_OVERDUE_DAYS,
    TEMPLATES,
    ReportSchedule,
    validate_schedule,
)
from app.services.audit import record

router = APIRouter(prefix="/api/reports", tags=["scheduled-reports"])

REPORT_ROLES = {
    "reports.admin": {"reports.manage", "reports.view"},
    "reports.viewer": {"reports.view"},
}


async def ensure_report_roles(db) -> None:
    """Create the reports roles if they are missing (grants nothing by itself).

    Called once at API startup (see the lifespan in ``app/main.py``) so the
    roles exist for administrators to grant; memberships are granted
    separately, e.g. by an administrator through the platform admin API.
    There is no role-creation endpoint, so without this call nobody could
    ever hold ``reports.manage`` — ``require`` would deny every caller,
    including legacy administrators (the permission is not bridged).
    """
    for key, permissions in REPORT_ROLES.items():
        await ensure_role(db, key, permissions)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleIn(Strict):
    template: str = Field(pattern="|".join(TEMPLATES))
    cadence: str = Field(default="daily", pattern="|".join(CADENCES))
    time_of_day: time = Field(default=time(8, 0))
    timezone: str = Field(default="Asia/Karachi", max_length=64)
    weekday: int = Field(default=0, ge=0, le=6)
    channels: list[str] = Field(default=["whatsapp"], min_length=1)
    recipients: list[UUID] = Field(min_length=1)
    quiet_start: time = Field(default=time(22, 0))
    quiet_end: time = Field(default=time(7, 0))
    overdue_days: int = Field(default=DEFAULT_OVERDUE_DAYS, ge=0)
    enabled: bool = True


class SchedulePatch(Strict):
    template: str | None = Field(default=None, pattern="|".join(TEMPLATES))
    cadence: str | None = Field(default=None, pattern="|".join(CADENCES))
    time_of_day: time | None = None
    timezone: str | None = Field(default=None, max_length=64)
    weekday: int | None = Field(default=None, ge=0, le=6)
    channels: list[str] | None = Field(default=None, min_length=1)
    recipients: list[UUID] | None = Field(default=None, min_length=1)
    quiet_start: time | None = None
    quiet_end: time | None = None
    overdue_days: int | None = Field(default=None, ge=0)
    enabled: bool | None = None


class PreferenceIn(Strict):
    template: str = Field(pattern="|".join(TEMPLATES))
    channels: list[str] = Field(default=["whatsapp"], min_length=1)
    cadence: str = Field(default="daily", pattern="|".join(CADENCES))
    enabled: bool = True
    quiet_start: time | None = None
    quiet_end: time | None = None


async def _org_and_principal(db, user):
    principal = principal_for(user)
    org = await ensure_org_scope(db)
    return principal, org


async def _require_manage(db, user):
    principal, org = await _org_and_principal(db, user)
    await require(db, principal, "reports.manage", org.id)
    return org


async def _require_view(db, user):
    principal, org = await _org_and_principal(db, user)
    await require(db, principal, "reports.view", org.id)
    return principal, org


def _schedule_out(row: ReportScheduleRow) -> dict:
    return {
        "id": str(row.id),
        "template": row.template,
        "cadence": row.cadence,
        "time_of_day": row.time_of_day.isoformat(),
        "timezone": row.timezone,
        "weekday": row.weekday,
        "channels": list(row.channels or []),
        "recipients": [str(r) for r in (row.recipients or [])],
        "quiet_start": row.quiet_start.isoformat(),
        "quiet_end": row.quiet_end.isoformat(),
        "overdue_days": row.overdue_days,
        "enabled": row.enabled,
        "last_sent_at": row.last_sent_at.isoformat() if row.last_sent_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _delivery_out(row: ReportDelivery) -> dict:
    return {
        "id": str(row.id),
        "schedule_id": str(row.schedule_id),
        "period_start": row.period_start.isoformat(),
        "channel": row.channel,
        "recipient_user_id": str(row.recipient_user_id),
        "status": row.status,
        "attempts": row.attempts,
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "last_error_code": row.last_error_code,
        "provider_ref": row.provider_ref,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _preference_out(row: ReportPreference) -> dict:
    return {
        "id": str(row.id),
        "user_id": str(row.user_id),
        "template": row.template,
        "channels": list(row.channels or []),
        "cadence": row.cadence,
        "enabled": row.enabled,
        "quiet_start": row.quiet_start.isoformat() if row.quiet_start else None,
        "quiet_end": row.quiet_end.isoformat() if row.quiet_end else None,
    }


def _to_schedule(body: ScheduleIn | SchedulePatch, base: ReportSchedule | None = None) -> ReportSchedule:
    """Merge a request body onto an existing schedule (or defaults) and validate it."""
    current = base or ReportSchedule(
        template="daily_cash_summary", cadence="daily", time_of_day=time(8, 0),
        timezone="Asia/Karachi", channels=("whatsapp",), recipients=(),
    )
    data = body.model_dump(exclude_unset=True)
    if "channels" in data and data["channels"] is not None:
        data["channels"] = tuple(data["channels"])
    if "recipients" in data and data["recipients"] is not None:
        data["recipients"] = tuple(str(r) for r in data["recipients"])
    merged = ReportSchedule(
        template=data.get("template", current.template),
        cadence=data.get("cadence", current.cadence),
        time_of_day=data.get("time_of_day", current.time_of_day),
        timezone=data.get("timezone", current.timezone),
        weekday=data.get("weekday", current.weekday),
        channels=data.get("channels", current.channels),
        recipients=data.get("recipients", current.recipients),
        quiet_start=data.get("quiet_start", current.quiet_start),
        quiet_end=data.get("quiet_end", current.quiet_end),
        overdue_days=data.get("overdue_days", current.overdue_days),
        enabled=data.get("enabled", current.enabled),
        last_sent_at=current.last_sent_at,
    )
    try:
        validate_schedule(merged)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    return merged


async def _check_recipients(db, recipients: tuple[str, ...]) -> None:
    """Every recipient must be an existing, active user."""
    if not recipients:
        raise InvalidInputError("at least one recipient is required")
    rows = (await db.execute(select(User.id, User.is_active).where(
        User.id.in_([UUID(r) for r in recipients])))).all()
    found = {str(row[0]): row[1] for row in rows}
    for recipient in recipients:
        if recipient not in found:
            raise InvalidInputError(f"unknown recipient: {recipient}")
        if not found[recipient]:
            raise InvalidInputError(f"recipient is not active: {recipient}")


@router.post("/schedules", status_code=201)
async def create_schedule(request: Request, db: DbSession, user: CurrentUser, body: ScheduleIn):
    await _require_manage(db, user)
    schedule = _to_schedule(body)
    await _check_recipients(db, schedule.recipients)
    row = ReportScheduleRow(created_by=user.id)
    apply_to_row(row, schedule)
    row.last_sent_at = None
    db.add(row)
    record(db, audit_context(request, user), "reports.schedule_created", target=str(row.id),
           details={"template": row.template, "cadence": row.cadence})
    await db.commit()
    return _schedule_out(row)


@router.get("/schedules")
async def list_schedules(db: DbSession, user: CurrentUser):
    await _require_manage(db, user)
    rows = await db.scalars(select(ReportScheduleRow).order_by(ReportScheduleRow.created_at))
    return {"schedules": [_schedule_out(row) for row in rows]}


@router.get("/schedules/{schedule_id}")
async def get_schedule(db: DbSession, user: CurrentUser, schedule_id: UUID):
    await _require_manage(db, user)
    row = await db.get(ReportScheduleRow, schedule_id)
    if row is None:
        raise NotFoundError("Report schedule not found.")
    return _schedule_out(row)


@router.patch("/schedules/{schedule_id}")
async def update_schedule(request: Request, db: DbSession, user: CurrentUser,
                          schedule_id: UUID, body: SchedulePatch):
    await _require_manage(db, user)
    row = await db.get(ReportScheduleRow, schedule_id)
    if row is None:
        raise NotFoundError("Report schedule not found.")
    schedule = _to_schedule(body, base=schedule_from_row(row))
    await _check_recipients(db, schedule.recipients)
    apply_to_row(row, schedule)
    row.updated_at = datetime.now(UTC)
    record(db, audit_context(request, user), "reports.schedule_updated", target=str(row.id),
           details={"template": row.template, "enabled": row.enabled})
    await db.commit()
    # The UPDATE expires server-defaulted columns (created_at); re-fetch
    # before serialising — touching them expired raises MissingGreenlet.
    await db.refresh(row)
    return _schedule_out(row)


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(request: Request, db: DbSession, user: CurrentUser, schedule_id: UUID):
    await _require_manage(db, user)
    row = await db.get(ReportScheduleRow, schedule_id)
    if row is None:
        raise NotFoundError("Report schedule not found.")
    await db.delete(row)
    record(db, audit_context(request, user), "reports.schedule_deleted", target=str(row.id),
           details={"template": row.template})
    await db.commit()
    return {"deleted": True, "id": str(schedule_id)}


@router.get("/deliveries")
async def list_deliveries(db: DbSession, user: CurrentUser,
                          schedule_id: UUID | None = None,
                          status: str | None = None,
                          limit: int = Query(default=50, ge=1, le=200)):
    principal, org = await _require_view(db, user)
    # A manager sees every delivery; a viewer sees only their own.
    manages = (await authorize(db, principal, "reports.manage", org.id)).allowed
    query = select(ReportDelivery).order_by(ReportDelivery.created_at.desc())
    if not manages:
        query = query.where(ReportDelivery.recipient_user_id == user.id)
    if schedule_id is not None:
        query = query.where(ReportDelivery.schedule_id == schedule_id)
    if status is not None:
        if status not in ("pending", "sending", "sent", "failed", "retry", "cancelled"):
            raise InvalidInputError(f"unknown delivery status: {status}")
        query = query.where(ReportDelivery.status == status)
    rows = await db.scalars(query.limit(limit))
    return {"deliveries": [_delivery_out(row) for row in rows]}


@router.get("/preferences")
async def list_preferences(db: DbSession, user: CurrentUser):
    await _require_view(db, user)
    rows = await db.scalars(select(ReportPreference).where(
        ReportPreference.user_id == user.id).order_by(ReportPreference.template))
    return {"preferences": [_preference_out(row) for row in rows]}


@router.put("/preferences/{template}")
async def upsert_preference(request: Request, db: DbSession, user: CurrentUser,
                            template: str, body: PreferenceIn):
    await _require_view(db, user)
    if template != body.template:
        raise InvalidInputError("template in the path must match the body")
    if any(channel not in CHANNELS for channel in body.channels):
        raise InvalidInputError(f"unknown channel: {body.channels}")
    row = await db.scalar(select(ReportPreference).where(
        ReportPreference.user_id == user.id, ReportPreference.template == template))
    if row is None:
        row = ReportPreference(user_id=user.id, template=template)
        db.add(row)
    row.channels = list(body.channels)
    row.cadence = body.cadence
    row.enabled = body.enabled
    row.quiet_start = body.quiet_start
    row.quiet_end = body.quiet_end
    row.updated_at = datetime.now(UTC)
    record(db, audit_context(request, user), "reports.preference_updated", target=str(user.id),
           details={"template": template, "channels": row.channels})
    await db.commit()
    # Same as update_schedule: the UPDATE expires server-defaulted columns.
    await db.refresh(row)
    return _preference_out(row)
