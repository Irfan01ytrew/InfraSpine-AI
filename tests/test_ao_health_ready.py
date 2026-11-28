"""Tests for Step AO slice 5: /health/ready carries measured database health.

The unit tests use the same fake engine as test_health_readiness_unit.py
(no scalar() — the collector cannot run against it). The DB test runs
against a real throwaway cluster. Nothing here touches the live
Codespace database or the owner's PC database.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.api.routes import health
from app.db.session import create_engine

TEST_URL = os.environ.get("TEST_DATABASE_URL")


class _Connection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return 1


class _Engine:
    def connect(self):
        return _Connection()


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _SessionMaker:
    def __call__(self):
        return _Session()


def _request(engine):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(engine=engine, sessionmaker=_SessionMaker()))
    )


def _settings(monkeypatch):
    monkeypatch.setattr(
        health,
        "get_settings",
        lambda: SimpleNamespace(rag_ingestion_mode="worker", rag_worker_heartbeat_seconds=10),
    )
    monkeypatch.setattr("app.rag.ingestion.workers_online", _async_zero)


async def _async_zero(_db, _settings):
    return 0


def _critical_detail():
    return {
        "reachable": True,
        "overall": "critical",
        "connections": {"total": 49, "active": 40, "idle_in_transaction": 0, "max": 50, "band": "critical"},
        "longest_query_seconds": 12.0,
        "longest_query_band": "ok",
        "locks": {"total": 0, "blocked": 0, "oldest_lock_wait_seconds": 0.0, "band": "ok"},
        "dead_tuples": 0,
        "live_tuples": 100,
        "dead_tuple_ratio": 0.0,
        "bloat_band": "ok",
        "replication": {"replicas": 0, "lag_seconds": None, "band": "unknown"},
        "pg_stat_statements_available": False,
    }


def _warning_detail():
    detail = _critical_detail()
    detail["overall"] = "warning"
    detail["connections"] = {"total": 40, "active": 30, "idle_in_transaction": 0, "max": 50, "band": "warning"}
    return detail


def test_critical_database_health_returns_degraded_503(monkeypatch):
    _settings(monkeypatch)

    async def _fake_collect(_engine):
        return _critical_detail()

    monkeypatch.setattr(health, "collect_db_health", _fake_collect)
    response = asyncio.run(health.readiness(_request(_Engine())))
    body = json.loads(response.body)
    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["database"] == "ok"  # the string is kept; the detail carries the verdict
    assert body["database_detail"]["overall"] == "critical"


def test_warning_database_health_stays_ready_but_is_reported(monkeypatch):
    _settings(monkeypatch)

    async def _fake_collect(_engine):
        return _warning_detail()

    monkeypatch.setattr(health, "collect_db_health", _fake_collect)
    response = asyncio.run(health.readiness(_request(_Engine())))
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["database_detail"]["overall"] == "warning"


def test_collector_failure_keeps_the_old_shape(monkeypatch):
    """When the collector cannot run, the response is byte-identical to the pre-AO shape."""
    _settings(monkeypatch)

    async def _failing_collect(_engine):
        raise AttributeError("fake connection has no scalar()")

    monkeypatch.setattr(health, "collect_db_health", _failing_collect)
    response = asyncio.run(health.readiness(_request(_Engine())))
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body == {
        "status": "ready",
        "database": "ok",
        "index_role": None,
        "ingestion": {"mode": "worker", "status": "waiting_for_worker", "workers_online": 0},
    }


@pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL is not set")
def test_real_engine_readiness_carries_measured_detail(monkeypatch):
    _settings(monkeypatch)

    async def _body():
        engine = create_engine(TEST_URL, pool_size=1, max_overflow=0)
        try:
            response = await health.readiness(_request(engine))
            return response
        finally:
            await engine.dispose()

    response = asyncio.run(_body())
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["database"] == "ok"
    detail = body["database_detail"]
    assert detail["reachable"] is True
    assert detail["overall"] in ("ok", "warning")  # a quiet throwaway cluster is ok
    assert detail["connections"]["max"] >= 1
    assert detail["replication"]["band"] == "unknown"  # no replica on a single node
    # The payload carries numbers, never a connection string or secret.
    assert "postgresql://" not in json.dumps(detail)
