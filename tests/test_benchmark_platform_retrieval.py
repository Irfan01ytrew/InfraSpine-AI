"""Tiny-scale smoke test for the step C retrieval benchmark harness (no database, no network)."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("faiss")

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_platform_retrieval.py"
_spec = importlib.util.spec_from_file_location("benchmark_platform_retrieval", _PATH)
bench = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench  # dataclasses resolve their module by name
_spec.loader.exec_module(bench)


def test_harness_reports_latency_and_never_leaks_across_scopes():
    report = bench.run(chunks=2000, dimension=32, scopes=10, users=4, visible_fraction=0.3,
                       queries=16, k=5, concurrency=2, chunk_words=16, batch=500)
    assert report["ingestion"]["chunks"] == 2000
    assert report["search"]["latency"]["p95_ms"] >= report["search"]["latency"]["p50_ms"] >= 0
    assert report["acl"]["leaked_results"] == 0
    assert report["acl"]["empty_results"] == 0
    assert report["hardware"]["cpu_count"]


def test_corpus_and_visibility_are_deterministic():
    a = bench.make_corpus(100, 16, 5, 8, seed=7)
    b = bench.make_corpus(100, 16, 5, 8, seed=7)
    assert (a.scope_of_chunk == b.scope_of_chunk).all() and (a.vectors == b.vectors).all()
    sets = bench.visible_scope_sets(10, 3, 0.2, seed=7)
    assert all(len(s) == 2 for s in sets)


def test_qdrant_backend_requires_an_isolated_test_url(monkeypatch):
    pytest.importorskip("qdrant_client")
    monkeypatch.delenv("QDRANT_TEST_URL", raising=False)
    with pytest.raises(RuntimeError, match="QDRANT_TEST_URL"):
        bench.QdrantBackend(16)
