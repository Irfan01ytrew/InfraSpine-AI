"""DB tests for Step AO slice 3: pool stats, slow-query honesty, index audit.

Run with --noconftest: these tests build their own engines against the
throwaway cluster. Nothing here touches the live Codespace database or
the owner's PC database.

Each test runs its whole async body inside ONE asyncio.run call: an async
engine created under one event loop must never be used or disposed under
another.
"""

import asyncio
import os

import pytest
from sqlalchemy import text

from app.db.session import create_engine
from app.ops.db_performance import (
    collect_pool_stats,
    collect_slow_queries,
    index_audit,
    pool_tuning_recommendation,
)

TEST_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set")


def test_pool_stats_measure_a_real_burst():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=5, max_overflow=10)
        try:
            idle = collect_pool_stats(engine)
            assert idle["checked_out"] == 0
            assert idle["size"] == 5
            assert idle["max_overflow"] == 10
            assert idle["band"] == "ok"

            async def _sleep(conn):
                await conn.execute(text("SELECT pg_sleep(2)"))

            # Three concurrent queries hold three connections at once.
            async with engine.connect() as c1, engine.connect() as c2, engine.connect() as c3:
                burst = asyncio.gather(_sleep(c1), _sleep(c2), _sleep(c3))
                await asyncio.sleep(0.5)
                mid = collect_pool_stats(engine)
                await burst
            assert mid["checked_out"] >= 3
            assert mid["band"] == "ok"
            # The measured peak justifies keeping the current pool numbers.
            tuning = pool_tuning_recommendation(
                peak_checked_out=mid["checked_out"],
                pool_size=5,
                max_overflow=10,
                overflow_observed=mid["overflow"] > 0,
            )
            assert tuning["recommendation"] in ("keep", "decrease")
            return idle, mid
        finally:
            await engine.dispose()

    asyncio.run(_body())


def test_slow_query_report_is_honest_about_pg_stat_statements():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            report = await collect_slow_queries(engine)
            async with engine.connect() as conn:
                extension_present = bool(
                    await conn.scalar(
                        text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'")
                    )
                )
            return report, extension_present
        finally:
            await engine.dispose()

    report, extension_present = asyncio.run(_body())
    assert report["available"] is extension_present
    assert isinstance(report["queries"], list)
    for query in report["queries"]:
        assert query["band"] in ("ok", "warning", "critical", "unknown")


def test_index_audit_finds_an_unused_index_and_a_scan_candidate():
    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS ao_perf_unused"))
                await conn.execute(text("DROP TABLE IF EXISTS ao_perf_scanned"))
                await conn.execute(
                    text("CREATE TABLE ao_perf_unused (id serial PRIMARY KEY, v text)")
                )
                await conn.execute(
                    text("CREATE INDEX ao_perf_unused_v_idx ON ao_perf_unused (v)")
                )
                await conn.execute(
                    text("CREATE TABLE ao_perf_scanned (id serial PRIMARY KEY, v text)")
                )
                await conn.execute(
                    text(
                        "INSERT INTO ao_perf_scanned (v) "
                        "SELECT 'row-' || g FROM generate_series(1, 2000) g"
                    )
                )
                # Sequential scans only: no index exists on this table at all.
                # Six scans of 2,000 rows = 12,000 rows read, past the
                # audit's 10,000-row candidate threshold.
                for like in ("row-1%", "row-2%", "row-3%", "row-4%", "row-5%", "row-6%"):
                    await conn.execute(
                        text("SELECT count(*) FROM ao_perf_scanned WHERE v LIKE :like"),
                        {"like": like},
                    )
            # pg_stat_* counters flush asynchronously (default 500 ms);
            # poll until the scans land instead of racing the collector.
            for _ in range(20):
                async with engine.connect() as conn:
                    seen = await conn.scalar(
                        text(
                            "SELECT seq_tup_read FROM pg_stat_user_tables "
                            "WHERE relname = 'ao_perf_scanned'"
                        )
                    )
                if seen is not None and int(seen) > 10000:
                    break
                await asyncio.sleep(0.5)
            audit = await index_audit(engine)
            async with engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS ao_perf_unused"))
                await conn.execute(text("DROP TABLE IF EXISTS ao_perf_scanned"))
            return audit
        finally:
            await engine.dispose()

    audit = asyncio.run(_body())
    unused_names = {item["index"] for item in audit["unused_indexes"]}
    assert "ao_perf_unused_v_idx" in unused_names
    candidates = {item["table"] for item in audit["sequential_scan_candidates"]}
    assert "ao_perf_scanned" in candidates
    candidate = next(
        item for item in audit["sequential_scan_candidates"] if item["table"] == "ao_perf_scanned"
    )
    assert "EXPLAIN" in candidate["note"]
