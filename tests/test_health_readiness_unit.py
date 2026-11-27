"""Database-free readiness contract tests for API and document-worker availability."""

import asyncio
import json
from types import SimpleNamespace

from app.api.routes import health


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


def _request():
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(engine=_Engine(), sessionmaker=_SessionMaker())
        )
    )


def test_readiness_reports_database_up_but_worker_missing(monkeypatch):
    monkeypatch.setattr(
        health,
        "get_settings",
        lambda: SimpleNamespace(rag_ingestion_mode="worker", rag_worker_heartbeat_seconds=10),
    )
    monkeypatch.setattr("app.rag.ingestion.workers_online", _async_zero)
    response = asyncio.run(health.readiness(_request()))
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body == {
        "status": "ready",
        "database": "ok",
        "index_role": None,  # no RAG components in this stub app
        "ingestion": {"mode": "worker", "status": "waiting_for_worker", "workers_online": 0},
    }


async def _async_zero(_db, _settings):
    return 0


def test_readiness_reports_worker_online(monkeypatch):
    monkeypatch.setattr(
        health,
        "get_settings",
        lambda: SimpleNamespace(rag_ingestion_mode="worker", rag_worker_heartbeat_seconds=10),
    )
    monkeypatch.setattr("app.rag.ingestion.workers_online", _async_one)
    response = asyncio.run(health.readiness(_request()))
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["ingestion"] == {"mode": "worker", "status": "available", "workers_online": 1}


async def _async_one(_db, _settings):
    return 1
