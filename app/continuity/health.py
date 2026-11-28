"""Step G — Company Continuity Engine: health checks and backup manifests.

Pure functions only (standard library, no I/O, no database, no network, no
secrets). Callers — API routes, the worker, the live-push backup step — supply
probe results and file listings; this module only evaluates them.

Two jobs live here:

1. Health roll-up: per-component probes (api, database, workflow_worker,
   ingestion_worker, vector_index, backup) are aggregated to one overall
   continuity status for the dashboard.
2. Backup manifests: a manifest describes one backup (id, kind, time,
   Alembic head, database name, files with size + SHA-256). A manifest is
   verified against the files actually present before any restore is
   attempted; a failed manifest blocks the restore.

Nothing in this module reads live data, executes a backup, or restores
anything. Restore drills run on throwaway ``*_test`` databases only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

Status = Literal["ok", "degraded", "down", "unknown"]

# Severity order: worst first. "unknown" (cannot confirm) degrades the
# roll-up but is less severe than a confirmed "down".
_SEVERITY: dict[Status, int] = {"down": 3, "degraded": 2, "unknown": 1, "ok": 0}

COMPONENTS: tuple[str, ...] = (
    "api",
    "database",
    "workflow_worker",
    "ingestion_worker",
    "vector_index",
    "backup",
)

DEFAULT_PROBE_FRESHNESS = timedelta(minutes=5)
DEFAULT_BACKUP_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class Probe:
    """One evaluated component probe."""

    component: str
    status: Status
    checked_at: datetime
    detail: str = ""
    latency_ms: int | None = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def evaluate_probe(
    component: str,
    status: str,
    checked_at: datetime,
    detail: str = "",
    latency_ms: int | None = None,
) -> Probe:
    """Normalise one raw probe result into a :class:`Probe`.

    Raises ``ValueError`` for an unknown component, an invalid status, or a
    negative latency — bad probe data must fail loudly, never silently
    become "ok".
    """
    if component not in COMPONENTS:
        raise ValueError(f"Unknown continuity component: {component!r}")
    if status not in _SEVERITY:
        raise ValueError(f"Invalid probe status: {status!r}")
    if latency_ms is not None and latency_ms < 0:
        raise ValueError("latency_ms must not be negative")
    return Probe(
        component=component,
        status=status,  # type: ignore[arg-type]
        checked_at=_as_utc(checked_at),
        detail=detail,
        latency_ms=latency_ms,
    )


def aggregate_health(
    probes: list[Probe],
    *,
    now: datetime,
    freshness: timedelta = DEFAULT_PROBE_FRESHNESS,
) -> dict[str, Any]:
    """Roll a list of probes up to one overall continuity status.

    - Overall status is the worst probe status (down > degraded > unknown
      > ok).
    - A probe older than ``freshness`` is stale: it counts as ``unknown``
      (its recorded status is kept in the per-component detail so the
      staleness is visible, not hidden).
    - A component with no probe at all counts as ``unknown``.
    - An empty probe list means nothing was checked: overall ``unknown``.
    """
    now_utc = _as_utc(now)
    by_component: dict[str, Probe] = {}
    for probe in probes:
        # Latest probe wins when a component is probed twice.
        existing = by_component.get(probe.component)
        if existing is None or probe.checked_at >= existing.checked_at:
            by_component[probe.component] = probe

    components: dict[str, dict[str, Any]] = {}
    worst: Status = "ok"
    for name in COMPONENTS:
        probe = by_component.get(name)
        if probe is None:
            effective: Status = "unknown"
            components[name] = {
                "status": effective,
                "detail": "No probe reported for this component.",
                "checked_at": None,
                "stale": True,
            }
        else:
            stale = now_utc - probe.checked_at > freshness
            effective = "unknown" if stale else probe.status
            components[name] = {
                "status": effective,
                "recorded_status": probe.status,
                "detail": probe.detail,
                "checked_at": probe.checked_at.isoformat(),
                "latency_ms": probe.latency_ms,
                "stale": stale,
            }
        if _SEVERITY[effective] > _SEVERITY[worst]:
            worst = effective

    return {
        "status": worst,
        "checked_at": now_utc.isoformat(),
        "components": components,
        "components_ok": sum(1 for c in components.values() if c["status"] == "ok"),
        "components_total": len(COMPONENTS),
    }


def sha256_hex(data: bytes) -> str:
    """SHA-256 hex digest of ``data`` (helper for manifest builders)."""
    return hashlib.sha256(data).hexdigest()


def build_backup_manifest(
    *,
    backup_id: str,
    created_at: datetime,
    alembic_head: str,
    database: str,
    files: list[dict[str, Any]],
    kind: str = "full",
) -> dict[str, Any]:
    """Build a backup manifest dict from file entries.

    Each file entry needs ``path`` (relative, no ``..``), ``size_bytes``
    (non-negative int) and ``sha256`` (64 hex chars). Raises ``ValueError``
    on a bad entry or a duplicate path — a manifest must never describe a
    backup that cannot be verified.
    """
    if not backup_id:
        raise ValueError("backup_id must not be empty")
    if not alembic_head:
        raise ValueError("alembic_head must not be empty")
    if kind not in ("full", "incremental", "drill"):
        raise ValueError(f"Unknown backup kind: {kind!r}")
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for entry in files:
        path = entry.get("path", "")
        size = entry.get("size_bytes")
        digest = entry.get("sha256", "")
        if not isinstance(path, str) or not path or ".." in path.split("/"):
            raise ValueError(f"Invalid backup file path: {path!r}")
        if path in seen:
            raise ValueError(f"Duplicate backup file path: {path!r}")
        seen.add(path)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"Invalid size_bytes for {path!r}: {size!r}")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest.lower())
        ):
            raise ValueError(f"Invalid sha256 for {path!r}: {digest!r}")
        entries.append(
            {"path": path, "size_bytes": size, "sha256": digest.lower()}
        )
    if not entries:
        raise ValueError("A backup manifest must list at least one file")
    return {
        "backup_id": backup_id,
        "kind": kind,
        "created_at": _as_utc(created_at).isoformat(),
        "alembic_head": alembic_head,
        "database": database,
        "files": entries,
        "file_count": len(entries),
        "total_bytes": sum(e["size_bytes"] for e in entries),
    }


def verify_backup_manifest(
    manifest: dict[str, Any],
    *,
    available_files: dict[str, tuple[int, str]],
    now: datetime,
    max_age: timedelta = DEFAULT_BACKUP_MAX_AGE,
    expected_alembic_head: str | None = None,
) -> dict[str, Any]:
    """Verify a manifest against the files actually present.

    ``available_files`` maps relative path -> (size_bytes, sha256 hex).
    Returns ``{"valid": bool, "problems": [str, ...], "checked": int}``.
    Problems cover: missing file, size mismatch, SHA-256 mismatch, stale
    backup, Alembic head mismatch, empty file list, duplicate paths, and
    a malformed manifest. A manifest with any problem must block a
    restore — ``valid`` is True only when ``problems`` is empty.
    """
    problems: list[str] = []
    now_utc = _as_utc(now)

    backup_id = manifest.get("backup_id") or "<missing backup_id>"
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        problems.append(f"{backup_id}: manifest lists no files")
        files = []
    seen: set[str] = set()
    checked = 0
    for entry in files:
        if not isinstance(entry, dict):
            problems.append(f"{backup_id}: malformed file entry {entry!r}")
            continue
        path = entry.get("path", "")
        if path in seen:
            problems.append(f"{backup_id}: duplicate file path {path!r}")
        seen.add(path)
        if path not in available_files:
            problems.append(f"{backup_id}: missing file {path!r}")
            continue
        checked += 1
        size, digest = available_files[path]
        if size != entry.get("size_bytes"):
            problems.append(
                f"{backup_id}: size mismatch for {path!r} "
                f"(manifest {entry.get('size_bytes')}, found {size})"
            )
        if str(digest).lower() != str(entry.get("sha256", "")).lower():
            problems.append(f"{backup_id}: sha256 mismatch for {path!r}")

    created_raw = manifest.get("created_at")
    try:
        created = _as_utc(datetime.fromisoformat(str(created_raw)))
    except (TypeError, ValueError):
        problems.append(f"{backup_id}: unparseable created_at {created_raw!r}")
        created = None
    if created is not None and now_utc - created > max_age:
        problems.append(
            f"{backup_id}: backup is stale "
            f"(created {created.isoformat()}, max age {max_age})"
        )

    if expected_alembic_head is not None:
        head = manifest.get("alembic_head")
        if head != expected_alembic_head:
            problems.append(
                f"{backup_id}: alembic head mismatch "
                f"(manifest {head!r}, expected {expected_alembic_head!r})"
            )

    return {"valid": not problems, "problems": problems, "checked": checked}


def backup_freshness(
    manifest: dict[str, Any] | None,
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_BACKUP_MAX_AGE,
) -> Literal["fresh", "stale", "missing"]:
    """One-word backup freshness for the continuity dashboard."""
    if manifest is None:
        return "missing"
    created_raw = manifest.get("created_at")
    try:
        created = _as_utc(datetime.fromisoformat(str(created_raw)))
    except (TypeError, ValueError):
        return "stale"
    return "fresh" if _as_utc(now) - created <= max_age else "stale"


def summarize_health(
    aggregate: dict[str, Any],
    manifest: dict[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    """One-line roll-up combining component health and backup freshness."""
    status: Status = aggregate["status"]
    freshness = backup_freshness(manifest, now=now)
    note = (
        f"{aggregate['components_ok']}/{aggregate['components_total']} "
        f"components ok; backup {freshness}."
    )
    if freshness == "missing":
        note += " No backup manifest found — take a backup before relying on continuity."
    elif freshness == "stale":
        note += " Backup is older than the allowed age — take a fresh backup."
    return {
        "status": status,
        "backup": freshness,
        "summary": note,
        "continuity_ready": status == "ok" and freshness == "fresh",
    }
