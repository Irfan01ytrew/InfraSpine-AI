"""Bounded ACL-filter and ID-enumeration benchmark for an isolated Qdrant server.

Requires QDRANT_TEST_URL and QDRANT_TEST_API_KEY (if configured). Creates a uniquely named
synthetic collection and leaves it in the isolated test service for review; it never deletes data.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import subprocess
import sys
import time
import tracemalloc
import uuid
from pathlib import Path

import numpy as np
import psutil
from qdrant_client import QdrantClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.qdrant_store import QdrantVectorStore


def _percentiles(samples: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(float(np.percentile(samples, 50)), 3),
        "p95_ms": round(float(np.percentile(samples, 95)), 3),
        "mean_ms": round(statistics.fmean(samples), 3),
    }


def _container_measurements(name: str | None) -> dict[str, str | None]:
    result: dict[str, str | None] = {"memory_usage": None, "storage_kib": None}
    if not name:
        return result
    memory = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if memory.returncode == 0:
        result["memory_usage"] = memory.stdout.strip()
    storage = subprocess.run(
        ["docker", "exec", name, "du", "-sk", "/qdrant/storage"],
        capture_output=True,
        text=True,
        check=False,
    )
    if storage.returncode == 0 and storage.stdout.split():
        result["storage_kib"] = storage.stdout.split()[0]
    return result


def benchmark(vector_count: int, dimension: int, query_count: int, concurrency: int, seed: int) -> dict:
    if vector_count < 1 or dimension < 8 or query_count < 1 or concurrency < 1:
        raise ValueError("vectors, queries, and concurrency must be positive; dimension must be at least 8")
    url = os.environ.get("QDRANT_TEST_URL")
    if not url:
        raise RuntimeError("Set QDRANT_TEST_URL to the isolated Qdrant test service.")
    api_key = os.environ.get("QDRANT_TEST_API_KEY")
    namespace = uuid.uuid4().hex[:16]
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((vector_count, dimension), dtype=np.float32)
    vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    queries = rng.standard_normal((query_count, dimension), dtype=np.float32)
    queries /= np.maximum(np.linalg.norm(queries, axis=1, keepdims=True), 1e-12)
    ids = np.arange(1, vector_count + 1, dtype=np.int64)
    allowed = ids[::10].tolist()
    filter_json_bytes = len(
        json.dumps({"must": [{"key": "chunk_id", "match": {"any": allowed}}]}, separators=(",", ":")).encode()
    )
    allowed_python_bytes = sys.getsizeof(allowed) + sum(sys.getsizeof(item) for item in allowed)

    client = QdrantClient(url=url, api_key=api_key, timeout=60)
    client.get_collections()
    directory = Path(os.environ.get("TEMP", "/tmp")) / f"infraspine-qdrant-bench-{namespace}"
    store = QdrantVectorStore(
        url=url,
        api_key=api_key,
        directory=directory,
        name=f"benchmark_{namespace}",
        dimension=dimension,
        model_name="synthetic-benchmark-v1",
        client=client,
    )
    process = psutil.Process()
    rss_before = process.memory_info().rss
    server_before = _container_measurements(os.environ.get("QDRANT_TEST_CONTAINER"))
    start = time.perf_counter()
    store.apply(add_ids=ids.tolist(), vectors=vectors)
    build_seconds = time.perf_counter() - start
    rss_after_build = process.memory_info().rss

    def query(vector: np.ndarray) -> tuple[list[tuple[int, float]], float]:
        started = time.perf_counter()
        hits = store.search(vector, 10, allowed)
        return hits, (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        measured = list(pool.map(query, queries))
    elapsed = time.perf_counter() - started
    results = [hits for hits, _latency in measured]
    latencies_ms = [latency for _hits, latency in measured]
    allowed_set = set(allowed)
    failures = sum(
        not hits or any(chunk_id not in allowed_set for chunk_id, _score in hits)
        for hits in results
    )

    tracemalloc.start()
    enumerated = sum(1 for _ in store.iter_ids(batch_size=1000))
    _, stream_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    tracemalloc.start()
    materialized_ids = set(store.iter_ids(batch_size=1000))
    _, materialized_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    server_after = _container_measurements(os.environ.get("QDRANT_TEST_CONTAINER"))
    report = {
        "dataset": "synthetic normalized float32 vectors; unique isolated Qdrant collection",
        "vectors": vector_count,
        "dimension": dimension,
        "allowed_ids": len(allowed),
        "allowed_fraction": round(len(allowed) / vector_count, 4),
        "queries": query_count,
        "concurrency": concurrency,
        "build_seconds": round(build_seconds, 3),
        "build_vectors_per_second": round(vector_count / max(build_seconds, 1e-9), 1),
        "acl_filter_json_bytes_estimate": filter_json_bytes,
        "allowed_id_python_list_bytes": allowed_python_bytes,
        "query_throughput_per_second": round(query_count / max(elapsed, 1e-9), 2),
        "query_latency_ms": _percentiles(latencies_ms),
        "failures": failures,
        "id_enumeration_count": enumerated,
        "id_enumeration_stream_peak_python_bytes": stream_peak,
        "id_enumeration_materialized_peak_python_bytes": materialized_peak,
        "id_enumeration_materialized_count": len(materialized_ids),
        "client_rss_delta_after_build_bytes": max(0, rss_after_build - rss_before),
        "qdrant_container_before": server_before,
        "qdrant_container_after": server_after,
        "collection": store._active_collection,
        "warning": "Synthetic bounded benchmark only; it does not qualify 600 GB capacity or database ACL-query cost.",
    }
    store.close()
    if failures:
        raise RuntimeError(f"Qdrant benchmark produced {failures} invalid or empty filtered results.")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vectors", type=int, default=10000)
    parser.add_argument("--dimension", type=int, default=384)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20261022)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.vectors, args.dimension, args.queries, args.concurrency, args.seed), indent=2))


if __name__ == "__main__":
    main()
