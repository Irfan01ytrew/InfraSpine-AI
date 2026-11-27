"""The worker health probe stays light and detects missing offline embeddings."""

import asyncio
import builtins
from argparse import Namespace
from types import SimpleNamespace

from sqlalchemy.exc import DBAPIError

from app import worker
from app.core import config
from app.db import session


def test_worker_schema_probe_reports_only_safe_database_failure_classes():
    class DriverFailure(Exception):
        sqlstate = "08006"

        def __str__(self):
            return "private connection detail must not be surfaced"

    failure = DBAPIError("SELECT 1", {"secret": "private"}, DriverFailure(), False)

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, _statement):
            raise failure

    result = asyncio.run(worker._schema_ready(lambda: FakeDB()))

    assert result == "The database is not reachable (DBAPIError/DriverFailure/sqlstate:08006). Check DATABASE_URL."
    assert "private" not in result and "secret" not in result


def test_worker_check_uses_lightweight_config_and_database_probe(monkeypatch, tmp_path, capsys):
    class FakeEngine:
        async def dispose(self):
            return None

    settings = SimpleNamespace(
        database_url=SimpleNamespace(get_secret_value=lambda: "synthetic-test-dsn"),
        database_pool_size=2,
        database_pool_max_overflow=3,
        database_pool_timeout_seconds=7,
        database_pool_recycle_seconds=600,
        rag_worker_concurrency=1,
        rag_data_dir=tmp_path,
        rag_parse_isolation=False,
        rag_processing_timeout_seconds=60,
        rag_parser_memory_mb=512,
        rag_ingestion_mode="worker",
        rag_worker_enabled=False,
        rag_models_dir=str(tmp_path / "models"),
        rag_embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        rag_embedding_dimension=384,
        rag_embedding_allow_download=True,
    )
    engine = FakeEngine()
    engine_options = {}
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(session, "create_engine", lambda _url, **kwargs: engine_options.update(kwargs) or engine)
    monkeypatch.setattr(session, "create_sessionmaker", lambda _engine: object())

    async def schema_ready(_sessionmaker):
        return None

    monkeypatch.setattr(worker, "_schema_ready", schema_ready)
    original_import = builtins.__import__

    def reject_parser_stack(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app.rag" and "components" in fromlist:
            raise AssertionError("worker health probe imported the parser/vector component stack")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_parser_stack)
    result = asyncio.run(worker._run(Namespace(concurrency=None, check=True, once=False)))

    assert result == 0
    # The worker honours the same pool settings as the API, including timeout and recycle.
    assert engine_options == {"pool_size": 2, "max_overflow": 3, "pool_timeout": 7, "pool_recycle": 600}
    output = capsys.readouterr().out
    assert "Worker configuration OK" in output
    assert "download permitted (connectivity not checked)" in output


def test_worker_check_fails_fast_when_offline_embedding_model_is_missing(monkeypatch, tmp_path, caplog):
    class FakeEngine:
        async def dispose(self):
            return None

    settings = SimpleNamespace(
        database_url=SimpleNamespace(get_secret_value=lambda: "synthetic-test-dsn"),
        database_pool_size=2,
        database_pool_max_overflow=3,
        rag_worker_concurrency=1,
        rag_data_dir=tmp_path,
        rag_parse_isolation=False,
        rag_processing_timeout_seconds=60,
        rag_parser_memory_mb=512,
        rag_ingestion_mode="worker",
        rag_worker_enabled=False,
        rag_models_dir=str(tmp_path / "models"),
        rag_embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        rag_embedding_dimension=384,
        rag_embedding_allow_download=False,
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(session, "create_engine", lambda _url, **_kwargs: FakeEngine())
    monkeypatch.setattr(session, "create_sessionmaker", lambda _engine: object())
    monkeypatch.setattr(worker, "_schema_ready", lambda _sessionmaker: _ready())

    result = asyncio.run(worker._run(Namespace(concurrency=None, check=True, once=False)))

    assert result == 1
    assert "Embedding model files are not available locally" in caplog.text


def test_worker_check_accepts_a_staged_local_model_without_loading_it(monkeypatch, tmp_path, capsys):
    class FakeEngine:
        async def dispose(self):
            return None

    model_dir = tmp_path / "models" / "test-model"
    model_dir.mkdir(parents=True)
    for name in ("modules.json", "config.json", "model.safetensors", "tokenizer_config.json"):
        (model_dir / name).write_text("synthetic", encoding="utf-8")
    settings = SimpleNamespace(
        database_url=SimpleNamespace(get_secret_value=lambda: "synthetic-test-dsn"),
        database_pool_size=2,
        database_pool_max_overflow=3,
        rag_worker_concurrency=1,
        rag_data_dir=tmp_path,
        rag_parse_isolation=False,
        rag_processing_timeout_seconds=60,
        rag_parser_memory_mb=512,
        rag_ingestion_mode="worker",
        rag_worker_enabled=False,
        rag_models_dir=str(tmp_path / "models"),
        rag_embedding_model=str(model_dir),
        rag_embedding_dimension=384,
        rag_embedding_allow_download=False,
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(session, "create_engine", lambda _url, **_kwargs: FakeEngine())
    monkeypatch.setattr(session, "create_sessionmaker", lambda _engine: object())
    monkeypatch.setattr(worker, "_schema_ready", lambda _sessionmaker: _ready())
    original_import = builtins.__import__

    def reject_parser_stack(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app.rag" and "components" in fromlist:
            raise AssertionError("worker health probe imported the parser/vector component stack")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_parser_stack)
    result = asyncio.run(worker._run(Namespace(concurrency=None, check=True, once=False)))

    assert result == 0
    assert "embedding model staged locally" in capsys.readouterr().out


async def _ready():
    return None
