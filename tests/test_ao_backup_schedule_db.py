"""DB tests for Step AO slice 2: full backup cycle + restore drill on throwaway databases.

Run with --noconftest: these tests build their own engines and create/drop
scratch databases on the throwaway cluster. Nothing here touches the live
Codespace database or the owner's PC database.

Each test runs its whole async body inside ONE asyncio.run call: an async
engine created under one event loop must never be used or disposed under
another.
"""

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.db.ao_backup_models import BackupReceipt, RestoreDrill
from app.db.base import Base
from app.db.session import create_engine, create_sessionmaker
from app.ops.backup_schedule import (
    ensure_database,
    run_backup_cycle,
    run_restore_drill,
)
from app.ops.postgres_backup import create_backup

TEST_URL = os.environ.get("TEST_DATABASE_URL")
ADMIN_URL = str(make_url(TEST_URL).set(database="postgres")) if TEST_URL else None

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set")


def _source_url(name: str) -> str:
    return str(make_url(TEST_URL).set(database=name))


async def _create_ao_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[BackupReceipt.__table__, RestoreDrill.__table__]
            )
        )


async def _make_source(db_name: str, with_tables: bool):
    await ensure_database(ADMIN_URL, db_name)
    engine = create_engine(_source_url(db_name), pool_size=1, max_overflow=0)
    try:
        if with_tables:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE TABLE t1 (id serial PRIMARY KEY, v text)"))
                await conn.execute(text("CREATE TABLE t2 (id serial PRIMARY KEY, v text)"))
                await conn.execute(text("INSERT INTO t1 (v) VALUES ('a'), ('b'), ('c')"))
                await conn.execute(text("INSERT INTO t2 (v) VALUES ('x'), ('y')"))
    finally:
        await engine.dispose()


async def _drop_scratch_database(db_name: str):
    """Test-only cleanup: drop a scratch database this test created.

    Deliberately separate from ``drop_database`` (which only ever drops
    ``ao_drill_*`` targets): the scheduler must never drop a source
    database, but a test must clean up after itself.
    """
    assert db_name in ("ao_src_test", "ao_empty_test", "ao_drill_busy_test")
    engine = create_engine(ADMIN_URL, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))  # noqa: S608
    finally:
        await engine.dispose()


async def _drop_database_quietly(db_name: str):
    try:
        await _drop_scratch_database(db_name)
    except Exception:
        pass


def test_full_backup_cycle_passes_and_records_receipt_and_drill(tmp_path: Path):
    receipt_engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)

    async def _body():
        # The receipt engine is disposed inside this same event loop: an
        # async engine must never be used or disposed under another loop.
        try:
            await _create_ao_tables(receipt_engine)
            await _make_source("ao_src_test", with_tables=True)
            summary = await run_backup_cycle(
                source_url=_source_url("ao_src_test"),
                backup_dir=tmp_path,
                admin_url=ADMIN_URL,
                receipt_engine=receipt_engine,
                drill_db_name="ao_drill_cycle_test",
                stamp="20261128T000000Z",
            )
            # ORM reads go through a session: in this SQLAlchemy version a
            # raw connection returns raw column rows for select(Model).
            maker = create_sessionmaker(receipt_engine)
            async with maker() as session:
                receipt = (
                    await session.execute(
                        select(BackupReceipt).where(BackupReceipt.id == summary["receipt_id"])
                    )
                ).scalars().one()
                drill = (
                    await session.execute(
                        select(RestoreDrill).where(RestoreDrill.id == summary["drill_id"])
                    )
                ).scalars().one()
            async with receipt_engine.connect() as conn:
                drill_db_gone = (
                    await conn.execute(
                        text("SELECT count(*) FROM pg_database WHERE datname = 'ao_drill_cycle_test'")
                    )
                ).scalar()
            return summary, receipt, drill, drill_db_gone
        finally:
            await receipt_engine.dispose()

    try:
        summary, receipt, drill, drill_db_gone = asyncio.run(_body())
    finally:
        asyncio.run(_drop_database_quietly("ao_src_test"))

    assert summary["passed"] is True
    assert summary["failures"] == []
    assert summary["tables_expected"] == summary["tables_restored"] == 2
    assert summary["rows_checked"] == summary["rows_matched"] == 5
    assert summary["sha256_match"] is True
    assert receipt.status == "verified"
    assert receipt.drill_verified is True
    assert receipt.tables == 2
    assert receipt.rows_total == 5
    assert drill.passed is True
    assert drill.failures == []
    assert drill_db_gone == 0  # the drill database is dropped after the drill


def test_empty_source_database_records_a_visible_skip(tmp_path: Path):
    receipt_engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)

    async def _body():
        # Same-loop disposal as the full-cycle test (see above).
        try:
            await _create_ao_tables(receipt_engine)
            await _make_source("ao_empty_test", with_tables=False)
            summary = await run_backup_cycle(
                source_url=_source_url("ao_empty_test"),
                backup_dir=tmp_path,
                admin_url=ADMIN_URL,
                receipt_engine=receipt_engine,
                stamp="20261128T000001Z",
            )
            maker = create_sessionmaker(receipt_engine)
            async with maker() as session:
                receipt = (
                    await session.execute(
                        select(BackupReceipt).where(BackupReceipt.id == summary["receipt_id"])
                    )
                ).scalars().one()
            return summary, receipt
        finally:
            await receipt_engine.dispose()

    try:
        summary, receipt = asyncio.run(_body())
    finally:
        asyncio.run(_drop_database_quietly("ao_empty_test"))

    assert summary["passed"] is False
    assert summary["status"] == "skipped"
    assert summary["reason"] == "empty_database"
    assert receipt.status == "skipped"
    assert receipt.drill_verified is False
    assert "empty_database" in (receipt.error or "")


def test_restore_drill_refuses_a_non_empty_target(tmp_path: Path):
    async def _body():
        await _make_source("ao_src_test", with_tables=True)
        await ensure_database(ADMIN_URL, "ao_drill_busy_test")
        busy_engine = create_engine(_source_url("ao_drill_busy_test"), pool_size=1, max_overflow=0)
        try:
            async with busy_engine.begin() as conn:
                await conn.execute(text("CREATE TABLE already_here (id int)"))
        finally:
            await busy_engine.dispose()
        backup_path = tmp_path / "src.dump"
        receipt = await asyncio.to_thread(create_backup, _source_url("ao_src_test"), backup_path)
        source_engine = create_engine(_source_url("ao_src_test"), pool_size=1, max_overflow=0)
        try:
            with pytest.raises(RuntimeError, match="Restore refused"):
                await run_restore_drill(
                    source_engine=source_engine,
                    drill_url=_source_url("ao_drill_busy_test"),
                    backup_path=backup_path,
                    receipt_sha256=str(receipt["sha256"]),
                )
        finally:
            await source_engine.dispose()

    asyncio.run(_body())
    asyncio.run(_drop_database_quietly("ao_src_test"))
    asyncio.run(_drop_database_quietly("ao_drill_busy_test"))
