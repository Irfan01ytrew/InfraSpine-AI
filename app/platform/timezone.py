"""Organisation business time. Storage stays UTC; business days (delegation, SLA, reminders, report periods) use
the organisation's time zone, default Asia/Karachi."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Date, cast, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError
from app.platform.models import DEFAULT_TIME_ZONE, OrgSettings


def _utcnow() -> datetime:
    """The clock used for business days; tests replace it to check edge times such as 00:30 PKT."""
    return datetime.now(UTC)


def validate_time_zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidInputError("Choose a valid IANA time zone, for example Asia/Karachi.") from None
    return name


async def org_tz(db: AsyncSession) -> ZoneInfo:
    name = await db.scalar(select(OrgSettings.time_zone).where(OrgSettings.id == 1))
    try:
        return ZoneInfo(name or DEFAULT_TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIME_ZONE)


async def org_today(db: AsyncSession, now: datetime | None = None) -> date:
    """Today's date in the organisation's time zone."""
    moment = now or _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(await org_tz(db)).date()


def org_tz_sql():
    return func.coalesce(
        select(OrgSettings.time_zone).where(OrgSettings.id == 1).scalar_subquery(), literal(DEFAULT_TIME_ZONE)
    )


def org_today_sql(at=None):
    """SQL expression for today's organisation date, for query builders that have no session (evaluated by the
    database at the statement's transaction time)."""
    return cast(func.timezone(org_tz_sql(), at if at is not None else func.now()), Date)


async def set_org_time_zone(db: AsyncSession, name: str) -> str:
    """Caller must already have authorized `org.settings.manage`."""
    validate_time_zone(name)
    await db.execute(
        insert(OrgSettings).values(id=1, time_zone=name)
        .on_conflict_do_update(index_elements=[OrgSettings.id], set_={"time_zone": name, "updated_at": func.now()})
    )
    return name
