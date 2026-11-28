# Step H — Founder scenario + load test + ingestion: implementation plan

Prepared by Agent 17 (Step H preparation), branch `agent/steph-founder`,
base `step-e-gaps-wip` `ca956c2`. Status: **ALL SLICES DONE by Agent 115**
(2026-11-28): slice 1 (Agent 17) + slices 2–3 (adapter + scenario tests +
founder-only route) + slices 4–5 bounded (harness + small-scale runs only —
full qualification needs target-like hardware) + slice 6 (CHANGES-step-H.md
+ gates). Scenario 8/8 passed, slice-1 canonical 32 passed, load file 8
passed, Alembic roundtrip clean. Ready to merge after renumbering
migration 0104. The full Step H build is still ahead
and must be done in plan order after Step E is green and Steps F–G are
finished.

## 1. What Step H is (HANDOVER-PROMPT.md §4)

Founder scenario end-to-end, plus scale qualification:

1. A **Roman Urdu voice note** from the founder drives real work:
   - finance spend chart,
   - "32 of 50 invoices verified" headline,
   - continuity replacement **with an access check**,
   - an approval **bound to the exact action**,
   - **separate** verify / approve-payment / transfer permissions
     (separation of duties — one person cannot do all three for one batch).
2. **Load test for 1,000+ users.**
3. **600 GB ingestion qualification.**

Rules that bind all of Step H (handover §5): every lookup/dispatch through
`app/platform` (`resolve_principal`, `authorize`, `require`); voice transcripts,
documents and web pages are **untrusted input** — text in them can never grant a
permission, change a payee, or change what an approval covers; approvals are
bound to the exact action (hash), expire, single-use, separation of duties;
every side effect goes through `execute_once` with read-back verification;
tests only on throwaway `*_test` databases; quality bar is the full suite green
(≥1347 passed, 0 failed), Alembic round trip + `alembic check` clean, frontend
clean, `CHANGES-step-H.md` in plain language, small logical commits.

## 2. What already exists (mapped 2026-11-28)

### Voice notes
- `POST /api/rag/voice-question` (`app/api/routes/rag.py:959`) + 
  `app/rag/voice_questions.py`: local Whisper via `AudioParser.parse_spoken_question`,
  8 MB / 120 s cap, transcript is **not stored** and no RAG trace is written.
  `transcribe_spoken_question()` / `transcribe_voice_upload()` exist.
- Tests: `tests/test_voice_question_upload_unit.py`. NOTE: in the Agent 1 baseline
  (2026-11-27) 4 of these failed with `PermissionError` creating temp dirs under a
  root-owned worktree while running as user `agent1` — environmental, not code.
- **Gap:** the voice route only *answers a question*. Nothing turns a voice note
  into a workflow run / action. No Roman Urdu intent handling exists.

### Finance (the "32 of 50" headline and charts)
- `tests/test_finance_invoice_flow.py` already asserts the headline
  `'32 of 50 verified'` (4 places) — the verification summary exists and is tested.
- `app/rag/charts.py`: `build_chart()` / `parse_chart()` — chart answers from
  retrieved chunks, with rejection rules.
- `app/finance/verification.py`, `app/finance/service.py`, `app/finance/workflow.py`:
  invoice intake, 17 checks, approval flow.
- `app/services/approval_authority.py`: approval limits per position/person,
  **with separation of duties**.
- **Gap:** no test ties the voice note to the chart + verification headline in one
  founder flow.

### Continuity replacement with access check
- `app/continuity/` exists: `service.py`, `access.py` (`scope_for()` /
  `node_for()` / `nodes_for()` — department-scoped access), `journal.py`,
  `intelligence.py`, `routes.py`, `schemas.py`, `models.py`.
- **Gap:** no founder-scenario test proves a replacement is refused when the
  requester lacks access to the node/department, and allowed when they have it.

### Approvals (exact action, single use, SoD)
- Governance runtime (`app/services/agent_action_runtime.py`, wired into
  `agent_actions.py`, engine tick) + unified workflow engine
  (`app/workflows/unified.py`, `execute_once` ledger) — proven by Gap 1/1b tests.
- **Gap:** no scenario test proves a voice-note approval request is bound to the
  exact action hash and cannot be reused for a different action, and that
  verify / approve-payment / transfer stay separate permissions.

### Ingestion
- `app/rag/bulk_import.py`: `import_folder()` (sha256, per-source locks,
  storage via `DocumentStorage`), folder-sync worker (`app/worker.py`).
- `tests/test_folder_sync_worker_throughput.py`: bounded native run — 8 synthetic
  TXT files (7,120 bytes) ingested at **0.061 files/s** cold across two
  `app.worker --once` processes (143 s test). A 1,000-file synthetic scan/hash
  harness hit 75 files/s scan, 103 ops/s restart — but that harness never ran the
  real PostgreSQL importer, parser, embedder or worker.
- Docling parsers, `all-MiniLM-L6-v2` embeddings, FAISS ↔ Qdrant.
- **Gap:** no sustained-throughput, mixed-media, large-tree, or
  recovery-under-load measurement at anything near 600 GB.

### Load testing
- `scripts/load_test_review_stack.py`: concurrent login burst (Argon2) + waves of
  authenticated reads (`/api/auth/me`, `/api/rag/documents`, `/api/conversations`);
  **LLM chat is explicitly excluded**; single host only.
- `scripts/benchmark_platform_retrieval.py`, `benchmark_qdrant_acl.py`,
  `benchmark_vector_store.py`: synthetic 10k-vector FAISS/Qdrant measurements
  (see `docs/capacity-benchmark.md` — every entry ends with "600 GB readiness
  remains unverified" / "target-hardware qualification remains pending").
- `tests/test_phase7_enterprise_bench.py`: opt-in (`RUN_PHASE7_BENCHMARKS=1`),
  400 requests, API load stubs answer generation, no provider calls.
- `docs/capacity-benchmark.md` + `assessment/stepB/architecture-and-capacity-plan.md`:
  the 600 GB / 1,000+ users capacity plan exists on paper only.
- **Gap:** no 1,000-concurrent-user test, no chat/ingestion under load, no target
  hardware, no sustained-run (soak) test.

## 3. First safe slice (implemented by Agent 17 — this branch)

Two new pure modules under `app/qa/` (Module 12: independent quality evidence,
never part of RAG execution) + tests. No production behaviour changed, no
migration, no network calls, no real audio model, no real large ingestion.

1. **`app/qa/load_harness.py`** — transport-agnostic async load harness:
   `LoadScenario` (name, virtual users, duration/iterations, request mix),
   `run_load_pass()` driving N virtual users with `asyncio` against any injected
   async transport callable, `LoadReport` with requests/errors/throughput and
   p50/p95/p99/max/mean latencies. Deliberately separate from
   `scripts/load_test_review_stack.py` (which is tied to one live review stack);
   the harness is reusable for the 1,000-user test and for ingestion load.
2. **`app/qa/founder_voice.py`** — Roman Urdu voice-note intake parsing +
   safety validation: `parse_founder_voice_command()` turns a transcript into a
   structured `FounderVoiceCommand` (intent + parameters, `requires_approval`
   always true); `validate_voice_command()` rejects bypass language
   ("bina approval", "skip approval", permission grants, payee changes) and
   marks hostile transcripts as rejected. A parsed command is a **request only**:
   it is never executable by itself, never grants a permission, and must go
   through the unified workflow engine with an exact-action approval and
   `execute_once`.
3. **Tests `tests/test_step_h_founder_load_harness.py`**: harness statistics,
   concurrency, error counting; voice intents (spend chart, invoice verification,
   continuity replacement, approval request); hostile-transcript rejection;
   verify / approve-payment / transfer stay three separate permissions.

## 4. What remains for Step H (full build, in order)

1. **Founder voice-note → workflow run — DONE (Agent 115):** `app/founder_voice/`
   adapter (`record_voice_command` idempotent on the action hash,
   `mirror_voice_command` onto the unified engine as `origin='founder_voice'`,
   `mirror_pending_voice_commands` tick sweep, `on_workflow_cancelled`,
   `FounderVoiceExecuteHandler`); migration `0104_founder_voice_commands`
   (renumber at merge — 0104 also claimed by HR/AE/AI/G/foundry). The sealed
   transcript is stored only as an untrusted input record, never as authority.
   Correction found during the build: slice 1's `REQUIRED_PERMISSIONS` named
   permissions that do not exist in the catalog (`finance.invoices.verify`,
   `finance.payments.approve`, `finance.payments.transfer`,
   `continuity.replacement.request`, `governance.approve`) — corrected to the
   real ones (`finance.read`, `invoice.verify`, `payment.approve`,
   `funds.transfer`; continuity has none, its tenant scope is the authority).
   The action node and the `execute_once` duty use `finance.read` on purpose:
   the handler only *reads* the chart/headline; the duty the request is
   *about* belongs to whoever later performs it through the finance workflow.
2. **End-to-end founder scenario test — DONE (Agent 115):**
   `tests/test_step_h_founder_scenario.py` (throwaway DB, real finance flow):
   Roman Urdu voice note → spend chart + "32 of 50 verified" (the headline
   the finance workflow itself produced) + continuity replacement
   allowed/denied by the access check + approval bound to the exact action
   hash + SoD (requester can never approve their own note; a decision is
   single-use) + tampered transcript / other owner's command never execute +
   `execute_once` ledger holds exactly one verified execution.
3. **Voice-note upload permissions — DONE (Agent 115):** `POST /api/founder-voice/note`
   (JSON) and `/note-audio` (audio → `transcribe_voice_upload`, same 8 MB /
   120 s caps, staged audio discarded) are founder-only: `AdminUser` (role
   admin) + `require_scope` department membership; `GET /commands/{hash}`
   read-only. Whisper API fallback stays behind the local-model-first rule
   (handover §5 rule 2). Retention decision still open (see §5): transcripts
   ARE stored sealed in `founder_voice_commands` for audit — the owner must
   confirm how long they are kept.
4. **1,000+ user load test:** extend the harness to the real API (auth + chat
   with stubbed provider + document reads + ingestion), run against a staging
   stack on target-like hardware, report p50/p95/p99, error rate, and the
   breaking point; fix the first bottleneck found, re-run.
5. **600 GB ingestion qualification:** staged synthetic corpus (mixed
   PDF/DOCX/TXT/media), sustained ingestion throughput, worker restart under
   load, memory/disk behaviour, time-to-index projection for 600 GB; update
   `docs/capacity-benchmark.md` with real numbers.
6. **Docs + release:** `assessment/CHANGES-step-H.md` (plain language, file list,
   test counts), Alembic round trip + check if any migration is added, frontend
   clean if any UI changes (founder scenario UI), full suite green, merge only
   when green, live-push only with owner approval + verified backup.

## 5. Risks / open questions for the owner

- **Hardware:** 600 GB / 1,000-user qualification needs target-like hardware;
  this cloud VM (1.5 shared CPUs) cannot qualify it — only the harness and
  small-scale runs can be proven here.
- **Models:** HuggingFace is unreachable from this VM (Agent 1 baseline: 9
  failures) — Docling/embedding-dependent ingestion tests need pre-staged models
  or mocks; the full ingestion qualification cannot run here.
- **Whisper API fallback:** needs an API key decision by the owner; local Whisper
  is the default and stays first.
- **Voice-note retention:** the current route deliberately does not store
  transcripts; Step H must decide (owner) whether founder voice notes are stored
  for audit, and for how long.
