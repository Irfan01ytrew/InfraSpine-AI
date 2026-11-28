"""Platform administration: organisation settings (time zone), access revocation, channel identities."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import available_timezones

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import DbSession, PlatformAdminUser, audit_context
from app.platform import access as platform_access
from app.platform.models import DEFAULT_TIME_ZONE, OrgSettings
from app.platform.outbox import outbox_health, requeue_dead
from app.platform.timezone import _utcnow, org_today, set_org_time_zone, validate_time_zone
from app.services import audit

router = APIRouter(prefix="/api/admin/platform", tags=["platform"])


class OrgSettingsOut(BaseModel):
    time_zone: str
    currency: str
    today: str
    local_time: str
    default_time_zone: str = DEFAULT_TIME_ZONE


class OrgSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time_zone: str = Field(min_length=1, max_length=64)


async def _out(db) -> OrgSettingsOut:
    row = await db.get(OrgSettings, 1, populate_existing=True)
    zone = (row.time_zone if row else None) or DEFAULT_TIME_ZONE
    from zoneinfo import ZoneInfo
    now = _utcnow().astimezone(ZoneInfo(zone))
    return OrgSettingsOut(time_zone=zone, currency=(row.currency if row else "PKR"),
                          today=(await org_today(db)).isoformat(), local_time=now.strftime("%H:%M"))


@router.get("/org-settings", response_model=OrgSettingsOut)
async def get_org_settings(db: DbSession, admin: PlatformAdminUser):
    return await _out(db)


@router.get("/time-zones")
async def time_zones(admin: PlatformAdminUser) -> dict:
    zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))
    return {"time_zones": ["UTC", *zones]}


@router.put("/org-settings", response_model=OrgSettingsOut)
async def put_org_settings(body: OrgSettingsIn, request: Request, db: DbSession, admin: PlatformAdminUser):
    p = platform_access.principal_for(admin)
    org = await platform_access.ensure_org_scope(db)
    await platform_access.require(db, p, "org.settings.manage", org.id)
    zone = validate_time_zone(body.time_zone.strip())
    previous = await db.scalar(select(OrgSettings.time_zone).where(OrgSettings.id == 1)) or DEFAULT_TIME_ZONE
    await set_org_time_zone(db, zone)
    audit.record(db, audit_context(request, admin), "platform.org_time_zone_changed", target="org_settings",
                 details={"from": previous, "to": zone, "at": datetime.now().isoformat(timespec="seconds")})
    await db.commit()
    return await _out(db)



class RequeueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_ids: list[int] | None = None


@router.get("/outbox")
async def get_outbox(db: DbSession, admin: PlatformAdminUser):
    p = platform_access.principal_for(admin)
    org = await platform_access.ensure_org_scope(db)
    await platform_access.require(db, p, "platform.manage", org.id)
    return await outbox_health(db)


@router.post("/outbox/requeue")
async def post_outbox_requeue(body: RequeueIn, request: Request, db: DbSession, admin: PlatformAdminUser):
    p = platform_access.principal_for(admin)
    org = await platform_access.ensure_org_scope(db)
    await platform_access.require(db, p, "platform.manage", org.id)
    count = await requeue_dead(db, body.event_ids)
    audit.record(db, audit_context(request, admin), "platform.outbox_requeued", target="platform_outbox",
                 details={"count": count})
    return {"requeued": count}
