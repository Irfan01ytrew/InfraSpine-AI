"""Step AO slice 6: backup-before-migrate gate.

No migration runs without a fresh verified backup of the target
database first. The rule is enforced by the ``db-migrate-safe``
wrapper (see ``app/cli.py``), not by memory: the wrapper evaluates
the gate, refuses with exit code 2 when it fails, and only then runs
``alembic upgrade head``.

The gate's inputs are explicit. ``last_migration_at`` is the time the
last migration was applied to *this* database (taken from the deploy
record); when it is unknown the gate falls back to freshness alone
and says so in its reasons — a missing deploy record weakens the
gate, it never silently passes it.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.ao_backup_models import STATUS_VERIFIED, BackupReceipt

#: Default recovery-point objective: a backup must be at most this fresh.
DEFAULT_RPO_HOURS = 24


def backup_gate_decision(
    verified_backup_times: list[datetime],
    last_migration_at: datetime | None,
    now: datetime,
    *,
    rpo_hours: float = DEFAULT_RPO_HOURS,
) -> dict:
    """Pure gate verdict. ``verified_backup_times`` are drill-passed backups only.

    A backup counts as verified only when a restore drill passed for
    it — a pg_dump that was never restored proves nothing. Skipped
    runs (empty source database) and failed runs never appear here.
    """
    newest = max(verified_backup_times) if verified_backup_times else None
    blocking: list[str] = []
    notes: list[str] = []
    if newest is None:
        blocking.append("no verified backup exists for this database")
    else:
        if last_migration_at is not None and newest < last_migration_at:
            blocking.append(
                "newest verified backup predates the last migration "
                f"({newest.isoformat()} < {last_migration_at.isoformat()})"
            )
        age = now - newest
        if age > timedelta(hours=rpo_hours):
            blocking.append(
                f"newest verified backup is {age} old, older than the RPO of {rpo_hours}h"
            )
    if last_migration_at is None:
        notes.append("last migration time unknown: gate evaluated on freshness alone")
    return {
        "allowed": not blocking,
        "reasons": blocking + notes,
        "newest_verified_backup": newest.isoformat() if newest else None,
        "rpo_hours": rpo_hours,
    }


async def newest_verified_backup(engine: AsyncEngine, database: str) -> datetime | None:
    """Newest drill-verified backup time for one database, or None."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(engine) as session:
        result = await session.execute(
            select(BackupReceipt.created_at)
            .where(
                BackupReceipt.database == database,
                BackupReceipt.status == STATUS_VERIFIED,
                BackupReceipt.drill_verified.is_(True),
            )
            .order_by(BackupReceipt.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def evaluate_backup_gate(
    engine: AsyncEngine,
    database: str,
    last_migration_at: datetime | None,
    now: datetime,
    *,
    rpo_hours: float = DEFAULT_RPO_HOURS,
) -> dict:
    """Gate verdict against the receipts stored for one database."""
    newest = await newest_verified_backup(engine, database)
    return backup_gate_decision(
        [newest] if newest else [], last_migration_at, now, rpo_hours=rpo_hours
    )


def _run_alembic_upgrade() -> int:
    """Run ``alembic upgrade head`` from the repository root."""
    repo_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["alembic", "upgrade", "head"], cwd=repo_root, check=False
    ).returncode


async def run_migrate_safe(
    engine: AsyncEngine,
    database: str,
    last_migration_at: datetime | None,
    now: datetime,
    *,
    rpo_hours: float = DEFAULT_RPO_HOURS,
    check_only: bool = False,
    run_alembic=_run_alembic_upgrade,
) -> dict:
    """Evaluate the gate; migrate only when it allows it.

    ``check_only`` evaluates the gate without migrating. The alembic
    runner is injectable so tests never run a real migration.
    Returns the gate decision plus, when a migration ran, its exit code.
    """
    decision = await evaluate_backup_gate(
        engine, database, last_migration_at, now, rpo_hours=rpo_hours
    )
    if check_only or not decision["allowed"]:
        return {**decision, "migrated": False, "alembic_exit_code": None}
    exit_code = run_alembic()
    return {**decision, "migrated": exit_code == 0, "alembic_exit_code": exit_code}
