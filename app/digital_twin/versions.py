"""Persisted twin snapshot versions + drift history (Step K v2 slice 2).

The twin snapshot builder (Step S) builds a snapshot on demand and
throws it away. This module stores versions so drift has a timeline
and predictions can be checked against the twin they were computed
from (plan §1 item 2, slice 3's job).

Rules (plan §4/§5 slice 2):

- **Append-only.** A version is validated and fingerprinted before
  it is stored; the same ``(tenant_id, fingerprint)`` is never stored
  twice (recording the same snapshot again returns the existing
  version, no new row); rows are never edited. Old rows only leave
  through the retention rule.
- **Retention (owner decision §6, defaults).** Keep the newest
  ``DEFAULT_RETENTION_VERSIONS`` (30) versions and nothing older than
  ``DEFAULT_RETENTION_DAYS`` (90) days. Pruning runs inside
  :func:`record_version` after each insert.
- **Fingerprint is recomputed from content.** A stored ``fingerprint``
  field on the incoming snapshot is ignored — the row key is always
  the SHA-256 of the canonical entities + relationships, so a tampered
  label cannot misidentify a version. The stored snapshot copy drops
  the ``fingerprint`` key for the same reason.
- **Money is exposure only, owner-only on read.** The stored drift
  report carries per-currency money deltas; the API shows them only
  to the tenant owner (see :func:`redact_money_for_viewer`). This
  module never approves, dispatches, grants access, or pays anything
  — it only records versions.
- **No record endpoint.** Slice 2 adds no write route: versions are
  recorded by calling :func:`record_version` (the twin service /
  worker wires that up in a later slice). The API in
  ``app.api.routes.digital_twin`` is read-only.

All functions here are async and take an ``AsyncSession``; the pure
helpers (:func:`redact_money_for_viewer`, :func:`version_summary`)
take plain data and are unit-testable without a database.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select

from app.db.twin_snapshot_models import TwinSnapshotVersion
from app.digital_twin import drift

#: Retention defaults (plan §6 — confirm or change with the owner).
DEFAULT_RETENTION_VERSIONS = 30
DEFAULT_RETENTION_DAYS = 90

#: Hard cap on list responses so a version list stays bounded.
MAX_LIST_LIMIT = 100

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_RETENTION_VERSIONS",
    "MAX_LIST_LIMIT",
    "get_drift",
    "get_version",
    "list_versions",
    "prune_versions",
    "record_version",
    "redact_money_for_viewer",
    "version_summary",
]


def _parse_as_of(value, *, now):
    """Parse an ``as_of`` value (datetime, ISO string, or None → now)."""
    if value is None:
        return now
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Invalid as_of timestamp: {value!r}.") from None
    else:
        raise ValueError(f"Invalid as_of timestamp: {value!r}.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _normalize_source_freshness(value):
    """Source freshness must be a list of dicts with a string ``name``."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("source_freshness must be a list of source dicts.")
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("source_freshness entries must be dicts.")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("source_freshness entries need a non-empty string 'name'.")
        try:
            json.dumps(item)
        except (TypeError, ValueError):
            raise ValueError(f"source_freshness entry {name!r} is not JSON data.") from None
        normalized.append(dict(item))
    return normalized


def _content_fingerprint(snapshot):
    """Fingerprint of a snapshot computed from its content only.

    Any stored ``fingerprint`` field is ignored, so the key is always
    the content hash — identical to the slice-1 computed fingerprint.
    """
    content = {key: value for key, value in snapshot.items() if key != "fingerprint"}
    return drift.snapshot_fingerprint(content)


def _canonical_snapshot(snapshot, *, as_of):
    """Stored copy: no stored fingerprint, ``as_of`` always present."""
    stored = {key: value for key, value in snapshot.items() if key != "fingerprint"}
    stored["as_of"] = as_of.isoformat()
    return stored


def version_summary(row):
    """Small read-only summary of a stored version row.

    Never includes the full snapshot JSON or money amounts — the list
    endpoint stays bounded and amount-free; money lives only in the
    drift report, which the API redacts for non-owners.
    """
    drift_report = row.drift_report
    if drift_report is not None:
        counts = drift_report.get("counts", {})
        entity_count = counts.get("entities_new")
        relationship_count = counts.get("relationships_new")
        drift_score = drift_report.get("drift_score")
        trust = drift_report.get("trust")
        stale_sources = drift_report.get("stale_sources", [])
    else:
        entity_count = len((row.snapshot or {}).get("entities", []))
        relationship_count = len((row.snapshot or {}).get("relationships", []))
        drift_score = None
        trust = None
        stale_sources = list((row.snapshot or {}).get("stale_sources", []))
    return {
        "id": str(row.id),
        "seq": row.seq,
        "tenant_id": str(row.tenant_id),
        "fingerprint": row.fingerprint,
        "as_of": row.as_of.isoformat(),
        "created_at": row.created_at.isoformat(),
        "drift_score": drift_score,
        "trust": trust,
        "entity_count": entity_count,
        "relationship_count": relationship_count,
        "stale_sources": sorted(stale_sources),
        "source_freshness": copy.deepcopy(row.source_freshness or []),
        "is_first_version": drift_report is None,
    }


def redact_money_for_viewer(drift_report):
    """Copy of a drift report with money amounts removed (non-owners).

    Returns ``None`` when the report itself is ``None`` (first
    version). Amounts are owner-only (plan §4); the redacted copy
    keeps the score, trust, counts and structure, and says so
    explicitly with ``money_redacted: true``.
    """
    if drift_report is None:
        return None
    redacted = copy.deepcopy(drift_report)
    redacted["money"] = None
    redacted["money_redacted"] = True
    return redacted


async def record_version(db, tenant_id, snapshot, *, as_of=None, source_freshness=None, now=None):
    """Validate, fingerprint, and store one twin snapshot version.

    Idempotent: recording the same snapshot for the same tenant
    returns the existing version without writing a row. The drift
    report is computed against the latest previous version for the
    tenant (``None`` for the first version). Retention pruning runs
    in the same transaction after the insert. Raises ``ValueError``
    for a malformed snapshot, bad ``as_of``, or bad source freshness
    — nothing is written in that case.
    """
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    if not isinstance(snapshot, dict):
        raise ValueError("Snapshot must be a dict.")
    drift.validate_snapshot(snapshot)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    resolved_as_of = _parse_as_of(
        as_of if as_of is not None else snapshot.get("as_of"), now=current
    )
    freshness = _normalize_source_freshness(source_freshness)
    fingerprint = _content_fingerprint(snapshot)

    existing = await db.scalar(
        select(TwinSnapshotVersion).where(
            TwinSnapshotVersion.tenant_id == tenant_id,
            TwinSnapshotVersion.fingerprint == fingerprint,
        )
    )
    if existing is not None:
        return version_summary(existing)

    previous = await db.scalar(
        select(TwinSnapshotVersion)
        .where(TwinSnapshotVersion.tenant_id == tenant_id)
        .order_by(TwinSnapshotVersion.seq.desc())
        .limit(1)
    )
    stored_snapshot = _canonical_snapshot(snapshot, as_of=resolved_as_of)
    drift_report = None
    if previous is not None:
        drift_report = drift.diff_snapshots(previous.snapshot, stored_snapshot)

    row = TwinSnapshotVersion(
        tenant_id=tenant_id,
        fingerprint=fingerprint,
        as_of=resolved_as_of,
        snapshot=stored_snapshot,
        drift_report=drift_report,
        source_freshness=freshness,
    )
    db.add(row)
    await db.flush()
    # Summary first: created_at is a server default, so reading it
    # loads it now; pruning afterwards can only remove older rows
    # (the new row is the newest and fresh, so it always survives
    # under any sane retention setting).
    summary = version_summary(row)
    await prune_versions(db, tenant_id, now=current)
    return summary


async def prune_versions(db, tenant_id, *, keep=DEFAULT_RETENTION_VERSIONS,
                         max_age_days=DEFAULT_RETENTION_DAYS, now=None):
    """Delete versions beyond the retention rule; returns how many.

    A version leaves when it is older than ``max_age_days`` OR when
    it falls outside the newest ``keep`` versions for the tenant.
    Derived data only — a pruned version can be rebuilt by recording
    the snapshot again.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1.")
    if max_age_days < 0:
        raise ValueError("max_age_days must not be negative.")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current - timedelta(days=max_age_days)
    rows = (
        await db.scalars(
            select(TwinSnapshotVersion)
            .where(TwinSnapshotVersion.tenant_id == tenant_id)
            .order_by(TwinSnapshotVersion.seq.desc())
        )
    ).all()
    doomed = [
        row.id
        for index, row in enumerate(rows)
        if index >= keep or row.created_at < cutoff
    ]
    if doomed:
        await db.execute(
            delete(TwinSnapshotVersion).where(TwinSnapshotVersion.id.in_(doomed))
        )
    return len(doomed)


async def list_versions(db, tenant_id, *, limit=DEFAULT_RETENTION_VERSIONS):
    """Newest-first summaries for one tenant (never the full snapshots)."""
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    clamped = max(1, min(MAX_LIST_LIMIT, int(limit)))
    rows = (
        await db.scalars(
            select(TwinSnapshotVersion)
            .where(TwinSnapshotVersion.tenant_id == tenant_id)
            .order_by(TwinSnapshotVersion.seq.desc())
            .limit(clamped)
        )
    ).all()
    return [version_summary(row) for row in rows]


def _check_fingerprint(fingerprint):
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError(f"Invalid twin version fingerprint: {fingerprint!r}.")
    return fingerprint


async def get_version(db, tenant_id, fingerprint):
    """One stored version row, or ``None`` when it does not exist."""
    if not isinstance(tenant_id, UUID):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}.")
    _check_fingerprint(fingerprint)
    return await db.scalar(
        select(TwinSnapshotVersion).where(
            TwinSnapshotVersion.tenant_id == tenant_id,
            TwinSnapshotVersion.fingerprint == fingerprint,
        )
    )


async def get_drift(db, tenant_id, fingerprint):
    """Stored drift report for one version, or ``None`` if no such version.

    Returns ``{"version": summary, "drift": report | None,
    "is_first_version": bool}`` — the drift is ``None`` for the first
    version recorded (there was nothing to diff against). Callers
    redact money for non-owners with :func:`redact_money_for_viewer`.
    """
    row = await get_version(db, tenant_id, fingerprint)
    if row is None:
        return None
    return {
        "version": version_summary(row),
        "drift": copy.deepcopy(row.drift_report),
        "is_first_version": row.drift_report is None,
    }
