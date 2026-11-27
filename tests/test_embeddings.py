import sys
from types import SimpleNamespace

import numpy as np
import pytest

from app.rag.embeddings import EmbeddingModelUnavailableError, SentenceTransformerEmbedder
from app.rag.components import build_embedder as build_rag_embedder


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _cache_snapshot(cache_dir):
    snapshot = cache_dir / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    for name in ("modules.json", "config.json", "model.safetensors", "tokenizer.json"):
        (snapshot / name).write_text("local test fixture", encoding="utf-8")
    ref = snapshot.parent.parent / "refs" / "main"
    ref.parent.mkdir()
    ref.write_text("revision", encoding="utf-8")
    return snapshot


def test_local_health_check_never_constructs_model_when_cache_is_missing(tmp_path, monkeypatch):
    calls = []

    def sentence_transformer(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("missing model must not trigger a load or download")

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=sentence_transformer),
    )
    embedder = SentenceTransformerEmbedder(MODEL_NAME, 384, tmp_path)

    assert embedder.available_locally is False
    with pytest.raises(EmbeddingModelUnavailableError, match="not available locally"):
        embedder.health_check()
    assert calls == []
    assert embedder.is_loaded is False


def test_local_health_check_loads_cached_model_and_validates_output(tmp_path, monkeypatch):
    _cache_snapshot(tmp_path)
    calls = []

    class FakeModel:
        def get_embedding_dimension(self):
            return 384

        def encode(self, texts, **kwargs):
            assert texts == ["InfraSpine embedding health check"]
            assert kwargs["normalize_embeddings"] is True
            vectors = np.zeros((1, 384), dtype=np.float32)
            vectors[0, 0] = 1.0
            return vectors

    def sentence_transformer(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeModel()

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=sentence_transformer),
    )
    embedder = SentenceTransformerEmbedder(MODEL_NAME, 384, tmp_path)

    embedder.health_check()

    assert embedder.available_locally is True
    assert embedder.is_loaded is True
    assert calls[0][1]["local_files_only"] is True
    assert calls[0][1]["device"] == "cpu"


def test_embedding_fails_without_downloading_when_disabled(tmp_path, monkeypatch):
    calls = []

    def sentence_transformer(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("disabled model downloads must not call the model hub")

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=sentence_transformer),
    )
    embedder = SentenceTransformerEmbedder(MODEL_NAME, 384, tmp_path, allow_download=False)

    with pytest.raises(EmbeddingModelUnavailableError, match="not available locally"):
        embedder.embed(["synthetic test text"])
    assert calls == []
    assert embedder.is_loaded is False


def test_missing_embedding_model_uses_actionable_safe_document_failure():
    from app.rag.ingestion import _processing_failure

    code, message, permanent = _processing_failure(
        EmbeddingModelUnavailableError("private model path and provider details must not reach the user")
    )

    assert code == "embedding_model_unavailable"
    assert "administrator" in message and "reprocess" in message
    assert "private model path" not in message
    assert permanent is True


def test_unclassified_processing_failure_remains_generic_and_retryable():
    from app.rag.ingestion import _processing_failure

    code, message, permanent = _processing_failure(RuntimeError("internal credential-bearing details"))

    assert (code, message, permanent) == ("processing_error", "Processing failed unexpectedly.", False)
    assert "credential-bearing" not in message


def test_embedding_download_requires_explicit_opt_in(tmp_path, monkeypatch):
    calls = []

    class FakeModel:
        def get_embedding_dimension(self):
            return 384

        def encode(self, texts, **kwargs):
            assert texts == ["synthetic test text"]
            result = np.zeros((1, 384), dtype=np.float32)
            result[0, 0] = 1.0
            return result

    def sentence_transformer(*args, **kwargs):
        calls.append((args, kwargs))
        if kwargs.get("local_files_only"):
            raise OSError("synthetic missing local files")
        return FakeModel()

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=sentence_transformer),
    )
    embedder = SentenceTransformerEmbedder(MODEL_NAME, 384, tmp_path, allow_download=True)

    result = embedder.embed(["synthetic test text"])

    assert result.shape == (1, 384)
    assert [call[1].get("local_files_only") for call in calls] == [True, None]


def test_rag_component_passes_the_download_policy(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        rag_models_dir=None,
        rag_data_dir=tmp_path,
        rag_embedding_model=MODEL_NAME,
        rag_embedding_dimension=384,
        rag_embedding_allow_download=False,
    )

    # conftest replaces components.build_embedder with FakeEmbedder for ordinary
    # integration tests. Keep this factory reference from module import so this
    # unit test exercises the real settings-to-embedder wiring.
    embedder = build_rag_embedder(settings)

    assert embedder._allow_download is False
    assert embedder._cache_dir == tmp_path.resolve() / "models"
