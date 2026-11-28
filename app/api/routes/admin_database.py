"""Admin-only database operations summary (Step AO slice 6). Read-only.

No endpoint here restores, migrates, or deletes anything: restores
stay a human-approved CLI action, migrations run through the
``db-migrate-safe`` gate. This page only shows what the database
looks like — health, backups, drills, integrity, and the migration
gate — so an admin can see the state before deciding.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api.deps import require_admin
from app.db.ao_backup_models import BackupReceipt, RestoreDrill
from app.ops.db_health import collect_db_health
from app.ops.db_integrity import check_integrity
from app.ops.migration_safety import evaluate_backup_gate

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _receipt_dict(row: BackupReceipt | None) -> dict | None:
    if row is None:
        return None
    return {
        "database": row.database,
        "path": row.path,
        "bytes": row.bytes,
        "sha256": row.sha256,
        "catalog_entries": row.catalog_entries,
        "tables": row.tables,
        "rows_total": row.rows_total,
        "status": row.status,
        "drill_verified": row.drill_verified,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _drill_dict(row: RestoreDrill | None) -> dict | None:
    if row is None:
        return None
    return {
        "target_database": row.target_database,
        "passed": row.passed,
        "failures": row.failures,
        "tables_expected": row.tables_expected,
        "tables_restored": row.tables_restored,
        "rows_checked": row.rows_checked,
        "rows_matched": row.rows_matched,
        "sha256_match": row.sha256_match,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/database/summary")
async def database_summary(request: Request) -> dict:
    """Backups, drills, health, integrity, and the migration gate for one database."""
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = request.app.state.engine
    database = engine.url.database or "postgres"
    health = await collect_db_health(engine)
    async with AsyncSession(engine) as session:
        newest_receipt = (
            await session.execute(
                select(BackupReceipt)
                .order_by(BackupReceipt.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        latest_drill = (
            await session.execute(
                select(RestoreDrill).order_by(RestoreDrill.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    integrity = await check_integrity(engine)
    # The API has no deploy record, so the gate is evaluated on
    # freshness alone; the CLI takes --last-migration-at for the full gate.
    gate = await evaluate_backup_gate(engine, database, None, datetime.now(UTC))
    return {
        "database": database,
        "health": health,
        "newest_backup": _receipt_dict(newest_receipt),
        "latest_drill": _drill_dict(latest_drill),
        "integrity": integrity,
        "migration_gate": gate,
        "restore_note": "Restores are human-approved only: run db-restore-empty from the CLI.",
    }
