## Step H chunking-only projection — 2026-11-28 (cloud VM, bounded)

Measured by Agent 115 with `app/qa/ingestion_projection.py` (deterministic
synthetic TXT corpus, real `split_text` chunker, single worker, wall-clock
on this cloud VM — 1.5 shared CPUs):

- Corpus: 200 pages × ~4 KB = 823,890 bytes; chunking took **0.004 s
  (~200 MB/s)**.
- Linear projection to 600 GiB at that rate: **~0.86 hours**.
- Sublinear projection (scaling exponent 0.9, throughput degrading as the
  index grows): **~3.32 hours**.

**This is a lower bound, not a qualification.** Chunking is the cheapest
ingestion stage: parsing, embedding and index writes are NOT in the number,
and the real pipeline can only be slower. The measurement itself is a
4 ms timing over an 824 KB corpus, so it is noisy; re-measure on target
hardware with a larger corpus before trusting the rate. 600 GB readiness
remains unverified, as stated above.

## Current-source bounded FAISS run — 2026-10-29 (native Windows)

Command: `python scripts/benchmark_vector_store.py --vectors 10000 --dimension 384 --queries 20 --seed 20261029` on the available Windows host (12 logical CPUs, 31.8 GiB RAM). The current-source synthetic 10,000-vector/384-dimension build took **0.065 s (154,678.8 vectors/s)**. Full-index search p50/p95/p99 was **5.404/6.034/6.670 ms**; 10% scoped search was **1.852/2.129/2.174 ms**. The 15,440,090-byte index snapshot added 16,773,120 bytes RSS.

Evidence: `.verification-local-review-20261029-c19-bounded/evidence/c19-current-source-faiss-benchmark.json` (SHA-256 `e2f1976fe3d6dc931780af14574873c33ad1ec59dbbd6107a0a335bf71f691e8`). This uses generated vectors only; it does not cover source parsing, embeddings from documents, PostgreSQL ACL cost, concurrency, interruption/recovery, Qdrant server persistence, or production/target hardware. It is not evidence of 600 GB readiness.

## Bounded application worker measurement — 2026-10-28 (native Windows)

The current-source folder-sync worker processed one fresh synthetic source on the retained loopback-only PostgreSQL cluster, database `infraspine_f08throughput2_20261028_test`, migration 0059. The focused test `tests/test_folder_sync_worker_throughput.py::test_bounded_native_worker_ingests_a_small_synthetic_source` passed **1/1 in 143.01s**.

- Eight synthetic TXT files, 7,120 bytes total, became eight ready indexed documents; zero failures.
- End-to-end elapsed time was **132.063s (0.061 files/s)** across two cold `app.worker --once` processes. The first pass scheduled/imported the directory entries; the second consumed the queued documents. The run used the pinned local MiniLM model, FAISS, and one-thread CPU limits.
- Exact machine-test evidence: `.verification-local-review-20261028-f02-meetings/evidence/f08-native-worker-throughput.json`.

This is a tiny cold-start host measurement, not representative throughput or target-hardware qualification. It does not establish sustained memory/disk behavior, concurrency, large-tree ingestion, mixed-media processing, or 600 GB readiness. The separate actual worker-process interruption/restart test passed **1/1 in 53.14s**; target-hardware throughput and recovery qualification remain pending.

## Latest bounded F08 host measurement — 2026-10-27 (synthetic file-tree scan/recovery harness)

This host-only measurement used 1,000 synthetic UTF-8 `.txt` files of 4,100 bytes each (4,100,000 bytes total). It called the current source tree helpers `_files`, `_open_import_source` and `_sha256`; durable per-file restart checkpoints were recorded in a local SQLite harness. It did not run the application PostgreSQL importer, parser, embedder, worker or customer data.

- Initial 500-file scan/hash/checkpoint pass: 6.662 s, 75.0 files/s.
- Simulated restart rescanned 1,000 entries in 9.678 s (103.3 scan/checkpoint operations/s), recognized 500 unchanged files, and checkpointed the remaining 500.
- All 1,000 hashes/checkpoints and SQLite integrity checks passed after restart.

F08's passed browser flows were not repeated. This bounded synthetic harness demonstrates local scan/hash throughput and its SQLite checkpoint restart behavior only; it does not demonstrate application-level PostgreSQL/worker interruption recovery, representative large-tree scale, or target-hardware capacity. **Target-hardware qualification remains pending.**

## Latest bounded run — 2026-10-23 (Qdrant, 10k vectors)

This rerun used the actual isolated Qdrant 1.19 server after Docker recovery, Windows 11 host, Python 3.12.10, 12 logical CPUs, 31.8 GiB physical RAM, and a 15.52 GiB Qdrant container memory limit. Dataset: 10,000 synthetic normalized float32 vectors at 384 dimensions; ACL allowed 1,000 IDs (10%); 20 queries, concurrency 1, seed `20261023`.

Command: `scripts/benchmark_qdrant_acl.py --vectors 10000 --dimension 384 --queries 20 --concurrency 1 --seed 20261023`.

- Build/upsert: 10.028 s (997.2 vectors/s).
- Search: 51.3 queries/s; p50 22.184 ms, p95 25.129 ms, mean 19.157 ms; zero failures.
- ACL filter JSON estimate: 4,936 bytes; Python allowed-ID list: 36,056 bytes.
- Enumerating 10,000 IDs: 1,344,508-byte streaming peak versus 2,055,829-byte materialized peak.
- Client RSS delta: 12,378,112 bytes. Qdrant process memory: 66.16 MiB before and 181.3 MiB after; storage: 2,760 KiB before and 39,112 KiB after.

The benchmark collection is synthetic and was retained in the isolated QA Qdrant volume. It does not include document parsing, PostgreSQL ACL-query latency, concurrent traffic, recovery under load, or target-hardware limits. **600 GB readiness remains unverified.**

## Earlier same-day bounded run — 2026-10-23 (10k vectors)

Run on the Windows 11 host with Python 3.12.10 and Docker Desktop `desktop-linux`; Qdrant 1.19 ran in the uniquely named, isolated test project. The host reports 12 logical CPUs and 31.8 GiB RAM; the Qdrant container limit was 15.52 GiB. Both benchmarks used 10,000 synthetic normalized float32 vectors at 384 dimensions. These are current small-sample measurements, not a production capacity test.

FAISS command: `python scripts/benchmark_vector_store.py --vectors 10000 --dimension 384 --queries 20 --seed 20261023`.

- Build: 0.098 s (102,451.7 vectors/s).
- Exact search over all vectors: p50 6.021 ms, p95 7.681 ms, p99 9.071 ms.
- Search over a 10% ID scope: p50 2.208 ms, p95 2.645 ms, p99 2.689 ms.
- Snapshot: 15,440,090 bytes; raw float32 vectors: 15,360,000 bytes; process RSS increase: 16,605,184 bytes.

Qdrant command: `python scripts/benchmark_qdrant_acl.py --vectors 10000 --dimension 384 --queries 20 --concurrency 1`.

- Upsert: 6.945 s (1,439.8 vectors/s); 20 sequential queries, concurrency 1, filtered to 1,000 allowed IDs; zero permission-filter failures.
- Query latency: p50 17.006 ms, p95 33.143 ms, mean 19.651 ms (50.57 queries/s).
- ACL filter JSON estimate: 4,936 bytes; Python ID list: 36,056 bytes.
- Enumerating 10,000 IDs: 1,344,743-byte streaming peak versus 2,055,688-byte materialized peak.
- Client RSS increase after build: 12,189,696 bytes. Qdrant container memory: 72.28 MiB before and 225.5 MiB after; storage: 2,100 KiB before and 38,572 KiB after.

Neither benchmark includes source-file parsing, PostgreSQL ACL-query cost, mixed-media processing, concurrent retrieval, recovery, or target-hardware limits. **600 GB readiness remains unverified.**

## Previous bounded Qdrant run — 2026-10-22 (100k vectors)

Command run by `scripts/verify_host.ps1 -BenchmarkVectors 100000` against the isolated Qdrant 1.19 test server. The host used the recovered Python 3.12.10 environment; the Qdrant service ran in Docker Desktop.

```powershell
python scripts/benchmark_qdrant_acl.py --vectors 100000 --dimension 384 --queries 20 --concurrency 1
```

- Host: 12 logical CPUs, 31.8 GiB RAM. Qdrant container limit: 15.52 GiB.
- Synthetic set: 100,000 normalized vectors, 384 dimensions; ACL scope contains 10,000 IDs (10%); 20 sequential queries, concurrency 1.
- Build/upsert: 96.761 s (1,033.5 vectors/s).
- Search: 13.39 queries/s; p50 28.606 ms, p95 104.760 ms, mean 74.466 ms; zero permission-filter failures.
- ACL filter JSON estimate: 58,936 bytes. Python allowed-ID list: 360,056 bytes.
- Enumerating 100,000 IDs: 1,529,286-byte peak Python allocation when streamed, versus 9,277,904 bytes materialized.
- Client RSS delta after build: 16,293,888 bytes. Qdrant process memory: 74.44 MiB before, 134.4 MiB after. Storage: 2,100 KiB before, 244,896 KiB after.
- This smoke run measures the vector service, client-side ACL ID list, and synthetic vectors. It does not include PostgreSQL ACL-query time, concurrent load, media/model processing, corpus ingestion from disk, backups, failover, or adversarial ACL recall.

The actual Qdrant server integration test passed for persistence/rebuild, interrupted-rebuild recovery, duplicate prevention, alias cutover/rollback, updates, deletion, and permission revocation. This is a bounded behavior test and 100k-vector performance sample, not a production-scale capacity qualification.

## Previous bounded FAISS run — 2026-10-22 (100k vectors)

The host verification runner ran `python scripts/benchmark_vector_store.py --vectors 100000 --dimension 384 --queries 10` on the same 12-logical-CPU, 31.8-GiB host.

- 100,000 normalized float32 vectors at 384 dimensions; 10 queries per scope; deterministic seed `20261021`.
- Build: 0.901 s (110,954.6 vectors/s).
- Exact full-index search: p50 59.895 ms, p95 72.499 ms, p99 73.578 ms, mean 59.789 ms.
- Search restricted to 10% of vector IDs: p50 8.388 ms, p95 9.580 ms, p99 10.042 ms, mean 8.564 ms.
- Snapshot: 154,400,090 bytes; raw vectors: 153,600,000 bytes; process RSS increase: 162,615,296 bytes.

This is a current synthetic vector-only measurement. The 10% scope does not include the PostgreSQL ACL query and is not a retrieval-quality or production-throughput test.

## Previous bounded Qdrant run — 2026-10-22 (10k vectors)

An earlier same-day smoke run built 10,000 vectors at 1,874.2 vectors/s, with p50 20.104 ms and p95 33.253 ms across 20 sequential searches. It is retained as a smaller-sample comparison; the 100k run above is the current measurement.

## Previous bounded FAISS run — 2026-10-22 (10k vectors)

An earlier same-day smoke run built 10,000 vectors at 77,415.3 vectors/s, with p50 13.562 ms and p95 23.292 ms for full-index search. It is retained as a smaller-sample comparison; the 100k run above is the current measurement.

## Historical FAISS sample — 2026-10-21

Run with `python scripts/benchmark_vector_store.py --vectors 100000 --dimension 384 --queries 50`. The deterministic synthetic data was held in a temporary local FAISS index; it did not use the application database or document directory.

- 100,000 vectors at 384 float32 dimensions; 50 queries for each of two scopes.
- Build: 0.732 s (136,657 vectors/s).
- Exact full-index search: p50 56.683 ms, p95 66.080 ms, p99 77.168 ms.
- Search restricted to 10% of vector IDs: p50 8.507 ms, p95 10.975 ms, p99 13.015 ms.
- FAISS snapshot: 154,400,090 bytes; raw vectors: 153,600,000 bytes; process RSS increase during build: 162,574,336 bytes.

This is historical evidence for the exact in-memory `IndexFlatIP` backend, not a current service benchmark.

## Capacity interpretation and limits

At 384 dimensions, raw float32 vector data alone is 1,536 bytes per chunk: approximately 1.43 GiB for 1 million chunks, 14.30 GiB for 10 million, or 143.05 GiB for 100 million. IDs, metadata, database embeddings, text, indexes, OCR, parsers, models, concurrency, and operating-system cache require additional resources.

FAISS live-index memory scales with the number of vectors and its rebuild path still materializes/sorts IDs for reconciliation. Qdrant's configured on-disk behavior and bounded ID enumeration reduce some client-side pressure, but the 100k result cannot predict a 600 GB source corpus. Chunk count depends on formats, extracted text, chunk settings, and duplicates.

**600 GB readiness remains unverified.** The 100k Qdrant run does not establish ingestion throughput, sustained memory/disk behavior, retrieval quality, full backup/restore, concurrency limits, or service-level objectives at target scale. Qualification requires representative synthetic corpora and target hardware with ACL-query profiling, sustained ingestion/recovery tests, recall/security evaluation, and monitored mixed-media load. No customer corpus or server was used.
