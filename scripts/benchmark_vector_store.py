"""Measure persistent FAISS build and ACL-filtered search on synthetic vectors.

Run from the repository root, for example:
    python scripts/benchmark_vector_store.py --vectors 100000 --queries 50

This measures the configured exact index only. It is a reproducible capacity signal, not a
600 GB document corpus benchmark. No application database or configured RAG data is touched.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.vector_store import FaissVectorStore


def _percentiles(samples: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(float(np.percentile(samples, 50)), 3),
        "p95_ms": round(float(np.percentile(samples, 95)), 3),
        "p99_ms": round(float(np.percentile(samples, 99)), 3),
        "mean_ms": round(statistics.fmean(samples), 3),
    }


def benchmark(vector_count: int, dimension: int, query_count: int, seed: int) -> dict:
    if vector_count < 1 or dimension < 8 or query_count < 1:
        raise ValueError("vectors and queries must be positive; dimension must be at least 8")
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((vector_count, dimension), dtype=np.float32)
    vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    queries = rng.standard_normal((query_count, dimension), dtype=np.float32)
    queries /= np.maximum(np.linalg.norm(queries, axis=1, keepdims=True), 1e-12)
    ids = np.arange(1, vector_count + 1, dtype=np.int64)
    restricted_ids = ids[::10]  # representative 10% department/visibility scope

    process = psutil.Process()
    rss_before = process.memory_info().rss
    with tempfile.TemporaryDirectory(prefix="infraspine-vector-bench-") as tmp:
        store = FaissVectorStore(Path(tmp), dimension, "synthetic-benchmark")
        start = time.perf_counter()
        store.rebuild(ids, vectors)
        build_seconds = time.perf_counter() - start
        build_rss = process.memory_info().rss

        timings: dict[str, list[float]] = {"all_vectors": [], "10_percent_scope": []}
        allowed_sets = {"all_vectors": set(ids.tolist()), "10_percent_scope": set(restricted_ids.tolist())}
        for query in queries:
            for label, allowed in (("all_vectors", ids), ("10_percent_scope", restricted_ids)):
                start = time.perf_counter()
                hits = store.search(query, 10, allowed)
                timings[label].append((time.perf_counter() - start) * 1000)
                if not hits or any(chunk_id not in allowed_sets[label] for chunk_id, _ in hits):
                    raise RuntimeError(f"invalid or unauthorized result in {label} benchmark")
        description = store.describe()
        return {
            "dataset": "synthetic normalized float32 vectors; no source documents or database rows",
            "host": {
                "cpu_logical_count": psutil.cpu_count(logical=True),
                "system_ram_bytes": psutil.virtual_memory().total,
            },
            "vectors": vector_count,
            "dimension": dimension,
            "queries_per_scope": query_count,
            "seed": seed,
            "build_seconds": round(build_seconds, 3),
            "build_vectors_per_second": round(vector_count / max(build_seconds, 1e-9), 1),
            "search": {label: _percentiles(samples) for label, samples in timings.items()},
            "index_snapshot_bytes": description["snapshot_bytes"],
            "index_float32_vector_bytes": description["memory_bytes"],
            "estimated_vector_ram_gib_at_1m": round(1_000_000 * dimension * 4 / 1024**3, 3),
            "estimated_vector_ram_gib_at_10m": round(10_000_000 * dimension * 4 / 1024**3, 3),
            "process_rss_delta_bytes": max(0, build_rss - rss_before),
            "index_type": description["index_type"],
            "warning": "Synthetic FAISS benchmark only; does not validate 600 GB ingestion, OCR, PostgreSQL, or production hardware.",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vectors", type=int, default=100_000)
    parser.add_argument("--dimension", type=int, default=384)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.vectors, args.dimension, args.queries, args.seed), indent=2))


if __name__ == "__main__":
    main()
