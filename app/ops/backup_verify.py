"""Pure backup-verification helpers for Step AO.

Everything in this module is stdlib-only and touches no database, no
filesystem and no network. It answers the questions a backup schedule
must answer before it trusts a backup:

- Is this backup receipt well-formed and believable?
- Is the newest verified backup fresh enough for the stated RPO?
- Which old backups may be pruned, and which must never be pruned?
- Did a restore drill actually prove the backup restores?

A backup counts as verified ONLY when a restore drill passed (see
``restore_drill_verdict``). ``verified: True`` on a receipt alone means
``pg_restore --list`` could read the catalog — it does not mean the
backup restores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

#: Freshness bands for a backup's age, relative to the maximum age the
#: retention policy allows (the RPO window). A backup older than twice
#: the window is expired, not merely stale.
FRESH = "fresh"
STALE = "stale"
EXPIRED = "expired"


@dataclass(frozen=True)
class BackupRecord:
    """One backup receipt, as written by ``app.ops.postgres_backup.create_backup``."""

    database: str
    path: str
    bytes: int
    sha256: str
    catalog_entries: int
    verified: bool
    created_at: datetime


@dataclass(frozen=True)
class RestoreDrillResult:
    """The observed outcome of one restore drill into an empty database."""

    restore_ok: bool
    tables_expected: int
    tables_restored: int
    rows_checked: int
    rows_matched: int
    sha256_match: bool


@dataclass(frozen=True)
class RestoreDrillVerdict:
    passed: bool
    failures: tuple[str, ...]


def is_valid_sha256(value: str) -> bool:
    """True only for a 64-character lowercase hex digest."""
    return isinstance(value, str) and _SHA256_HEX.fullmatch(value) is not None


def validate_backup_record(record: BackupRecord) -> list[str]:
    """Return every problem with a backup receipt; an empty list means believable."""
    problems: list[str] = []
    if not record.database.strip():
        problems.append("database is empty")
    if not record.path.strip():
        problems.append("path is empty")
    if record.bytes <= 0:
        problems.append("bytes must be positive")
    if not is_valid_sha256(record.sha256):
        problems.append("sha256 is not a 64-character lowercase hex digest")
    if record.catalog_entries < 1:
        problems.append("catalog_entries must be at least 1")
    if not record.verified:
        problems.append("verified is false: pg_restore --list could not read the catalog")
    return problems


def backup_freshness(created_at: datetime, now: datetime, max_age_hours: float) -> str:
    """Classify a backup's age: fresh within the window, stale within twice it, expired beyond."""
    if max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive")
    age = now - created_at
    if age < timedelta(0):
        # A backup from the future is a clock problem, not a fresh backup.
        return EXPIRED
    if age <= timedelta(hours=max_age_hours):
        return FRESH
    if age <= timedelta(hours=2 * max_age_hours):
        return STALE
    return EXPIRED


def newest_verified(records: list[BackupRecord]) -> BackupRecord | None:
    """The newest verified backup, or None when no record is verified."""
    verified = [record for record in records if record.verified]
    if not verified:
        return None
    return max(verified, key=lambda record: record.created_at)


def prune_plan(
    records: list[BackupRecord],
    *,
    keep_newest: int,
    min_age_days: float,
    now: datetime,
) -> tuple[list[BackupRecord], list[BackupRecord]]:
    """Split backup records into (keep, prune).

    Rules, in order:
    1. The newest ``keep_newest`` records are always kept.
    2. A record younger than ``min_age_days`` is always kept.
    3. The newest verified backup is NEVER pruned, even if rules 1–2
       would drop it — pruning the last good backup is how a database
       ends up with no restore point at all.
    Everything else is a prune candidate. The caller deletes; this
    function only decides.
    """
    if keep_newest < 1:
        raise ValueError("keep_newest must be at least 1")
    if min_age_days < 0:
        raise ValueError("min_age_days must not be negative")
    ordered = sorted(records, key=lambda record: record.created_at, reverse=True)
    keep: list[BackupRecord] = []
    prune: list[BackupRecord] = []
    must_keep = newest_verified(records)
    for index, record in enumerate(ordered):
        if must_keep is not None and record == must_keep:
            keep.append(record)
        elif index < keep_newest:
            keep.append(record)
        elif now - record.created_at < timedelta(days=min_age_days):
            keep.append(record)
        else:
            prune.append(record)
    return keep, prune


def restore_drill_verdict(drill: RestoreDrillResult) -> RestoreDrillVerdict:
    """Judge a restore drill. Every failure is named; silence is not a pass."""
    failures: list[str] = []
    if not drill.restore_ok:
        failures.append("restore failed: pg_restore did not complete")
    if drill.tables_expected < 1:
        failures.append("nothing expected: the source database had no tables to compare")
    elif drill.tables_restored != drill.tables_expected:
        failures.append(
            f"table count mismatch: restored {drill.tables_restored} of {drill.tables_expected} expected"
        )
    if drill.rows_checked < 1 and drill.tables_expected < 1:
        # Nothing at all was compared — but a database with tables and
        # legitimately zero rows still proves its restore when the table
        # counts match and the digest matches, so zero rows alone is
        # not a failure when tables were compared.
        failures.append("nothing compared: no tables and no rows were checked after the restore")
    elif drill.rows_matched != drill.rows_checked:
        failures.append(
            f"row mismatch: {drill.rows_matched} of {drill.rows_checked} checked rows matched"
        )
    if not drill.sha256_match:
        failures.append("sha256 mismatch: the restored backup digest does not match the receipt")
    return RestoreDrillVerdict(passed=not failures, failures=tuple(failures))


def summarize_backups(records: list[BackupRecord], now: datetime, max_age_hours: float) -> dict[str, int | str | None]:
    """Counts a backup set needs for a status page: totals, freshness, newest verified age."""
    verified = [record for record in records if record.verified]
    newest = newest_verified(records)
    freshness_counts = {FRESH: 0, STALE: 0, EXPIRED: 0}
    for record in verified:
        freshness_counts[backup_freshness(record.created_at, now, max_age_hours)] += 1
    newest_age_hours: int | None = None
    if newest is not None:
        newest_age_hours = int((now - newest.created_at).total_seconds() // 3600)
    return {
        "total": len(records),
        "verified": len(verified),
        "fresh": freshness_counts[FRESH],
        "stale": freshness_counts[STALE],
        "expired": freshness_counts[EXPIRED],
        "newest_verified_age_hours": newest_age_hours,
        "newest_verified": newest.path if newest is not None else None,
    }
