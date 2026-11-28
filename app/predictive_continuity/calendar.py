"""Calendar-aware forecasting (Step L slice 4 prep — pure functions).

Step AG's leave data is NOT merged yet, so this module builds on
nothing from AG: it takes planned-leave records as plain inputs
(dicts with ``person_id`` / ``start`` / ``end`` / optional
``status``) and produces calendar-aware forecast adjustments:

- :func:`leave_window_forecasts` — one slice-1 forecast per leave
  window ("Ayesha is on leave 5–12 Dec; these processes lose their
  approver then"). The leave length is the horizon, the leave start
  is the forecast anchor.
- :func:`backup_overlap_warnings` — lead-time warnings when a
  backup holder's leave overlaps a critical process window: the
  process loses its recorded backup for the overlapping days.
  Severity is ``critical`` when every primary holder is also absent
  for the overlap, otherwise ``warning``.

When AG merges, wiring is a thin mapping: AG leave-request rows
become these dicts (AG's status values map onto ``statuses``), and
these functions are called unchanged. This module never reads AG
tables, never grants anything, never dispatches — forecasts and
warnings are advisory only.

Validation follows slice 1's strictness: malformed records, unknown
people, end-before-start, and absurd windows raise ``ValueError``
and produce nothing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.predictive_continuity.forecast import (
    AUTHORITY_KINDS,
    forecast_unavailability,
)

__all__ = [
    "MAX_LEAVE_DAYS",
    "PLANNED_STATUSES",
    "backup_overlap_warnings",
    "leave_window_forecasts",
    "merge_leave_intervals",
    "normalize_leave_records",
]

#: A leave window longer than a year is not a planned absence; it is
#: a data error (and the forecast record horizon is capped at 365).
MAX_LEAVE_DAYS = 365

#: Leave statuses that count as a planned absence. AG's status values
#: map onto this set at wiring time; callers may pass their own set.
PLANNED_STATUSES = frozenset({"approved"})

#: Default window (days) for a process with no due date: warnings
#: cover backup leave overlapping the next 30 days.
DEFAULT_WINDOW_DAYS = 30


# -------------------------------------------------------------------------------------- parsing / validation

def _parse_day(value, *, field: str) -> date:
    """Parse an ISO date (``YYYY-MM-DD``) or datetime; return the date part."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip()).date()
        except ValueError:
            pass
    raise ValueError(f"Leave {field} must be an ISO date (YYYY-MM-DD), got {value!r}.")


def _snapshot_people(snapshot: dict) -> dict:
    """Validate the snapshot shape lightly and return {id: entity} for people."""
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a dict.")
    entities = snapshot.get("entities")
    if not isinstance(entities, list):
        raise ValueError("snapshot must carry an 'entities' list.")
    people = {}
    for entity in entities:
        if not isinstance(entity, dict):
            raise ValueError("snapshot entities must be dicts.")
        entity_id = entity.get("id")
        if not isinstance(entity_id, str) or not entity_id:
            raise ValueError("snapshot entities must have non-empty string ids.")
        if entity.get("kind") == "person":
            people[entity_id] = entity
    return people


def normalize_leave_records(records, *, statuses=None) -> list[dict]:
    """Validate + normalize leave records to sorted plain dicts.

    Each record is a dict with ``person_id`` (non-empty string),
    ``start`` / ``end`` (ISO dates; end on/after start; at most
    :data:`MAX_LEAVE_DAYS` days inclusive) and optional ``status``.
    ``statuses`` is the set of statuses that count as a planned
    absence (default :data:`PLANNED_STATUSES`): records with another
    status are skipped, not rejected — a pending request is not a
    planned absence. Pass an explicit set to override the default
    (e.g. AG's status values once they are mapped at wiring time).

    Returns ``[{"person_id", "start", "end", "status"}]`` with
    ``start``/``end`` as :class:`datetime.date`, sorted by
    (start, person_id). Raises ``ValueError`` on malformed records.
    """
    if not isinstance(records, (list, tuple)):
        raise ValueError("leave records must be a list.")
    wanted = PLANNED_STATUSES if statuses is None else set(statuses)
    out = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"Leave record must be a dict, got {record!r}.")
        person_id = record.get("person_id")
        if not isinstance(person_id, str) or not person_id:
            raise ValueError(f"Leave record needs a non-empty person_id, got {record!r}.")
        status = record.get("status")
        if status is not None and status not in wanted:
            continue
        start = _parse_day(record.get("start"), field="start")
        end = _parse_day(record.get("end"), field="end")
        if end < start:
            raise ValueError(
                f"Leave for {person_id} ends {end} before it starts {start}.")
        if (end - start).days + 1 > MAX_LEAVE_DAYS:
            raise ValueError(
                f"Leave for {person_id} spans {(end - start).days + 1} days, "
                f"over the {MAX_LEAVE_DAYS}-day cap.")
        out.append({"person_id": person_id, "start": start, "end": end,
                    "status": status})
    out.sort(key=lambda item: (item["start"], item["person_id"]))
    return out


def merge_leave_intervals(records) -> dict[str, list[tuple[date, date]]]:
    """Merge normalized records into per-person non-overlapping intervals.

    Overlapping or adjacent (end + 1 == next start) intervals merge —
    leave days are inclusive, so back-to-back leave is one absence.
    Returns ``{person_id: [(start, end), ...]}`` sorted by start.
    """
    normalized = normalize_leave_records(records)
    merged: dict[str, list[tuple[date, date]]] = {}
    for record in normalized:
        intervals = merged.setdefault(record["person_id"], [])
        start, end = record["start"], record["end"]
        if intervals and start <= intervals[-1][1] + timedelta(days=1):
            prev_start, prev_end = intervals[-1]
            intervals[-1] = (prev_start, max(prev_end, end))
        else:
            intervals.append((start, end))
    return merged


def _overlap(first: tuple[date, date], second: tuple[date, date]):
    """Inclusive overlap of two (start, end) date intervals, or None."""
    start = max(first[0], second[0])
    end = min(first[1], second[1])
    return (start, end) if start <= end else None


# -------------------------------------------------------------------------------------- window forecasts

def leave_window_forecasts(snapshot: dict, leave_records, *, now=None,
                           statuses=None, depth: int = 6) -> list[dict]:
    """One forecast per planned leave window.

    For each normalized record the person is unavailable for the
    whole window: the horizon is the window length in days
    (inclusive) and the forecast anchor is the leave start at
    midnight UTC. Returns ``[{"person_id", "start", "end", "days",
    "forecast"}]`` sorted by (start, person_id).

    Raises ``ValueError`` for malformed records or a person the
    snapshot does not know (slice-1 strictness: unknown roots
    never forecast).
    """
    normalized = normalize_leave_records(leave_records, statuses=statuses)
    people = _snapshot_people(snapshot)
    out = []
    for record in normalized:
        person_id = record["person_id"]
        if person_id not in people:
            raise ValueError(f"Leave for unknown person in snapshot: {person_id!r}.")
        days = (record["end"] - record["start"]).days + 1
        anchor = datetime.combine(record["start"], datetime.min.time(),
                                  tzinfo=timezone.utc)
        forecast = forecast_unavailability(
            snapshot, [person_id], horizon_days=days, now=anchor, depth=depth,
        )
        out.append({
            "person_id": person_id,
            "start": record["start"].isoformat(),
            "end": record["end"].isoformat(),
            "days": days,
            "forecast": forecast,
        })
    return out


# -------------------------------------------------------------------------------------- backup overlap warnings

def _covers(intervals, window) -> bool:
    """True when some interval covers the whole window (inclusive)."""
    return any(start <= window[0] and end >= window[1] for start, end in intervals)


def _process_windows(snapshot: dict, today: date, window_days: int):
    """(process entity, window, backup holders, primary holders).

    The window is [today, due_at] when the process carries a
    parseable ``due_at`` on/after today, else [today, today +
    window_days]. Processes whose due date is already past get no
    window — the deadline has passed, backup leave changes nothing.
    Backup holders are persons with a ``backup_for`` edge into the
    process; primary holders are persons with an authority edge
    (``AUTHORITY_KINDS``) into it.
    """
    entities = snapshot.get("entities") or []
    relationships = snapshot.get("relationships") or []
    by_id = {e["id"]: e for e in entities if isinstance(e, dict)}
    backups: dict[str, list[str]] = {}
    primaries: dict[str, list[str]] = {}
    for edge in relationships:
        if not isinstance(edge, dict):
            continue
        source, target, kind = edge.get("source_id"), edge.get("target_id"), edge.get("kind")
        if not source or not target:
            continue
        source_entity = by_id.get(source)
        if source_entity is None or source_entity.get("kind") != "person":
            continue
        if kind == "backup_for":
            backups.setdefault(target, []).append(source)
        elif kind in AUTHORITY_KINDS:
            primaries.setdefault(target, []).append(source)
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("kind") != "process":
            continue
        process_id = entity.get("id")
        due_raw = (entity.get("attributes") or {}).get("due_at")
        if due_raw is not None:
            due = _parse_day(due_raw, field="due_at")
            if due < today:
                continue
            window = (today, due)
        else:
            window = (today, today + timedelta(days=window_days))
        yield entity, window, backups.get(process_id, []), primaries.get(process_id, [])


def backup_overlap_warnings(snapshot: dict, leave_records, *, now=None,
                            statuses=None, window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    """Lead-time warnings for backup coverage lost to leave.

    For every process with recorded backup holders, a warning fires
    when a backup holder's leave overlaps the process window
    ([today, due_at], or the next ``window_days`` days when the
    process has no due date). Each warning names the process, the
    backup holder, the overlapping days, and the lead time in days
    from today until the overlap starts (0 when it already started).

    Severity is ``critical`` when every primary holder of the
    process is also on leave for the whole overlap (nobody is left
    to cover it); otherwise ``warning``. Warnings are advisory
    only — they never change a leave, a backup, or a process.

    Sorted by (lead days, process id, backup person id), so the
    most urgent warning comes first. Raises ``ValueError`` for
    malformed records or leave for a person the snapshot does not
    know.
    """
    if now is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(now, datetime):
        today = now.date()
    elif isinstance(now, date):
        today = now
    else:
        raise ValueError(f"now must be a date or datetime, got {now!r}.")
    if not isinstance(window_days, int) or isinstance(window_days, bool) or window_days < 1:
        raise ValueError("window_days must be a positive integer.")
    normalized = normalize_leave_records(leave_records, statuses=statuses)
    people = _snapshot_people(snapshot)
    for record in normalized:
        if record["person_id"] not in people:
            raise ValueError(
                f"Leave for unknown person in snapshot: {record['person_id']!r}.")
    intervals = merge_leave_intervals(normalized)
    warnings = []
    for process, window, backup_ids, primary_ids in _process_windows(
            snapshot, today, window_days):
        for backup_id in sorted(set(backup_ids)):
            for leave_start, leave_end in intervals.get(backup_id, []):
                overlap = _overlap(window, (leave_start, leave_end))
                if overlap is None:
                    continue
                # Critical when no primary holder is available for the
                # whole overlap: every primary is on leave covering it.
                all_primary_absent = bool(primary_ids) and all(
                    _covers(intervals.get(primary_id, []), overlap)
                    for primary_id in primary_ids
                )
                warnings.append({
                    "type": "backup_unavailable",
                    "process_id": process["id"],
                    "process_name": process.get("name") or process["id"],
                    "backup_person_id": backup_id,
                    "backup_person_name": (people[backup_id].get("name")
                                           or backup_id),
                    "primary_ids": sorted(set(primary_ids)),
                    "overlap_start": overlap[0].isoformat(),
                    "overlap_end": overlap[1].isoformat(),
                    "lead_days": (overlap[0] - today).days,
                    "severity": "critical" if all_primary_absent else "warning",
                })
    warnings.sort(key=lambda item: (item["lead_days"], item["process_id"],
                                    item["backup_person_id"]))
    return warnings
