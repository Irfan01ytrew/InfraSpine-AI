"""The API loads the embedding model at startup, off the request path, so the first question is not slow."""

import asyncio
import time

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import _warm_embedder, app
from tests.conftest import CSRF, FakeEmbedder


def _wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_api_startup_warms_the_embedding_model(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(FakeEmbedder, "warmup", lambda self: calls.append(1))
    with TestClient(app, headers=CSRF) as client:
        assert _wait_for(lambda: calls == [1])
        assert client.get("/health").status_code == 200


def test_startup_warmup_can_be_turned_off(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(FakeEmbedder, "warmup", lambda self: calls.append(1))
    monkeypatch.setenv("RAG_EMBEDDING_WARMUP", "false")
    get_settings.cache_clear()
    try:
        with TestClient(app, headers=CSRF) as client:
            assert client.get("/health").status_code == 200
            time.sleep(0.3)
        assert calls == []
    finally:
        get_settings.cache_clear()


def test_a_failed_warmup_is_logged_not_raised(caplog):
    class Broken:
        def warmup(self) -> None:
            raise RuntimeError("model files missing")

    asyncio.run(_warm_embedder(Broken()))
    assert "Embedding model warm-up failed: RuntimeError" in caplog.text
    assert "model files missing" not in caplog.text
