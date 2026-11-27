"""Step C benchmark harness: synthetic scoped corpus -> ingestion and ACL-filtered retrieval.

It models the platform-core retrieval shape: every chunk carries the scope it belongs to,
and a caller may only see chunks in the scopes `visible_scopes()` would return. The harness
measures ingestion rate (chunking + indexing), filtered search latency (p50/p95/p99) and
throughput, and checks that no result ever leaks from an invisible scope.

Backends:
  faiss   (default) in-process exact inner-product index with an ID-selector scope filter.
  qdrant  an isolated test server from QDRANT_TEST_URL (QDRANT_TEST_API_KEY optional) with a
          `scope_id` payload index and payload-filtered search. It creates a uniquely named
          collection and never deletes anything.

Vectors are synthetic (seeded, normalised float32); no model, application database or RAG
data is touched. Examples (from the repository root):
    python scripts/benchmark_platform_retrieval.py --chunks 1000000 --queries 200
    python scripts/benchmark_platform_retrieval.py --chunks 10000000 --dimension 384 --out report.json
    QDRANT_TEST_URL=http://127.0.0.1:6333 python scripts/benchmark_platform_retrieval.py --backend qdrant
Results are a capacity signal for the hardware it runs on; record them with the report's
`hardware` block before quoting tier numbers.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

WORDS = ("invoice payment vendor budget approval policy contract asset ledger audit payroll "
         "department workspace request report delegation receipt tender procurement salary").split()


@dataclass
class Corpus:
    """Synthetic documents spread over `scopes`; chunk ids are 0..n-1 in insertion order."""
    scope_of_chunk: np.ndarray            # int32 scope index per chunk
    vectors: np.ndarray                   # float32 (n, d), L2-normalised
    documents: int
    chunk_words: int
    text_bytes: int
    chunking_seconds: float


@dataclass
class Report:
    backend: str
    params: dict
    hardware: dict
    ingestion: dict = field(default_factory=dict)
    search: dict = field(default_factory=dict)
    acl: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__


def hardware_info() -> dict:
    info = {"python": platform.python_version(), "machine": platform.machine(), "system": platform.system(),
            "cpu_count": os.cpu_count(), "measured_at": datetime.now(UTC).isoformat()}
    try:
        import psutil
        info["memory_bytes"] = psutil.virtual_memory().total
        freq = psutil.cpu_freq()
        info["cpu_mhz"] = round(freq.max or freq.current) if freq else None
    except Exception:  # psutil is optional for the harness
        pass
    info["processor"] = platform.processor() or None
    return info


def percentiles(samples_ms: list[float]) -> dict[str, float]:
    if not samples_ms:
        return {}
    return {"p50_ms": round(float(np.percentile(samples_ms, 50)), 3),
            "p95_ms": round(float(np.percentile(samples_ms, 95)), 3),
            "p99_ms": round(float(np.percentile(samples_ms, 99)), 3),
            "mean_ms": round(statistics.fmean(samples_ms), 3)}


def _normalise(matrix: np.ndarray) -> np.ndarray:
    matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    return matrix


def make_corpus(chunks: int, dimension: int, scopes: int, chunk_words: int, seed: int,
                chunks_per_document: int = 20) -> Corpus:
    """Generate documents, chunk their text, and give each chunk a vector and a scope.

    Text is generated and chunked only for a bounded sample (to time the chunker honestly
    without holding 10M chunks of text in memory); the rate is extrapolated and labelled.
    """
    if chunks < 1 or dimension < 8 or scopes < 1 or chunk_words < 8:
        raise ValueError("chunks, scopes must be positive; dimension >= 8; chunk_words >= 8")
    rng = np.random.default_rng(seed)
    documents = max(1, -(-chunks // chunks_per_document))
    doc_scope = rng.integers(0, scopes, size=documents, dtype=np.int32)
    scope_of_chunk = np.repeat(doc_scope, chunks_per_document)[:chunks]
    sample_docs = min(documents, 200)
    words = np.array(WORDS)
    started = time.perf_counter()
    text_bytes = 0
    sampled_chunks = 0
    for _ in range(sample_docs):
        text = " ".join(words[rng.integers(0, len(words), size=chunk_words * chunks_per_document)])
        tokens = text.split()
        for i in range(0, len(tokens), chunk_words):
            piece = " ".join(tokens[i:i + chunk_words])
            text_bytes += len(piece.encode())
            sampled_chunks += 1
    sample_seconds = time.perf_counter() - started
    chunking_seconds = sample_seconds * chunks / max(sampled_chunks, 1)
    vectors = _normalise(rng.standard_normal((chunks, dimension), dtype=np.float32))
    return Corpus(scope_of_chunk, vectors, documents, chunk_words,
                  int(text_bytes * chunks / max(sampled_chunks, 1)), chunking_seconds)


def visible_scope_sets(scopes: int, users: int, fraction: float, seed: int) -> list[np.ndarray]:
    """Per synthetic user, the scope indices they may see (a stand-in for visible_scopes())."""
    rng = np.random.default_rng(seed + 1)
    count = max(1, round(scopes * fraction))
    return [np.sort(rng.choice(scopes, size=count, replace=False)).astype(np.int32) for _ in range(users)]


class FaissBackend:
    name = "faiss"

    def __init__(self, dimension: int):
        import faiss
        self.faiss = faiss
        self.index = faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
        self.scope_of_chunk: np.ndarray | None = None

    def ingest(self, corpus: Corpus, batch: int) -> None:
        self.scope_of_chunk = corpus.scope_of_chunk
        n = len(corpus.vectors)
        for start in range(0, n, batch):
            stop = min(n, start + batch)
            self.index.add_with_ids(corpus.vectors[start:stop], np.arange(start, stop, dtype=np.int64))

    def search(self, vector: np.ndarray, k: int, visible: np.ndarray) -> list[int]:
        allowed = np.flatnonzero(np.isin(self.scope_of_chunk, visible)).astype(np.int64)
        if allowed.size == 0:
            return []
        selector = self.faiss.IDSelectorBatch(allowed)
        params = self.faiss.SearchParameters(sel=selector)
        _scores, ids = self.index.search(vector.reshape(1, -1), k, params=params)
        return [int(i) for i in ids[0] if i >= 0]

    def close(self) -> None:
        self.index.reset()


class QdrantBackend:
    name = "qdrant"

    def __init__(self, dimension: int):
        from qdrant_client import QdrantClient, models
        url = os.environ.get("QDRANT_TEST_URL")
        if not url:
            raise RuntimeError("Set QDRANT_TEST_URL to an isolated Qdrant test service to use --backend qdrant.")
        self.models = models
        self.client = QdrantClient(url=url, api_key=os.environ.get("QDRANT_TEST_API_KEY"), timeout=120)
        self.collection = f"platform_bench_{uuid.uuid4().hex[:16]}"
        self.client.create_collection(self.collection, vectors_config=models.VectorParams(
            size=dimension, distance=models.Distance.DOT))
        self.client.create_payload_index(self.collection, "scope_id", models.PayloadSchemaType.INTEGER)

    def ingest(self, corpus: Corpus, batch: int) -> None:
        n = len(corpus.vectors)
        for start in range(0, n, batch):
            stop = min(n, start + batch)
            self.client.upsert(self.collection, wait=True, points=self.models.Batch(
                ids=list(range(start, stop)), vectors=corpus.vectors[start:stop].tolist(),
                payloads=[{"scope_id": int(s)} for s in corpus.scope_of_chunk[start:stop]]))

    def search(self, vector: np.ndarray, k: int, visible: np.ndarray) -> list[int]:
        flt = self.models.Filter(must=[self.models.FieldCondition(
            key="scope_id", match=self.models.MatchAny(any=[int(s) for s in visible]))])
        hits = self.client.query_points(self.collection, query=vector.tolist(), limit=k, query_filter=flt).points
        return [int(h.id) for h in hits]

    def close(self) -> None:
        self.client.close()


BACKENDS = {"faiss": FaissBackend, "qdrant": QdrantBackend}


def run(*, backend: str = "faiss", chunks: int = 20000, dimension: int = 384, scopes: int = 50,
        users: int = 20, visible_fraction: float = 0.2, queries: int = 50, k: int = 10,
        concurrency: int = 1, chunk_words: int = 120, batch: int = 10000, seed: int = 20261126) -> dict:
    params = {k_: v for k_, v in locals().items()}
    report = Report(backend=backend, params=params, hardware=hardware_info())
    corpus = make_corpus(chunks, dimension, scopes, chunk_words, seed)
    store = BACKENDS[backend](dimension)
    try:
        started = time.perf_counter()
        store.ingest(corpus, batch)
        index_seconds = time.perf_counter() - started
        report.ingestion = {
            "documents": corpus.documents, "chunks": chunks,
            "text_bytes_estimate": corpus.text_bytes,
            "chunking_seconds_extrapolated": round(corpus.chunking_seconds, 3),
            "index_seconds": round(index_seconds, 3),
            "index_chunks_per_second": round(chunks / max(index_seconds, 1e-9), 1),
            "embedding": "synthetic (not measured; add the real model's rate from the GPU tier)",
        }
        user_scopes = visible_scope_sets(scopes, users, visible_fraction, seed)
        rng = np.random.default_rng(seed + 2)
        query_vectors = _normalise(rng.standard_normal((queries, dimension), dtype=np.float32))

        def one(i: int) -> tuple[int, list[int], float]:
            visible = user_scopes[i % users]
            t0 = time.perf_counter()
            hits = store.search(query_vectors[i], k, visible)
            return i, hits, (time.perf_counter() - t0) * 1000

        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            measured = list(pool.map(one, range(queries)))
        elapsed = time.perf_counter() - started
        leaks = empty = 0
        for i, hits, _ms in measured:
            visible = set(user_scopes[i % users].tolist())
            leaks += sum(int(corpus.scope_of_chunk[h]) not in visible for h in hits)
            empty += not hits
        report.search = {"queries": queries, "k": k, "concurrency": concurrency,
                         "throughput_qps": round(queries / max(elapsed, 1e-9), 2),
                         "latency": percentiles([ms for _i, _h, ms in measured])}
        report.acl = {"visible_fraction": visible_fraction, "users": users,
                      "leaked_results": leaks, "empty_results": empty}
        if leaks:
            report.warnings.append(f"{leaks} results came from scopes the user cannot see.")
        report.warnings.append("Synthetic vectors: quality/recall of the real embedding model is not measured.")
        if backend == "qdrant":
            report.params["collection"] = store.collection
    finally:
        store.close()
    return report.as_dict()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=sorted(BACKENDS), default="faiss")
    parser.add_argument("--chunks", type=int, default=20000)
    parser.add_argument("--dimension", type=int, default=384)
    parser.add_argument("--scopes", type=int, default=50)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--visible-fraction", type=float, default=0.2)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--chunk-words", type=int, default=120)
    parser.add_argument("--batch", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--out", help="write the JSON report here as well as stdout")
    args = parser.parse_args()
    result = run(backend=args.backend, chunks=args.chunks, dimension=args.dimension, scopes=args.scopes,
                 users=args.users, visible_fraction=args.visible_fraction, queries=args.queries, k=args.k,
                 concurrency=args.concurrency, chunk_words=args.chunk_words, batch=args.batch, seed=args.seed)
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    if result["acl"]["leaked_results"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
