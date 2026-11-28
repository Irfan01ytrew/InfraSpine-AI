"""Step W slice 2 — signal collection: probes, storage, classification (DB tests)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import DOC_PENDING, DOC_PROCESSING, Document, IngestionWorkerHeartbeat, User
from app.db.session import test_engine_options as _test_engine_options
from app.self_healing.collector import attempt_history, collect_and_store, read_signals
from app.self_healing.models import SelfHealingIncident, SelfHealingProbe
from tests.conftest import TEST_DATABASE_URL, USER_EMAIL

pytestmark = pytest.mark.usefixtures("clean_tables")


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _probes(signals):
    return {p.component: p for p in signals.probes}


async def _heartbeat(db, mode, age_seconds, now):
    db.add(
        IngestionWorkerHeartbeat(
            id=f"w-{mode}",
            hostname="test-host",
            pid=1,
            mode=mode,
            concurrency=1,
            active_jobs=0,
            started_at=now - timedelta(hours=1),
            heartbeat_at=now - timedelta(seconds=age_seconds),
        )
    )


async def _document(db, owner_id, *, status, locked_until=None, next_attempt_at=None, now=None):
    now = now or datetime.now(UTC)
    db.add(
        Document(
            owner_id=owner_id,
            visibility="private",
            filename="a.pdf",
            storage_key="k" * 32,
            sha256="s" * 64,
            size_bytes=10,
            status=status,
            locked_until=locked_until,
            next_attempt_at=next_attempt_at or now,
        )
    )


def test_healthy_system_stores_six_probes_and_no_incident():
    async def go(sm):
        now = datetime.now(UTC)
        async with sm() as db:
            await _heartbeat(db, "worker", 5, now)
            await _heartbeat(db, "embedded", 5, now)
            await db.commit()
        async with sm() as db:
            signals, incident, row = await collect_and_store(
                db, api_ok=True, vector_index_ok=True, now=now
            )
            assert incident is None and row is None
            by_component = _probes(signals)
            assert by_component["database"].status == "ok"
            assert by_component["api"].status == "ok"
            assert by_component["workflow_worker"].status == "ok"
            assert by_component["ingestion_worker"].status == "ok"
            assert by_component["vector_index"].status == "ok"
            assert by_component["backup"].status == "unknown"
            stored = list(await db.scalars(select(SelfHealingProbe)))
            assert len(stored) == 6
            assert all(r.recorded_at is not None for r in stored)

    run(go)


def test_api_down_is_a_critical_incident():
    async def go(sm):
        async with sm() as db:
            _, incident, row = await collect_and_store(db, api_ok=False)
            assert incident is not None and incident.kind == "api_down"
            assert incident.severity == "critical"
            assert row is not None and row.status == "open" and row.attempts == 0
            assert row.key == "api_down"

    run(go)


def test_stale_worker_heartbeat_classifies_worker_stalled():
    async def go(sm):
        now = datetime.now(UTC)
        async with sm() as db:
            await _heartbeat(db, "worker", 600, now)
            await db.commit()
        async with sm() as db:
            signals, incident, row = await collect_and_store(db, api_ok=True, now=now)
            assert incident is not None and incident.kind == "worker_stalled"
            assert _probes(signals)["workflow_worker"].status == "down"
            assert row is not None and row.component == "workflow_worker"

    run(go)


def test_fresh_heartbeat_is_not_an_incident():
    async def go(sm):
        now = datetime.now(UTC)
        async with sm() as db:
            await _heartbeat(db, "worker", 5, now)
            await db.commit()
        async with sm() as db:
            signals, incident, _ = await collect_and_store(db, api_ok=True, now=now)
            assert incident is None
            assert _probes(signals)["workflow_worker"].status == "ok"

    run(go)


def test_ingestion_backlog_when_worker_degraded_and_queue_due():
    async def go(sm):
        now = datetime.now(UTC)
        async with sm() as db:
            owner_id = await db.scalar(select(User.id).where(User.email == USER_EMAIL))
            await _heartbeat(db, "embedded", 120, now)
            await _document(db, owner_id, status=DOC_PENDING, next_attempt_at=now - timedelta(minutes=1), now=now)
            await db.commit()
        async with sm() as db:
            signals, incident, _ = await collect_and_store(db, api_ok=True, now=now)
            assert signals.queue_depth == 1
            assert incident is not None and incident.kind == "ingestion_backlog"

    run(go)


def test_stale_document_lock_age_is_reported_and_classified():
    async def go(sm):
        now = datetime.now(UTC)
        async with sm() as db:
            owner_id = await db.scalar(select(User.id).where(User.email == USER_EMAIL))
            await _document(
                db, owner_id, status=DOC_PROCESSING,
                locked_until=now - timedelta(seconds=900), now=now,
            )
            await db.commit()
        async with sm() as db:
            signals, incident, _ = await collect_and_store(db, api_ok=True, now=now)
            assert signals.lock_age_seconds is not None
            assert 850 < signals.lock_age_seconds < 950
            assert incident is not None and incident.kind == "stale_lock"

    run(go)


def test_error_rate_spike_from_supplied_counts():
    async def go(sm):
        async with sm() as db:
            _, incident, _ = await collect_and_store(
                db, api_ok=True, error_total=200, error_count=30
            )
            assert incident is not None and incident.kind == "error_rate_spike"
            assert incident.severity == "high"

    run(go)


def test_attempt_history_round_trip_and_recurring_fault_reopens():
    async def go(sm):
        now = datetime.now(UTC)
        async with sm() as db:
            await _heartbeat(db, "worker", 600, now)
            await db.commit()
        async with sm() as db:
            _, _, row = await collect_and_store(db, api_ok=True, now=now)
            row.attempts = 2
            row.status = "remedied"
            await db.commit()
            history = attempt_history(row)
            assert history.attempts == 2 and history.last_attempt_at is None
        async with sm() as db:
            _, incident, reopened = await collect_and_store(db, api_ok=True, now=now)
            assert incident is not None and incident.kind == "worker_stalled"
            assert reopened.status == "open"  # the fault recurred: reopened
            assert reopened.attempts == 2  # history is kept, not reset
            assert attempt_history(reopened).attempts == 2

    run(go)


def test_read_signals_reports_missing_heartbeats_as_unknown():
    async def go(sm):
        async with sm() as db:
            signals = await read_signals(db, api_ok=True)
            assert _probes(signals)["workflow_worker"].status == "unknown"
            assert _probes(signals)["ingestion_worker"].status == "unknown"
            assert signals.worker_heartbeat_age_seconds is None
            assert signals.lock_age_seconds is None

    run(go)


def test_incident_key_is_stable_across_rounds():
    async def go(sm):
        async with sm() as db:
            _, _, first = await collect_and_store(db, api_ok=False)
            _, _, second = await collect_and_store(db, api_ok=False)
            assert first.id == second.id  # same row, upserted by key
            assert await db.scalar(select(func.count()).select_from(SelfHealingIncident)) == 1

    run(go)
