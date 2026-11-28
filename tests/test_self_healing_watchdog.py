"""Step W slice 6 — watchdog integration: pure decisions, recorded checks,
out-of-band database-down path, and the CLI the Windows script calls.
"""

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import IngestionWorkerHeartbeat
from app.db.session import test_engine_options as _test_engine_options
from app.self_healing.models import SelfHealingEscalation, SelfHealingIncident
from app.self_healing.watchdog import (
    HEARTBEAT_STALE_SECONDS,
    ProcessFacts,
    decide_watchdog,
    run_watchdog_check,
)
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("clean_tables")


def run(fn):
    async def go():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _facts(**overrides):
    base = dict(api_listening=True, api_healthy=True, worker_running=False,
                worker_heartbeat_age_seconds=10.0)
    base.update(overrides)
    return ProcessFacts(**base)


# --- pure decisions -------------------------------------------------------

def test_api_not_listening_restarts_api():
    d = decide_watchdog(_facts(api_listening=False, api_healthy=False))
    assert d.action == "restart_api"
    assert d.incident.kind == "api_down"
    assert d.incident.severity == "critical"


def test_api_wedged_escalates_only_never_kills():
    d = decide_watchdog(_facts(api_healthy=False))
    assert d.action == "escalate_only"
    assert d.incident.kind == "api_down"


def test_stale_heartbeat_no_local_worker_restarts_worker():
    d = decide_watchdog(_facts(worker_heartbeat_age_seconds=400.0))
    assert d.action == "restart_worker"
    assert d.incident.kind == "worker_stalled"
    assert d.incident.severity == "high"


def test_stale_heartbeat_with_local_worker_escalates_only():
    d = decide_watchdog(_facts(worker_heartbeat_age_seconds=400.0, worker_running=True))
    assert d.action == "escalate_only"
    assert d.incident.kind == "worker_stalled"


def test_fresh_heartbeat_no_local_worker_is_ok():
    # Workers may run on another machine sharing the database; a missing
    # local process with a fresh heartbeat is not a fault.
    d = decide_watchdog(_facts(worker_heartbeat_age_seconds=10.0, worker_running=False))
    assert d.action == "ok"
    assert d.incident is None


def test_no_heartbeat_record_is_ok():
    d = decide_watchdog(_facts(worker_heartbeat_age_seconds=None))
    assert d.action == "ok"
    assert d.incident is None


def test_stale_boundary_is_strict():
    assert decide_watchdog(_facts(worker_heartbeat_age_seconds=HEARTBEAT_STALE_SECONDS)).action == "ok"
    assert decide_watchdog(
        _facts(worker_heartbeat_age_seconds=HEARTBEAT_STALE_SECONDS + 1)
    ).action == "restart_worker"


def test_api_down_beats_worker_stall():
    d = decide_watchdog(_facts(api_listening=False, worker_heartbeat_age_seconds=400.0))
    assert d.action == "restart_api"
    assert d.incident.kind == "api_down"


# --- recorded checks ------------------------------------------------------

async def _heartbeat(db, age_seconds, now):
    db.add(
        IngestionWorkerHeartbeat(
            id="w-worker", hostname="test-host", pid=1, mode="worker",
            concurrency=1, active_jobs=0, started_at=now - timedelta(hours=1),
            heartbeat_at=now - timedelta(seconds=age_seconds),
        )
    )


def _probe_down(_url):
    return False, False


def _probe_wedged(_url):
    return True, False


def _probe_ok(_url):
    return True, True


def test_api_down_check_records_incident_and_escalation():
    async def go(sm):
        async with sm() as db:
            decision = await run_watchdog_check(db, api_probe=_probe_down)
            assert decision.action == "restart_api"
            incident = await db.scalar(
                select(SelfHealingIncident).where(SelfHealingIncident.key == "api_down")
            )
            assert incident is not None and incident.status == "open"
            escalation = await db.scalar(
                select(SelfHealingEscalation).where(SelfHealingEscalation.incident_key == "api_down")
            )
            assert escalation is not None and escalation.channel == "admin_ui"
        async with sm() as db:
            # A second check reopens the same rows instead of duplicating them.
            await run_watchdog_check(db, api_probe=_probe_down)
            count = await db.scalar(
                select(func.count()).select_from(SelfHealingIncident).where(
                    SelfHealingIncident.key == "api_down")
            )
            assert count == 1

    run(go)


def test_wedged_api_check_escalates_only():
    async def go(sm):
        async with sm() as db:
            decision = await run_watchdog_check(db, api_probe=_probe_wedged)
            assert decision.action == "escalate_only"
            assert decision.incident.kind == "api_down"

    run(go)


def test_stale_heartbeat_check_restarts_worker():
    async def go(sm):
        now = datetime.now(UTC)
        async with sm() as db:
            await _heartbeat(db, 400, now)
            await db.commit()
        async with sm() as db:
            decision = await run_watchdog_check(db, api_probe=_probe_ok, worker_running=False)
            assert decision.action == "restart_worker"
            assert decision.incident.kind == "worker_stalled"

    run(go)


def test_stale_heartbeat_with_running_worker_escalates_only():
    async def go(sm):
        now = datetime.now(UTC)
        async with sm() as db:
            await _heartbeat(db, 400, now)
            await db.commit()
        async with sm() as db:
            decision = await run_watchdog_check(db, api_probe=_probe_ok, worker_running=True)
            assert decision.action == "escalate_only"

    run(go)


def test_healthy_check_records_nothing():
    async def go(sm):
        now = datetime.now(UTC)
        async with sm() as db:
            await _heartbeat(db, 10, now)
            await db.commit()
        async with sm() as db:
            decision = await run_watchdog_check(db, api_probe=_probe_ok, worker_running=False)
            assert decision.action == "ok"
            count = await db.scalar(select(func.count()).select_from(SelfHealingIncident))
            assert count == 0

    run(go)


def test_database_down_writes_out_of_band_file(tmp_path):
    path = tmp_path / "escalations.jsonl"
    decision = asyncio.run(
        run_watchdog_check(None, api_probe=_probe_down, out_of_band_path=str(path))
    )
    assert decision.action == "escalate_only"
    assert decision.db_down is True
    assert decision.incident.kind == "database_unreachable"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "database_unreachable"


def test_database_down_without_file_still_returns_a_decision():
    decision = asyncio.run(run_watchdog_check(None, api_probe=_probe_down))
    assert decision.action == "escalate_only"
    assert decision.db_down is True


# --- CLI ------------------------------------------------------------------

def test_cli_help_exits_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "app.self_healing.watchdog", "--help"],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0
    assert "watchdog" in proc.stdout


def test_cli_api_refused_reports_restart_api():
    # Port 1 never listens: the CLI must report restart_api as JSON and
    # exit 0 (the decision is data, not an error). It records the incident
    # in the throwaway test database; the clean_tables fixture clears it.
    proc = subprocess.run(
        [sys.executable, "-m", "app.self_healing.watchdog",
         "--api-url", "http://127.0.0.1:1/health"],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    decision = json.loads(proc.stdout.strip().splitlines()[-1])
    assert decision["action"] == "restart_api"
    assert decision["kind"] == "api_down"
    assert decision["db_down"] is False
