"""Calendar-aware forecasting wired to real Step AG leave data (Step L slice 4).

Step AG's leave requests are the planned absences slice 4 forecasts
around. The mapping is thin and grounded in AG's own model
(``app/hr/models.py`` on ``agent/hr-module`` HEAD ``4d07feb``):

- table ``hr_leave_requests``; ``employee_id`` is the person (a user
  UUID), ``start_date`` / ``end_date`` are dates, ``status`` carries a
  database CHECK ``IN ('pending','approved','rejected','cancelled')``.
- Only ``approved`` counts as a planned absence: ``pending`` is
  skipped (not a planned absence yet), ``rejected`` / ``cancelled``
  are skipped too. There is NO ``applied`` status — an approved
  request that has been executed keeps status ``approved`` and is
  marked by ``applied_at`` instead; the mapper records that as
  ``applied: True`` for context but the planning value is the same.
- A half-day leave still maps to its whole day: the calendar math
  works in whole days, and treating a half-day absence as a
  full-day absence is the conservative (advisory-safe) choice.

Reading: :func:`load_ag_leave_records` reads the AG table **by
reflection** — it needs no ``app.hr`` import, so the wiring works
on this branch (where AG is not merged) and after the merge,
unchanged. It is a read-only select scoped to one department
(``group_id``) and to ``approved`` rows overlapping the caller's
window; it never writes, never grants, never dispatches. The
service-layer path is the same mapping: a caller that already holds
a principal should use ``HrService.list_team_leave(group_id)`` and
pass the returned rows to :func:`map_ag_leave_requests` — one
mapping, two entry points, no second code path.

End to end: :func:`calendar_forecasts_from_ag` loads the approved
leave, runs :func:`leave_window_forecasts` (leave length = horizon,
leave start = anchor) and :func:`backup_overlap_warnings`
(critical when every primary holder is also absent for the whole
overlap, else warning), and returns all three, advisory only.
Unknown people in the snapshot still raise ``ValueError`` (slice-1
strictness); a missing AG table raises :class:`AgLeaveUnavailable`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.ext.asyncio import AsyncSession

from app.predictive_continuity.calendar import (
    DEFAULT_WINDOW_DAYS,
    MAX_LEAVE_DAYS,
    backup_overlap_warnings,
    leave_window_forecasts,
)

__all__ = [
    "AG_LEAVE_STATUSES",
    "AG_LEAVE_TABLE",
    "AG_LOAD_LIMIT",
    "AG_PLANNED_STATUSES",
    "AgLeaveUnavailable",
    "calendar_forecasts_from_ag",
    "load_ag_leave_records",
    "map_ag_leave_request",
    "map_ag_leave_requests",
]

#: The AG table slice 4 reads. Stable across the AG branch (HEAD
#: 4d07feb) — if AG ever renames it, the loader fails loudly instead
#: of reading the wrong table.
AG_LEAVE_TABLE = "hr_leave_requests"

#: AG's status CHECK, verbatim from app/hr/models.py. Anything
#: outside this set is a schema change, not a leave state.
AG_LEAVE_STATUSES = frozenset({"pending", "approved", "rejected", "cancelled"})

#: The AG statuses that count as a planned absence.
AG_PLANNED_STATUSES = frozenset({"approved"})

#: Columns the loader reads. Pinned against the real AG model by
#: tests (test_forecast_ag_leave.py::test_loader_columns_match_ag_model).
_READ_COLUMNS = frozenset({
    "employee_id", "start_date", "end_date", "status",
    "half_day", "leave_type", "applied_at",
})

#: Same cap as AG's own list_team_leave (200 rows): a department
#: with more approved leave than this needs a bounded follow-up,
#: not a silent truncation the caller cannot see.
AG_LOAD_LIMIT = 200


class AgLeaveUnavailable(RuntimeError):
    """The AG leave table is missing or its shape changed.

    Raised when ``hr_leave_requests`` does not exist on this
    database (Step AG not merged yet) or when a column the loader
    reads is gone. A deployment/merge state, not a bad input.
    """


# -------------------------------------------------------------------------------------- mapping

def _coerce_day(value, *, field: str) -> date:
    """ISO date / datetime / date → date, or ValueError."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip()).date()
        except ValueError:
            pass
    raise ValueError(f"AG leave {field} must be an ISO date (YYYY-MM-DD), got {value!r}.")


def _field(row, name):
    """Read ``name`` from a dict row or an ORM row / attribute object."""
    if isinstance(row, dict):
        return row.get(name)
    if hasattr(row, "_mapping"):  # SQLAlchemy Row
        return row._mapping.get(name)
    return getattr(row, name, None)


def map_ag_leave_request(row) -> dict | None:
    """Map one AG leave-request row onto a calendar leave dict.

    Accepts an ORM row, a SQLAlchemy Row, or a plain dict with AG
    fields (``employee_id``, ``start_date``, ``end_date``,
    ``status``, optional ``half_day``, ``leave_type``,
    ``applied_at``). Returns ``None`` for a non-planned status
    (``pending`` / ``rejected`` / ``cancelled``) — skipped, not
    rejected. Returns ``{"person_id", "start", "end", "status",
    "leave_type", "half_day", "applied"}`` with ``start`` / ``end``
    as dates for an ``approved`` request.

    Raises ``ValueError`` for an unknown status, a missing person or
    date, an end before its start, or a window over
    :data:`MAX_LEAVE_DAYS` days.
    """
    status = _field(row, "status")
    if status is None:
        raise ValueError(f"AG leave row needs a status, got {row!r}.")
    if status not in AG_LEAVE_STATUSES:
        raise ValueError(
            f"Unknown AG leave status {status!r} — expected one of "
            f"{sorted(AG_LEAVE_STATUSES)}.")
    if status not in AG_PLANNED_STATUSES:
        return None
    employee_id = _field(row, "employee_id")
    if employee_id is None or (isinstance(employee_id, str) and not employee_id.strip()):
        raise ValueError(f"AG leave row needs an employee_id, got {row!r}.")
    start = _coerce_day(_field(row, "start_date"), field="start_date")
    end = _coerce_day(_field(row, "end_date"), field="end_date")
    if end < start:
        raise ValueError(f"AG leave for {employee_id} ends {end} before it starts {start}.")
    if (end - start).days + 1 > MAX_LEAVE_DAYS:
        raise ValueError(
            f"AG leave for {employee_id} spans {(end - start).days + 1} days, "
            f"over the {MAX_LEAVE_DAYS}-day cap.")
    return {
        "person_id": str(employee_id),
        "start": start,
        "end": end,
        "status": "approved",
        "leave_type": _field(row, "leave_type"),
        "half_day": bool(_field(row, "half_day") or False),
        "applied": _field(row, "applied_at") is not None,
    }


def map_ag_leave_requests(rows) -> list[dict]:
    """Map AG rows onto sorted calendar leave dicts (planned only).

    Non-planned rows are skipped. Raises ``ValueError`` on a
    malformed row. Returns ``[{"person_id", "start", "end",
    "status", "leave_type", "half_day", "applied"}]`` sorted by
    (start, person_id), the same order :func:`normalize_leave_records`
    produces.
    """
    if rows is None:
        raise ValueError("AG leave rows must be a list, got None.")
    out = []
    for row in rows:
        mapped = map_ag_leave_request(row)
        if mapped is not None:
            out.append(mapped)
    out.sort(key=lambda item: (item["start"], item["person_id"]))
    return out


# -------------------------------------------------------------------------------------- loading

def _coerce_group_id(group_id) -> UUID:
    if isinstance(group_id, UUID):
        return group_id
    try:
        return UUID(str(group_id))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"group_id must be a UUID, got {group_id!r}.")


def _reflect_table(sync_session) -> Table:
    """Reflect the AG leave table; fail loudly when it is absent."""
    metadata = MetaData()
    try:
        table = Table(AG_LEAVE_TABLE, metadata,
                      autoload_with=sync_session.get_bind())
    except NoSuchTableError as exc:
        raise AgLeaveUnavailable(
            f"AG leave table {AG_LEAVE_TABLE!r} is missing on this "
            f"database — Step AG is not merged here yet.") from exc
    missing = _READ_COLUMNS - set(table.columns.keys())
    if missing:
        raise AgLeaveUnavailable(
            f"AG leave table {AG_LEAVE_TABLE!r} is missing columns "
            f"{sorted(missing)} — the AG schema changed; update the loader.")
    return table


async def load_ag_leave_records(session: AsyncSession, group_id, *,
                                start: date | datetime | str | None = None,
                                end: date | datetime | str | None = None,
                                limit: int = AG_LOAD_LIMIT) -> list[dict]:
    """Load approved AG leave for one department as calendar dicts.

    Read-only: one select on ``hr_leave_requests`` (reflected, so no
    ``app.hr`` import is needed), scoped to ``group_id`` and to
    ``approved`` rows overlapping ``[start, end]`` when given.
    Returns the mapped dicts sorted by (start, person_id).

    Raises ``AgLeaveUnavailable`` when the AG table (or a column it
    reads) is missing, ``ValueError`` for a bad group id or window.
    ``limit`` caps the read at :data:`AG_LOAD_LIMIT` by default —
    the same cap AG's own list uses; pass an explicit limit to
    override it.
    """
    gid = _coerce_group_id(group_id)
    start_day = _coerce_day(start, field="start") if start is not None else None
    end_day = _coerce_day(end, field="end") if end is not None else None
    if start_day is not None and end_day is not None and end_day < start_day:
        raise ValueError(f"Load window ends {end_day} before it starts {start_day}.")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}.")
    table = await session.run_sync(_reflect_table)
    query = select(
        table.c.employee_id, table.c.start_date, table.c.end_date,
        table.c.status, table.c.half_day, table.c.leave_type,
        table.c.applied_at,
    ).where(
        table.c.group_id == gid,
        table.c.status == "approved",
    )
    if start_day is not None:
        query = query.where(table.c.end_date >= start_day)
    if end_day is not None:
        query = query.where(table.c.start_date <= end_day)
    query = query.order_by(table.c.start_date, table.c.employee_id).limit(limit)
    rows = (await session.execute(query)).mappings().all()
    return map_ag_leave_requests([dict(row) for row in rows])


# -------------------------------------------------------------------------------------- end to end

def _serialize_record(record: dict) -> dict:
    return {
        "person_id": record["person_id"],
        "start": record["start"].isoformat(),
        "end": record["end"].isoformat(),
        "status": record["status"],
        "leave_type": record.get("leave_type"),
        "half_day": record.get("half_day", False),
        "applied": record.get("applied", False),
    }


async def calendar_forecasts_from_ag(snapshot: dict, session: AsyncSession,
                                     group_id, *, now=None,
                                     window_days: int = DEFAULT_WINDOW_DAYS,
                                     start: date | datetime | str | None = None,
                                     end: date | datetime | str | None = None) -> dict:
    """Calendar-aware forecasts for one department, from real AG leave.

    Loads the department's approved leave overlapping the window
    (default ``[today, today + 365 days]`` — anything further out
    cannot produce a forecast inside the horizon cap), then runs
    :func:`leave_window_forecasts` and
    :func:`backup_overlap_warnings` on it. Advisory only: nothing
    here changes a leave, a backup, or a process.

    Returns ``{"group_id", "leave_records", "window_forecasts",
    "backup_warnings"}`` with ISO dates in the leave records.
    Raises ``ValueError`` for a malformed snapshot, leave for a
    person the snapshot does not know, or a bad window;
    ``AgLeaveUnavailable`` when the AG table is missing.
    """
    if now is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(now, datetime):
        today = now.date()
    elif isinstance(now, date):
        today = now
    else:
        raise ValueError(f"now must be a date or datetime, got {now!r}.")
    start_day = _coerce_day(start, field="start") if start is not None else today
    end_day = (_coerce_day(end, field="end") if end is not None
               else today + timedelta(days=MAX_LEAVE_DAYS))
    records = await load_ag_leave_records(
        session, group_id, start=start_day, end=end_day)
    return {
        "group_id": str(_coerce_group_id(group_id)),
        "leave_records": [_serialize_record(record) for record in records],
        "window_forecasts": leave_window_forecasts(snapshot, records, now=now),
        "backup_warnings": backup_overlap_warnings(
            snapshot, records, now=now, window_days=window_days),
    }
