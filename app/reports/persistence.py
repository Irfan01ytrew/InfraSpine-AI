"""Scheduled reports (Step AA, slice 2): conversion between the persisted
``report_schedules`` rows and the pure :class:`ReportSchedule` dataclass in
``app.reports.scheduled``. All scheduling math stays in the pure module; this
module only translates storage types (JSONB lists, naive/aware datetimes).
"""

from __future__ import annotations

from datetime import UTC

from app.db.report_schedule_models import ReportScheduleRow
from app.reports.scheduled import ReportSchedule, validate_schedule


def schedule_from_row(row: ReportScheduleRow) -> ReportSchedule:
    """Build the pure schedule model from a database row."""
    last_sent = row.last_sent_at
    if last_sent is not None and last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=UTC)
    return ReportSchedule(
        template=row.template,
        cadence=row.cadence,
        time_of_day=row.time_of_day,
        timezone=row.timezone,
        weekday=row.weekday,
        channels=tuple(row.channels or []),
        recipients=tuple(str(r) for r in (row.recipients or [])),
        quiet_start=row.quiet_start,
        quiet_end=row.quiet_end,
        overdue_days=row.overdue_days,
        enabled=row.enabled,
        last_sent_at=last_sent,
    )


def apply_to_row(row: ReportScheduleRow, schedule: ReportSchedule) -> None:
    """Copy a validated pure schedule onto a row (no commit)."""
    validate_schedule(schedule)
    row.template = schedule.template
    row.cadence = schedule.cadence
    row.time_of_day = schedule.time_of_day
    row.timezone = schedule.timezone
    row.weekday = schedule.weekday
    row.channels = list(schedule.channels)
    row.recipients = list(schedule.recipients)
    row.quiet_start = schedule.quiet_start
    row.quiet_end = schedule.quiet_end
    row.overdue_days = schedule.overdue_days
    row.enabled = schedule.enabled
    row.last_sent_at = schedule.last_sent_at
