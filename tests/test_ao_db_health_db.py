"""DB tests for Step AO: the read-only health collector and a real-backup sanity check.

Throwaway cluster only (TEST_DATABASE_URL, a *_test database). Nothing
here touches the live Codespace database or the owner's PC database.
Run with --noconftest: the collector needs a live PostgreSQL, not the
application schema, so these tests build their own engine.

Each test runs its whole async body inside ONE asyncio.run call: an
async engine created under one event loop must never be used or
disposed under another.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.session import create_engine
from app.ops.backup_verify import BackupRecord, validate_backup_record
from app.ops.db_health import classify_dead_tuple_ratio, collect_db_health, summarize_health
from app.ops.postgres_backup import create_backup, verify_backup

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set")


def test_collector_on_empty_database():
    """The collector reports the shared test database honestly.

    The database is shared with the other AO tests (receipts are written
    and updated here, which leaves dead tuples), so the bloat band and
    the overall band depend on live state. The test asserts the band
    matches the classifier applied to the measured counts — the
    collector must report what it measures, not a hardcoded "ok".
    """

    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            report = await collect_db_health(engine)
            async with engine.connect() as conn:
                dead, live = (
                    await conn.execute(
                        text(
                            "SELECT coalesce(sum(n_dead_tup), 0), coalesce(sum(n_live_tup), 0) "
                            "FROM pg_stat_user_tables"
                        )
                    )
                ).one()
            return report, int(dead), int(live)
        finally:
            await engine.dispose()

    report, dead_tuples, live_tuples = asyncio.run(_body())
    assert report["reachable"] is True
    assert report["database_bytes"] > 0
    assert report["connections"]["band"] == "ok"
    assert report["connections"]["max"] == 50
    assert report["longest_query_band"] == "ok"
    assert report["bloat_band"] == classify_dead_tuple_ratio(dead_tuples, live_tuples)
    assert report["dead_tuples"] == dead_tuples
    assert report["live_tuples"] == live_tuples
    assert report["locks"]["band"] == "ok"
    assert report["locks"]["blocked"] == 0
    assert report["replication"]["band"] == "unknown"  # no replica: unknown, never ok
    assert report["replication"]["lag_seconds"] is None
    assert isinstance(report["pg_stat_statements_available"], bool)
    assert report["overall"] == summarize_health(
        {
            "connections": report["connections"]["band"],
            "longest_query": report["longest_query_band"],
            "bloat": report["bloat_band"],
            "locks": report["locks"]["band"],
            "replication": report["replication"]["band"],
        }
    )


def test_collector_is_read_only():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS ao_probe"))
                await conn.execute(text("CREATE TABLE ao_probe (id serial PRIMARY KEY, v text)"))
                await conn.execute(text("INSERT INTO ao_probe (v) VALUES ('a'), ('b'), ('c')"))
                before = await conn.scalar(text("SELECT count(*) FROM ao_probe"))
            first = await collect_db_health(engine)
            second = await collect_db_health(engine)
            async with engine.connect() as conn:
                after = await conn.scalar(text("SELECT count(*) FROM ao_probe"))
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS ao_probe"))
            return before, after, first, second
        finally:
            await engine.dispose()

    before, after, first, second = asyncio.run(_body())
    assert before == 3
    assert after == 3  # the collector wrote nothing
    assert first["reachable"] is True
    assert second["reachable"] is True
    assert first["overall"] == second["overall"]


def test_collector_unreachable_reports_critical():
    bad_url = TEST_URL.rsplit(":", 1)[0] + ":55541/" + TEST_URL.rsplit("/", 1)[1]

    async def _body():
        engine = create_engine(bad_url, pool_size=1, max_overflow=0)
        try:
            return await collect_db_health(engine)
        finally:
            await engine.dispose()

    report = asyncio.run(_body())
    assert report["reachable"] is False
    assert report["overall"] == "critical"
    assert isinstance(report["error"], str) and report["error"]


def test_real_backup_verifies_and_receipt_is_valid(tmp_path: Path):
    async def _seed():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS ao_backup_probe"))
                await conn.execute(text("CREATE TABLE ao_backup_probe (id serial PRIMARY KEY, v text)"))
                await conn.execute(text("INSERT INTO ao_backup_probe (v) VALUES ('x')"))
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    # NOTE: create_backup refuses a backup with zero catalog entries, so
    # the database must hold at least one table. Whether an empty
    # database deserves a backup at all is a slice-2 decision, not slice 1's.
    destination = tmp_path / "ao173.dump"
    receipt = create_backup(TEST_URL, destination)
    assert destination.exists()
    assert receipt["bytes"] == destination.stat().st_size
    assert receipt["verified"] is True
    assert receipt["catalog_entries"] >= 1
    assert verify_backup(destination) == receipt["catalog_entries"]
    record = BackupRecord(
        database=receipt["database"],
        path=receipt["path"],
        bytes=receipt["bytes"],
        sha256=receipt["sha256"],
        catalog_entries=receipt["catalog_entries"],
        verified=receipt["verified"],
        created_at=datetime.now(timezone.utc),
    )
    assert validate_backup_record(record) == []
