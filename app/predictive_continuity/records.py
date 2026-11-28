"""Persisted forecast records + visibility-filtered reads (Step L slice 3).

Records are written only by the scan path (:func:`record_scan`),
never by a GET — reads have no side effects. Every read goes through
``app.platform`` (graph membership first, then the org-scope
``forecasts.read`` permission) and through the graph visibility rule:
hidden processes are omitted from lists, counts, and money totals.

Visibility filtering is post-hoc: a stored forecast was computed from
the full twin, and a reader only ever sees the slice they may see.
The filter is pure, so it is unit-tested without a database.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import platform
from app.company_graph.access import scope_for, visible
from app.company_graph.models import GraphRecord
from app.core.errors import NotFoundError
from app.core.permission_catalog import PERMISSION_FORECASTS_READ
from app.db.models import User
from app.predictive_continuity.forecast import MONEY_KINDS, money_at_risk
from app.predictive_continuity.models import ForecastRecord
from app.predictive_continuity.staleness import assess_staleness, trust_for_score

#: Retention defaults (plan §5 slice 3): per (tenant, root, horizon)
#: keep the newest 30 records; delete superseded records older than
#: 90 days. Current (non-superseded) records are never pruned.
RETENTION_KEEP = 30
RETENTION_MAX_AGE_DAYS = 90

__all__ = [
    "RETENTION_KEEP",
    "RETENTION_MAX_AGE_DAYS",
    "filter_forecast_visibility",
    "get_forecast",
    "list_forecasts",
    "prune_forecasts",
    "record_scan",
    "serialize_record",
]


# -------------------------------------------------------------------------------------- visibility filter

def _path_linked_money_ids(forecast):
    """Money entity ids linked to a process via the stored BFS paths.

    The forecast stores each affected entity's discovery path (edge
    views). A money entity counts as linked to a process when a path
    edge connects them. This is a subset of the true linkage — a money
    entity linked by a non-tree edge is missed — so it is only a
    fallback when no snapshot is available. Never over-states: it can
    understate per-process money, never leak hidden money.
    """
    linked = {}
    for entry in forecast.get("affected", []) or []:
        entity = entry.get("entity") or {}
        if entity.get("kind") not in MONEY_KINDS:
            continue
        money_id = entity.get("id")
        for edge in entry.get("path", []) or []:
            for other in (edge.get("source_id"), edge.get("target_id")):
                if other and other != money_id:
                    linked.setdefault(other, set()).add(money_id)
    return linked


def _snapshot_linked_money_ids(snapshot):
    """Money entity ids linked to each entity via snapshot relationships.

    Exact linkage: every relationship in the stored snapshot is an
    edge, so a money entity sharing any edge with a process is linked.
    """
    linked = {}
    for edge in snapshot.get("relationships", []) or []:
        source, target = edge.get("source_id"), edge.get("target_id")
        if not source or not target:
            continue
        linked.setdefault(source, set()).add(target)
        linked.setdefault(target, set()).add(source)
    return linked


def filter_forecast_visibility(forecast, visible_ids, *, snapshot=None):
    """Return a copy of ``forecast`` with hidden entities removed.

    ``visible_ids`` is the set of graph record ids the reader may see,
    or ``None`` for a reader who sees everything (graph owner) — the
    forecast is returned unchanged. Hidden processes are omitted from
    the process list, from every count, and from money totals (top
    level and per process). Hidden owners, backup people, dependency
    names, and live-work items are omitted the same way; a hidden
    unavailable root is reported by id only, never by name.

    Note: this filter is strictly safer than slice 1's compute-time
    visibility — slice 1 does not filter the ``covered_by`` backup
    names, so a hidden backup person's name can appear in a forecast
    computed without ``visible_ids``. This filter drops hidden backup
    entries, so the leak cannot reach a reader.
    """
    if visible_ids is None:
        return copy.deepcopy(forecast)
    visible = set(visible_ids)
    out = copy.deepcopy(forecast)

    affected = [e for e in (forecast.get("affected") or [])
                if (e.get("entity") or {}).get("id") in visible]
    by_id = {}
    for entry in affected:
        entity = entry.get("entity") or {}
        by_id[entity.get("id")] = entity

    # covered_by names leak hidden backup people: drop hidden entries.
    for entry in affected:
        entry["covered_by"] = [
            c for c in (entry.get("covered_by") or [])
            if c.get("person_id") in visible
        ]
    out["affected"] = affected
    out["uncovered"] = [
        u for u in (forecast.get("uncovered") or [])
        if (u.get("entity") or {}).get("id") in visible
    ]

    visible_entities = [entry["entity"] for entry in affected]
    visible_due = [entry["entity"] for entry in affected
                   if entry.get("due_status") == "due_within_horizon"]
    out["money_at_risk"] = money_at_risk(visible_entities)
    out["money_at_risk_due"] = money_at_risk(visible_due)

    hidden_money = {
        entity["id"] for entity in (
            (entry.get("entity") or {}) for entry in (forecast.get("affected") or []))
        if entity.get("kind") in MONEY_KINDS and entity.get("id") not in visible
    }
    if snapshot is not None:
        linked = _snapshot_linked_money_ids(snapshot)
    else:
        linked = _path_linked_money_ids(forecast)
    processes = []
    for number, proc in enumerate(
            [p for p in (forecast.get("processes") or []) if p.get("id") in visible],
            start=1):
        proc = copy.deepcopy(proc)
        proc["n"] = number
        owner_ids = [o for o in (proc.get("owner_ids") or []) if o in visible]
        owner_names = [n for o, n in zip(proc.get("owner_ids") or [],
                                          proc.get("owner_names") or [])
                       if o in visible]
        proc["owner_ids"] = owner_ids
        proc["owner_names"] = owner_names
        proc["dependencies"] = [
            d for d in (proc.get("dependencies") or [])
            if d.get("id") in visible
        ]
        proc["live_work_items"] = [
            item for item in (proc.get("live_work_items") or [])
            if item.get("id") in visible
        ]
        proc["covered_by"] = [
            c for c in (proc.get("covered_by") or [])
            if c.get("person_id") in visible
        ]
        if hidden_money:
            # Recompute from visible linked money only. With a
            # snapshot the linkage is exact; without one it is the
            # path-derived subset (a safe lower bound, never a leak).
            linked_money = [
                by_id[mid] for mid in sorted(linked.get(proc["id"], set()))
                if mid in by_id and by_id[mid].get("kind") in MONEY_KINDS
            ]
            proc["money_at_risk"] = money_at_risk(linked_money)
        processes.append(proc)
    out["processes"] = processes

    out["recovery_waves"] = [
        [entity for entity in wave if entity.get("id") in visible]
        for wave in (forecast.get("recovery_waves") or [])
    ]
    out["recovery_waves"] = [wave for wave in out["recovery_waves"] if wave]
    out["blocked_by_dependency_cycle"] = [
        entity for entity in (forecast.get("blocked_by_dependency_cycle") or [])
        if entity.get("id") in visible
    ]
    out["unavailable"] = [
        {"id": entry.get("id"),
         "name": entry.get("name") if entry.get("id") in visible else entry.get("id"),
         "kind": entry.get("kind")}
        for entry in (forecast.get("unavailable") or [])
    ]
    due_processes = [p for p in processes if p.get("due_status") == "due_within_horizon"]
    out["counts"] = {
        "processes": len(processes),
        "processes_due_within_horizon": len(due_processes),
        "affected": len(affected),
        "uncovered": len(out["uncovered"]),
    }
    return out


# -------------------------------------------------------------------------------------- service

def _parse_as_of(value, *, fallback):
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return fallback
    if isinstance(value, datetime):
        return value
    return fallback


async def _visible_ids_for(db: AsyncSession, scope) -> set[str] | None:
    """Graph record ids the caller may see, or None for the owner."""
    if scope.role == "owner":
        return None
    rows = await db.scalars(select(GraphRecord.id).where(visible(scope)))
    return {str(row) for row in rows}


async def _require_read(db: AsyncSession, tenant_id: UUID, user: User):
    """Graph membership (404 for non-members) then ``forecasts.read`` (403).

    Returns ``(scope, visible_ids)``. The order matters: a non-member
    learns nothing about whether the tenant or the permission exists.
    """
    scope = await scope_for(db, tenant_id, user)
    principal = platform.principal_for(user)
    org = await platform.ensure_org_scope(db)
    await platform.require(db, principal, PERMISSION_FORECASTS_READ, org.id)
    return scope, await _visible_ids_for(db, scope)


async def record_scan(db: AsyncSession, tenant_id: UUID, scan_result: dict,
                      snapshot: dict, *, created_by: UUID | None = None,
                      now: datetime | None = None) -> list[ForecastRecord]:
    """Persist one record per forecast in a scan result.

    Idempotent per fingerprint: re-recording the same scan returns the
    existing rows instead of duplicating them. Recording a new
    fingerprint for the same (tenant, root, horizon) supersedes the
    older records and stamps each with the drift score between its
    snapshot and the new one (``None`` when either snapshot is
    malformed — the record is still written, staleness unknown).

    Called only by the scan path (slice 2's ``scan_forecasts`` output),
    never by a GET.
    """
    moment = now or datetime.now(timezone.utc)
    scanned_at = _parse_as_of(scan_result.get("scanned_at"), fallback=moment)
    records = []
    for entry in scan_result.get("forecasts", []) or []:
        forecast = entry.get("forecast") or {}
        fingerprint = entry.get("fingerprint") or forecast.get("fingerprint")
        if not fingerprint:
            raise ValueError("scan forecast has no fingerprint")
        root_id = str(entry.get("root_id") or "")
        root_kind = str(entry.get("root_kind") or "")
        horizon_days = int(entry.get("horizon_days", forecast.get("horizon_days", 0)))
        values = {
            "tenant_id": tenant_id,
            "root_id": root_id,
            "root_kind": root_kind,
            "horizon_days": horizon_days,
            "fingerprint": fingerprint,
            "forecast": forecast,
            "snapshot": snapshot,
            "as_of": scanned_at,
            "superseded": False,
            "created_by": created_by,
        }
        await db.execute(
            pg_insert(ForecastRecord).values(**values).on_conflict_do_nothing(
                index_elements=["tenant_id", "root_id", "horizon_days", "fingerprint"])
        )
        record = await db.scalar(select(ForecastRecord).where(
            ForecastRecord.tenant_id == tenant_id,
            ForecastRecord.root_id == root_id,
            ForecastRecord.horizon_days == horizon_days,
            ForecastRecord.fingerprint == fingerprint,
        ))
        older = await db.scalars(select(ForecastRecord).where(
            ForecastRecord.tenant_id == tenant_id,
            ForecastRecord.root_id == root_id,
            ForecastRecord.horizon_days == horizon_days,
            ForecastRecord.fingerprint != fingerprint,
            ForecastRecord.superseded.is_(False),
        ))
        for old in older:
            old.superseded = True
            try:
                old.drift_score = assess_staleness(old.snapshot, snapshot)["drift_score"]
            except ValueError:
                old.drift_score = None
        records.append(record)
    await db.flush()
    return records


def serialize_record(record: ForecastRecord, visible_ids: set[str] | None) -> dict:
    """Public view of a record: metadata + visibility-filtered forecast."""
    if not record.superseded:
        # The current record is the freshest scan: trustworthy by
        # definition. A superseded record's trust comes from its stored
        # drift score (None = the snapshots could not be diffed).
        trust = "trustworthy"
    else:
        trust = trust_for_score(record.drift_score)
    return {
        "id": str(record.id),
        "tenant_id": str(record.tenant_id),
        "root_id": record.root_id,
        "root_kind": record.root_kind,
        "horizon_days": record.horizon_days,
        "fingerprint": record.fingerprint,
        "as_of": record.as_of.isoformat() if record.as_of else None,
        "superseded": record.superseded,
        "drift_score": record.drift_score,
        "trust": trust,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "forecast": filter_forecast_visibility(
            record.forecast, visible_ids, snapshot=record.snapshot),
    }


async def list_forecasts(db: AsyncSession, tenant_id: UUID, user: User, *,
                         include_superseded: bool = False,
                         limit: int = 50, offset: int = 0) -> list[dict]:
    """Visibility-filtered forecast records for a tenant, newest first.

    Read-only: issues SELECTs only. Raises ``NotFoundError`` for
    non-members or unknown tenants, ``PermissionDeniedError`` for
    members without ``forecasts.read``.
    """
    _, visible_ids = await _require_read(db, tenant_id, user)
    stmt = select(ForecastRecord).where(ForecastRecord.tenant_id == tenant_id)
    if not include_superseded:
        stmt = stmt.where(ForecastRecord.superseded.is_(False))
    stmt = (stmt.order_by(ForecastRecord.created_at.desc(), ForecastRecord.id.desc())
            .limit(max(1, min(limit, 100))).offset(max(0, offset)))
    rows = await db.scalars(stmt)
    return [serialize_record(row, visible_ids) for row in rows]


async def get_forecast(db: AsyncSession, tenant_id: UUID, record_id: UUID,
                       user: User) -> dict:
    """One visibility-filtered forecast record. Read-only (SELECTs only).

    Raises ``NotFoundError`` for non-members, unknown tenants, or
    unknown record ids; ``PermissionDeniedError`` for members without
    ``forecasts.read``.
    """
    _, visible_ids = await _require_read(db, tenant_id, user)
    record = await db.scalar(select(ForecastRecord).where(
        ForecastRecord.id == record_id, ForecastRecord.tenant_id == tenant_id))
    if record is None:
        raise NotFoundError("Forecast not found.")
    return serialize_record(record, visible_ids)


async def prune_forecasts(db: AsyncSession, tenant_id: UUID, *,
                          keep: int = RETENTION_KEEP,
                          max_age_days: int = RETENTION_MAX_AGE_DAYS,
                          now: datetime | None = None) -> int:
    """Delete stale superseded records per the retention policy.

    Deletes (a) superseded records older than ``max_age_days`` and
    (b) superseded records beyond the newest ``keep`` per (root,
    horizon). Current (non-superseded) records are never pruned.
    Maintenance only — call from a scheduled job or CLI, never from a
    GET. Returns the number of rows deleted.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=max_age_days)
    aged = await db.execute(delete(ForecastRecord).where(
        ForecastRecord.tenant_id == tenant_id,
        ForecastRecord.superseded.is_(True),
        ForecastRecord.created_at < cutoff,
    ))
    deleted = aged.rowcount or 0
    keys = (await db.execute(select(
        ForecastRecord.root_id, ForecastRecord.horizon_days).where(
        ForecastRecord.tenant_id == tenant_id).distinct())).all()
    for root_id, horizon_days in keys:
        ids = await db.scalars(select(ForecastRecord.id).where(
            ForecastRecord.tenant_id == tenant_id,
            ForecastRecord.root_id == root_id,
            ForecastRecord.horizon_days == horizon_days,
            ForecastRecord.superseded.is_(True),
        ).order_by(ForecastRecord.created_at.desc(), ForecastRecord.id.desc())
        .offset(max(0, keep)))
        stale_ids = list(ids)
        if stale_ids:
            result = await db.execute(delete(ForecastRecord).where(
                ForecastRecord.id.in_(stale_ids)))
            deleted += result.rowcount or 0
    await db.flush()
    return deleted
