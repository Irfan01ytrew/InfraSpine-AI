"""Step AO slice 2: backup scheduler + restore drills.

The schedule runs the existing ``create_backup`` (pg_dump custom format),
writes a receipt row for every attempt, then proves the backup restores
by restoring it into an empty throwaway database and comparing tables,
rows and the SHA-256 digest. Only a drill that passes marks the receipt
``drill_verified``.

Decisions recorded here (plan doc §3):

- An empty database (zero catalog entries — ``create_backup`` refuses
  it) is recorded as a *skipped* run with reason ``empty_database``.
  The skip is visible in the receipts table; the database is never
  counted as backed up. An empty database has nothing to restore, so
  there is nothing to prove — but the absence of a backup must be
  explicit, never silent.
- A drill database is created and dropped by this module only when its
  name starts with ``ao_drill_``. Anything else is refused: the
  scheduler never drops a database it did not create for a drill.
- Restores run only into empty databases (``restore_into_empty_database``
  enforces that) and only on throwaway drill targets. A restore of a
  live database is a human-approved operation, never a scheduled one.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.ao_backup_models import (
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_VERIFIED,
    BackupReceipt,
    RestoreDrill,
)
from app.db.session import create_engine
from app.ops.backup_verify import RestoreDrillResult, restore_drill_verdict
from app.ops.postgres_backup import create_backup, restore_into_empty_database

#: A backup run that produced no file because the source had no catalog entries.
EMPTY_DATABASE = "empty_database"
#: Prefix every drill database name must carry; the scheduler drops only these.
DRILL_TARGET_PREFIX = "ao_drill_"

_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_USER_SCHEMAS = "schemaname NOT IN ('pg_catalog', 'information_schema') AND schemaname NOT LIKE 'pg_toast%'"


def drill_database_name(source_database: str, stamp: str) -> str:
    """Deterministic drill-database name: ``ao_drill_<source>_<stamp>``, a valid identifier."""
    base = re.sub(r"[^a-z0-9_]", "_", source_database.lower()).strip("_") or "db"
    stamp_part = re.sub(r"[^a-z0-9_]", "_", stamp.lower()).strip("_") or "drill"
    name = f"{DRILL_TARGET_PREFIX}{base}_{stamp_part}"[:63].rstrip("_")
    if not _DATABASE_NAME.fullmatch(name):
        raise ValueError(f"cannot build a drill database name from {source_database!r}")
    return name


def is_empty_catalog_error(exc: Exception) -> bool:
    """True when ``create_backup`` refused a database with no catalog entries."""
    return "no catalog entries" in str(exc)


def sha256_file(path: Path) -> str:
    """SHA-256 hex digest of a file, streamed so large backups stay small in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


async def table_row_counts(engine: AsyncEngine) -> dict[str, int]:
    """Row count per user table (``schema.table``), read-only."""
    async with engine.connect() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT schemaname, tablename FROM pg_tables "
                    f"WHERE {_USER_SCHEMAS} ORDER BY schemaname, tablename"
                )
            )
        ).all()
        counts: dict[str, int] = {}
        for schema, table in tables:
            count = await conn.scalar(
                text(f'SELECT count(*) FROM "{schema}"."{table}"')  # noqa: S608
            )
            counts[f"{schema}.{table}"] = int(count or 0)
    return counts


async def ensure_database(admin_url: str, name: str) -> None:
    """CREATE DATABASE on the maintenance connection. Refuses invalid names."""
    if not _DATABASE_NAME.fullmatch(name):
        raise ValueError(f"refusing to create database with invalid name {name!r}")
    engine = create_engine(admin_url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'CREATE DATABASE "{name}"'))  # noqa: S608
    finally:
        await engine.dispose()


async def drop_database(admin_url: str, name: str) -> None:
    """DROP DATABASE, but only for drill databases this module created."""
    if not name.startswith(DRILL_TARGET_PREFIX):
        raise ValueError(
            f"refusing to drop {name!r}: the scheduler drops only {DRILL_TARGET_PREFIX}* databases"
        )
    engine = create_engine(admin_url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))  # noqa: S608
    finally:
        await engine.dispose()


async def _insert_receipt(
    engine: AsyncEngine,
    *,
    database: str,
    path: str,
    status: str,
    bytes_: int = 0,
    sha256: str = "",
    catalog_entries: int = 0,
    tables: int | None = None,
    rows_total: int | None = None,
    error: str | None = None,
) -> str:
    async with engine.begin() as conn:
        receipt_id = (
            await conn.execute(
                insert(BackupReceipt).values(
                    database=database,
                    path=path,
                    bytes=bytes_,
                    sha256=sha256,
                    catalog_entries=catalog_entries,
                    tables=tables,
                    rows_total=rows_total,
                    status=status,
                    error=error,
                ).returning(BackupReceipt.id)
            )
        ).scalar_one()
    return str(receipt_id)


async def _insert_drill(
    engine: AsyncEngine,
    *,
    receipt_id: str | None,
    target_database: str,
    passed: bool,
    failures: list[str],
    tables_expected: int,
    tables_restored: int,
    rows_checked: int,
    rows_matched: int,
    sha256_match: bool,
) -> str:
    async with engine.begin() as conn:
        drill_id = (
            await conn.execute(
                insert(RestoreDrill).values(
                    receipt_id=receipt_id,
                    target_database=target_database,
                    passed=passed,
                    failures=list(failures),
                    tables_expected=tables_expected,
                    tables_restored=tables_restored,
                    rows_checked=rows_checked,
                    rows_matched=rows_matched,
                    sha256_match=sha256_match,
                ).returning(RestoreDrill.id)
            )
        ).scalar_one()
    return str(drill_id)


async def run_restore_drill(
    *,
    source_engine: AsyncEngine,
    drill_url: str,
    backup_path: Path,
    receipt_sha256: str,
) -> RestoreDrillResult:
    """Restore a backup into an empty database and compare it with the source.

    The caller creates the (empty) drill database first; this function
    restores into it and compares table counts, row counts and the file
    digest. It never touches the source database and never drops anything.
    """
    source_counts = await table_row_counts(source_engine)
    await restore_into_empty_database(drill_url, backup_path)
    drill_engine = create_engine(drill_url, pool_size=1, max_overflow=0)
    try:
        restored_counts = await table_row_counts(drill_engine)
    finally:
        await drill_engine.dispose()
    rows_checked = sum(source_counts.values())
    rows_matched = sum(
        count for table, count in source_counts.items() if restored_counts.get(table) == count
    )
    return RestoreDrillResult(
        restore_ok=True,
        tables_expected=len(source_counts),
        tables_restored=len(restored_counts),
        rows_checked=rows_checked,
        rows_matched=rows_matched,
        sha256_match=sha256_file(backup_path) == receipt_sha256,
    )


async def run_backup_cycle(
    *,
    source_url: str,
    backup_dir: Path,
    admin_url: str,
    receipt_engine: AsyncEngine,
    drill_db_name: str | None = None,
    stamp: str | None = None,
) -> dict:
    """One scheduled backup + restore drill. Returns a summary; never raises for a failed backup.

    Every attempt writes a receipt row (verified, skipped, or failed) and
    every drill writes a drill row. The returned summary always carries
    ``passed`` — False means no verified, drill-proven backup exists for
    this run, and the caller (CLI exit code, scheduler log) must treat
    that as a failure, not a quiet skip.
    """
    from sqlalchemy.engine import make_url

    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    source_database = make_url(source_url).database or "postgres"
    backup_path = (backup_dir / f"{source_database}_{stamp}.dump").expanduser().resolve()
    source_engine = create_engine(source_url, pool_size=1, max_overflow=0)
    try:
        try:
            source_counts = await table_row_counts(source_engine)
            receipt = await asyncio.to_thread(create_backup, source_url, backup_path)
        except Exception as exc:  # pg_dump failure, empty catalog, verification failure
            if is_empty_catalog_error(exc):
                receipt_id = await _insert_receipt(
                    receipt_engine,
                    database=source_database,
                    path=str(backup_path),
                    status=STATUS_SKIPPED,
                    error=f"{EMPTY_DATABASE}: the source database has no catalog entries; nothing to back up",
                )
                return {
                    "passed": False,
                    "status": STATUS_SKIPPED,
                    "reason": EMPTY_DATABASE,
                    "receipt_id": receipt_id,
                    "backup_path": str(backup_path),
                    "failures": ["empty database: no catalog entries to back up"],
                }
            receipt_id = await _insert_receipt(
                receipt_engine,
                database=source_database,
                path=str(backup_path),
                status=STATUS_FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            return {
                "passed": False,
                "status": STATUS_FAILED,
                "receipt_id": receipt_id,
                "backup_path": str(backup_path),
                "failures": [f"backup failed: {type(exc).__name__}: {exc}"],
            }

        tables = len(source_counts)
        rows_total = sum(source_counts.values())
        receipt_id = await _insert_receipt(
            receipt_engine,
            database=source_database,
            path=str(backup_path),
            status=STATUS_VERIFIED,
            bytes_=int(receipt["bytes"]),
            sha256=str(receipt["sha256"]),
            catalog_entries=int(receipt["catalog_entries"]),
            tables=tables,
            rows_total=rows_total,
        )

        drill_name = drill_db_name or drill_database_name(source_database, stamp)
        drill_url = str(make_url(admin_url).set(database=drill_name))
        await ensure_database(admin_url, drill_name)
        try:
            try:
                drill = await run_restore_drill(
                    source_engine=source_engine,
                    drill_url=drill_url,
                    backup_path=backup_path,
                    receipt_sha256=str(receipt["sha256"]),
                )
            except Exception as exc:  # restore failure: a failed drill, recorded — never raised
                drill = RestoreDrillResult(
                    restore_ok=False,
                    tables_expected=tables,
                    tables_restored=0,
                    rows_checked=rows_total,
                    rows_matched=0,
                    sha256_match=False,
                )
                verdict = restore_drill_verdict(drill)
                failures = [f"restore failed: {type(exc).__name__}: {exc}", *verdict.failures]
                drill_id = await _insert_drill(
                    receipt_engine,
                    receipt_id=receipt_id,
                    target_database=drill_name,
                    passed=False,
                    failures=failures,
                    tables_expected=drill.tables_expected,
                    tables_restored=drill.tables_restored,
                    rows_checked=drill.rows_checked,
                    rows_matched=drill.rows_matched,
                    sha256_match=drill.sha256_match,
                )
                return {
                    "passed": False,
                    "status": STATUS_VERIFIED,
                    "receipt_id": receipt_id,
                    "drill_id": drill_id,
                    "backup_path": str(backup_path),
                    "tables_expected": drill.tables_expected,
                    "tables_restored": drill.tables_restored,
                    "rows_checked": drill.rows_checked,
                    "rows_matched": drill.rows_matched,
                    "sha256_match": drill.sha256_match,
                    "failures": failures,
                }
        finally:
            await drop_database(admin_url, drill_name)
        verdict = restore_drill_verdict(drill)
        drill_id = await _insert_drill(
            receipt_engine,
            receipt_id=receipt_id,
            target_database=drill_name,
            passed=verdict.passed,
            failures=list(verdict.failures),
            tables_expected=drill.tables_expected,
            tables_restored=drill.tables_restored,
            rows_checked=drill.rows_checked,
            rows_matched=drill.rows_matched,
            sha256_match=drill.sha256_match,
        )
        if verdict.passed:
            async with receipt_engine.begin() as conn:
                await conn.execute(
                    update(BackupReceipt)
                    .where(BackupReceipt.id == receipt_id)
                    .values(drill_verified=True)
                )
        return {
            "passed": verdict.passed,
            "status": STATUS_VERIFIED,
            "receipt_id": receipt_id,
            "drill_id": drill_id,
            "backup_path": str(backup_path),
            "tables_expected": drill.tables_expected,
            "tables_restored": drill.tables_restored,
            "rows_checked": drill.rows_checked,
            "rows_matched": drill.rows_matched,
            "sha256_match": drill.sha256_match,
            "failures": list(verdict.failures),
        }
    finally:
        await source_engine.dispose()
