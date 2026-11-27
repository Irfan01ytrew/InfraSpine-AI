# InfraSpine AI

InfraSpine AI is a FastAPI and React enterprise knowledge platform with database-driven provider management (including the existing Gemini-compatible integration), encrypted credentials, Admin/Manager/Employee permissions, department-scoped document access, PDF and Office RAG with citations, employee knowledge handoffs and self-service requests, scoped governance approvals, research agents and management reports.

Knowledge chat finds documents by their content and by their identity (a filename, title, month, date or site),
ranks documents before passages, keeps multi-turn conversations on the documents they are about, and shows an answer
only when its cited passages support it. Design notes are in `docs/`:

| Document | Covers |
|---|---|
| `docs/hybrid-retrieval.md` | Lexical + semantic search, query analysis, typo tolerance |
| `docs/document-discovery.md` | Document descriptors, hierarchical (document → passage) retrieval |
| `docs/conversation-context.md` | Per-turn source state, follow-ups, safe fallback to general answers |
| `docs/evidence-validation.md` | How cited answers are checked; numbers, amounts, dates; credentials |
| `docs/on-premises-deployment.md` | Local deployment, model configuration, folder sync, backup/restore and capacity limits |
| `docs/windows-development.md` | Isolated Windows Python environment, PostgreSQL test database and verification commands |
| `docs/capacity-benchmark.md` | Measured 100,000-vector FAISS sample and explicit 600 GB capacity caveats |
| `docs/completion-matrix.md` | Evidence ledger for all 49 core, approved and advanced requirements |
| `docs/release-readiness.md` | Release decisions, verified boundaries, open blockers and required owner action |
| `docs/completion-checkpoint.md` | Current verification environment and pass evidence |
| `docs/roles-and-permissions.md` | Built-in roles, department-scoped permission templates, revocation and current limits |

## Prerequisites

- Python 3.12+ and Node.js 20+
- PostgreSQL 14+ (developed against 17). No extensions needed: vectors are stored in a local FAISS index.
- ~1.3 GB disk for Python dependencies (CPU-only PyTorch) and ~90 MB for the embedding model

## Setup

```bash
# 1. Backend dependencies
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt -r requirements-media.txt -r requirements-document-tables.txt

# 2. Databases (one for the app, one for tests; the test DB name must end in _test)
psql -U postgres -c "CREATE ROLE infraspine_app LOGIN PASSWORD 'choose-a-password';"
psql -U postgres -c "CREATE DATABASE infraspine_ai OWNER infraspine_app;"
psql -U postgres -c "CREATE DATABASE infraspine_ai_test OWNER infraspine_app;"
psql -U postgres -c "CREATE DATABASE infraspine_ai_evalgate OWNER infraspine_app;"  # evaluation gate

# 3. Configuration
cp .env.example .env
python -m app.cli generate-master-key      # paste into MASTER_ENCRYPTION_KEY
# set DATABASE_URL; for local http development also set SESSION_COOKIE_SECURE=false

# 4. Schema and first admin
alembic upgrade head
python -m app.cli create-user --email you@company.com --role admin   # prompts for password (min 8 chars)

# 5. Admin UI
cd frontend && npm ci && npm run build && cd ..

# 6. Run (two terminals, same project folder and .env)
uvicorn app.main:app --reload        # API + UI (one process: it owns the vector index)
python -m app.worker                 # document worker: parses, chunks and embeds uploads
```

### Document worker

Uploaded files are processed by a separate worker process, so parsing never slows down or endangers the API.
The worker reads the same `.env` (database, `RAG_DATA_DIR`, embedding model, `RAG_*` limits).

| Command | Purpose |
|---|---|
| `python -m app.worker` | Run until stopped. Ctrl+C (or SIGTERM / closing the console) lets running documents finish for `RAG_WORKER_SHUTDOWN_GRACE_SECONDS`, then returns the rest to the queue. |
| `python -m app.worker --concurrency 4` | Process up to 4 documents in parallel (default `RAG_WORKER_CONCURRENCY`). |
| `python -m app.worker --once` | Process everything that is due, then exit (e.g. Windows Task Scheduler). |
| `python -m app.worker --check` | Validate configuration and database schema, then exit. |

The worker also runs the hourly training-deadline scheduler. It records one in-app reminder when an assigned
lesson is due within three days and one when it becomes overdue; it sends no email or external notification.
`--once` runs this reminder check once along with due document jobs. Reminders require migration 0024.

Personal assignment deadline reminders are also in-app by default. An operator can optionally enable a separate
SMTP outbox with `PERSONAL_TASK_EMAIL_ENABLED=true`, `PERSONAL_TASK_SMTP_HOST`, `PERSONAL_TASK_EMAIL_FROM`, and
either STARTTLS or implicit TLS. SMTP credentials stay in the protected deployment environment. The worker
retries transient failures with bounded backoff and exposes delivery status only in the assignee's private inbox;
email contains no task title or body. This requires migration 0053. SMS delivery is not included.

- **Scaling:** start more worker processes (on this or other machines sharing the database and the data
  directory). Jobs are claimed with `FOR UPDATE SKIP LOCKED`; each document goes to exactly one worker.
- **Isolation and limits:** each file is parsed in its own child process, killed after
  `RAG_PROCESSING_TIMEOUT_SECONDS` and capped at `RAG_PARSER_MEMORY_MB` (Windows Job Object / POSIX `RLIMIT_AS`).
  Size limits (`RAG_MAX_UPLOAD_MB`, `RAG_MAX_PAGES`, `RAG_MAX_EXTRACTED_CHARS`, `RAG_MAX_UNPACKED_MB`) apply in the worker too.
- **Recovery:** a claimed job holds a lease (`RAG_PROCESSING_LEASE_SECONDS`) that the worker renews while it works.
  If a worker crashes or is killed, the lease runs out and another worker takes the job; after `RAG_MAX_ATTEMPTS`
  interrupted attempts the document fails with `processing_abandoned`. Jobs survive API and worker restarts.
- **Index:** workers write PostgreSQL only. The API process applies new chunks to its FAISS index every
  `RAG_INDEX_SYNC_SECONDS` and holds an OS lock on the data directory, so no second process can write the index.
- **Single-process mode:** `RAG_INGESTION_MODE=embedded` runs the same worker loop inside the API process (the
  previous behaviour; parsing still happens in child processes).
- **Status:** the Knowledge page warns when documents are queued but no worker is running, and queued or
  processing documents can be cancelled. Admins see worker and queue counts at `GET /api/rag/index`.

Open http://127.0.0.1:8000/admin/, sign in, then:

1. **DeepSeek → Model & settings → Save configuration**
2. **API key → paste key → Verify & save key** (verified live with DeepSeek, then stored encrypted as *pending*)
3. **Activate** (the key is verified again, then goes live)

To rotate a key, repeat steps 2–3. The old key keeps serving until the new one is activated, and is deleted afterwards.

To add another API/model without changing the provider currently serving requests, open **AI Providers → Add provider**.
Enter a display name, OpenAI-compatible API base URL, model ID, and API key. **Verify & add inactive** checks the key
and model, then stores the key encrypted as pending. The current provider remains active until you explicitly activate
the new entry. Endpoints must be public HTTPS URLs; private or local endpoints (and plain HTTP to them) work only when
their exact origin is listed in `PROVIDER_TRUSTED_PRIVATE_ORIGINS`. The endpoint must implement OpenAI-compatible
model listing and chat completions. After adding one, its model ID can be edited and is checked with the saved key.
Changing its API base URL requires entering the API key for the new URL: saved keys are never sent to another URL, and
the keys saved for the previous URL are deleted once the new one is verified.

**Knowledge (RAG):** open **Knowledge**, upload a document or image, wait for *Ready*, then ask questions. Audio/video speech transcription is optional and uses a staged local model. Video on-screen text is sampled and OCRed locally; see [on-premises deployment](docs/on-premises-deployment.md). The first start downloads the
embedding model (~90 MB) into `data/models`; after that it loads offline.

**Upgrading an existing deployment:** take a verified database and document-storage backup, then run `alembic upgrade head` to apply migrations in order. Existing accounts and source documents are retained.

**UI development with hot reload:** keep uvicorn running, run `npm run dev` in `frontend/`, and open http://localhost:5173/admin/. Vite proxies `/api` to the backend; this address exists only while that dev server is running. The current synthetic QA candidate serves its built UI from the API origin at http://localhost:18081/admin/.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/db` |
| `MASTER_ENCRYPTION_KEY` | Base64 32-byte AES-256-GCM key. **Never stored in the DB.** Use a secret manager in production. |
| `MASTER_ENCRYPTION_KEY_ID` | Label stored with each ciphertext (for future master-key rotation) |
| `APP_ENV` | `development`, `testing`, `staging` or `production` |
| `LLM_LOCAL_ONLY` | Blocks built-in/cloud model requests, admin provider verification and environment-configured eval calls; permits only exact trusted custom origins resolving privately. Default `false` in development `.env.example`, `true` in `.env.onprem.example`. |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` | Chat request timeout and retries |
| `PROVIDER_VERIFY_TIMEOUT_SECONDS` | Timeout for key verification calls |
| `PROVIDER_TRUSTED_PRIVATE_ORIGINS` | Comma-separated origins (`scheme://host[:port]`) of approved internal custom-provider endpoints; empty = public HTTPS only |
| `SESSION_TTL_MINUTES`, `SESSION_COOKIE_SECURE` | Session lifetime; secure cookies are **required** in production |
| `LOGIN_MAX_FAILED_ATTEMPTS`, `LOGIN_LOCKOUT_MINUTES` | Account lockout policy |

Provider API keys are **not** environment variables. Enter them in the admin UI.

RAG settings (`RAG_*`) are documented in `.env.example`: data folder, upload/page limits, chunk size and overlap,
embedding model, top-k, minimum similarity and worker retries. Retrieval and answer behaviour:

| Variable | Default | Purpose |
|---|---|---|
| `RAG_HYBRID_RETRIEVAL` | `true` | Fuse PostgreSQL full-text matches with vector search |
| `RAG_QUERY_ANALYSIS` | `true` | Typo tolerance from the corpus vocabulary; decide when a follow-up needs context |
| `RAG_MAX_CHUNKS_PER_DOCUMENT` | `3` | At most this many passages of one document while others have eligible ones |
| `RAG_LEXICAL_CANDIDATES` | `50` | Full-text candidates fused per question |
| `RAG_HIERARCHICAL_RETRIEVAL` | `true` | Document discovery and document → passage ranking |
| `RAG_DISCOVERY_MAX_DOCUMENTS` | `3` | Documents a question can name (a tie at the limit is kept whole, up to twice this) |
| `RAG_SMALL_DOCUMENT_CHARS` | `4000` | A chosen document up to this size arrives whole (its header, table and total together) |
| `RAG_DATE_ORDER` | `dmy` | How numeric dates in filenames and headers are read: `dmy` (06.07.2026 = 6 July) or `mdy` |
| `RAG_GENERAL_ANSWERS` | `true` | Answer questions no document covers from general knowledge (labelled); `false` = documents only |
| `CHAT_CONTEXT_TURNS` | `3` | Earlier turns of a saved conversation sent with a question |

## API

| Method | Path | Access |
|---|---|---|
| GET | `/health`, `/health/ready` | public (liveness / DB readiness) |
| POST | `/api/auth/login`, `/api/auth/logout`; GET `/api/auth/me` | — / signed in |
| POST | `/api/onboarding/accept` | public: accept a single-use, expiring invitation and set a password |
| GET, POST | `/api/admin/users`, `/api/admin/users/invitations` | admin: account roster and invite employees/managers; raw invitation token returned once |
| PATCH, POST | `/api/admin/users/{id}`, `/active` | admin: manager/employee department assignments and account activation; deactivation revokes sessions but retains data |
| GET, POST, DELETE | `/api/departments/managed`, `/api/departments/{id}/members` | manager: inspect and change membership only in assigned local departments |
| POST | `/api/ai/chat` | any signed-in user |
| GET | `/api/admin/providers/catalog` | admin: provider/model/settings metadata |
| GET | `/api/admin/providers`, `/api/admin/providers/{id}` | admin: state (never secrets) |
| POST | `/api/admin/providers/custom` | admin: verify and add an inactive OpenAI-compatible provider/model |
| PUT | `/api/admin/providers/{id}` | admin: model/settings; custom provider URL |
| POST | `/api/admin/providers/{id}/test` | admin: live connection test (key not stored) |
| POST | `/api/admin/providers/{id}/credentials` | admin: verify and stage a key |
| POST | `/api/admin/providers/{id}/activate`, `/deactivate` | admin |
| GET | `/api/admin/audit-logs` | admin |
| GET, POST | `/api/rag/documents` | signed in: list readable documents / upload a supported file; admins can share or restrict by department, managers can upload only to managed departments |
| GET, DELETE | `/api/rag/documents/{id}` | readers (others get 404); delete: owner, or admin for shared/restricted |
| PUT | `/api/rag/documents/{id}/access` | `private` / `shared` / `restricted` (users, groups); managers can change ACLs only within their managed departments |
| PUT | `/api/rag/documents/{id}/file` | replace the file (previous version stays searchable until the new one is ready) |
| POST | `/api/rag/documents/{id}/reprocess` | retry a failed document |
| POST | `/api/rag/documents/{id}/cancel` | stop processing a queued or processing document (owner, or admin for shared) |
| POST | `/api/rag/chat` | signed in: grounded answer with `[S1]` citations (document + page) |
| POST | `/api/rag/research` | signed in: bounded query planner plus independent ACL-filtered, citation-checked evidence findings; general knowledge is disabled |
| POST | `/api/rag/training` | signed in: source-backed learning objectives, procedures and knowledge check; general knowledge is disabled; requires the `training` license feature when enforcement is enabled |
| GET | `/api/rag/market-research/status` | signed in: reports whether opt-in Gemini public web research is configured |
| POST | `/api/rag/market-research` | signed in: source-cited public market research; requires explicit external-search consent and the `market_research` license feature when enforcement is enabled |
| GET, POST | `/api/rag/index`, `/api/rag/index/rebuild` | admin: vector index status / rebuild from PostgreSQL |
| GET, POST, DELETE | `/api/memory`, `/api/memory/{id}` (PATCH edits) | signed in: own personal memories only; `DELETE /api/memory?all=true` forgets all |
| GET, POST | `/api/claims`, `/api/claims/{id}`, `/api/claims/{id}/withdraw` | signed in: submit, view and withdraw own unverified claims; never published into knowledge |
| GET, POST | `/api/governance/claims`, `/api/governance/claims/{id}/decisions` | users with scoped review/publish grants: append-only, two-person claim decisions; claims stay out of RAG until curated |
| GET, POST | `/api/handoffs` | signed in: submit/view durable process notes; author and assigned department managers only, with acknowledgement retained |
| GET, POST | `/api/knowledge`, `/api/knowledge/{id}`, `/api/knowledge/{id}/revisions`, `/api/knowledge/{id}/withdraw` | signed in: own unpublished knowledge drafts and their source passages; not searchable |
| GET, POST, DELETE | `/api/admin/access/groups[/{id}[/members[/{user_id}]]]` | admin: groups and local membership |
| GET, POST | `/api/admin/access/grants`, `/grants/{id}/revoke` | admin: reviewer/publisher/steward grants (never to self) |
| GET, POST | `/api/admin/role-templates`, `/role-template-assignments` | admin: immutable, department-scoped review/publish templates and active assignments |
| PATCH, POST | `/api/admin/role-templates/{id}/active`, `/role-templates/{id}/assignments`, `/role-template-assignments/{id}/revoke` | admin: stop new assignments, assign to a matching role and member departments, or revoke a whole assignment |
| GET | `/api/admin/access/quarantine` | admin: synced documents with an unusable source ACL (IDs and reasons only) |
| GET | `/api/admin/reports/usage` | admin: aggregate daily volume, reliability, feedback, tokens and latency; no question or answer text |
| GET, POST | `/api/service-requests` | signed in: submit and list only requests allowed by requester, HR privacy and department-manager scope |
| GET, POST | `/api/service-requests/{id}` and `/comments`, `/triage`, `/cancel` | requester history; Admin or assigned department manager triage; HR details are private to requester/Admin |

**Roles and onboarding:** administrators create departments, invite employees or managers, assign department memberships, and grant scoped review/publish rights from **People & access**. Admins can also create immutable permission templates for existing Managers or Employees. Template assignments apply only to departments already assigned to that person and are revoked when their role, department set, included department membership, or active status changes. Templates support knowledge review and publication only; arbitrary action permissions, successor versions, time-limited delegation and approvals for sensitive permission changes remain incomplete. See [roles and permissions](docs/roles-and-permissions.md). Invitations are single-use, expire after 72 hours, and are stored as token hashes. The app does not email invitations; copy the returned link into an approved company channel. Managers can manage membership and restricted documents only in departments assigned to them. Legacy `user` accounts remain valid and are displayed as Employees.

**Employee knowledge preservation:** employees can write a department handoff with process steps, dependencies and open work. The record is not indexed or used in AI answers. Department managers can acknowledge it; deactivating an account revokes sessions without deleting its documents, conversations, memories or handoffs.

**Employee support:** the **Help & requests** page records IT, access, facilities, HR, and other internal requests with an immutable history. HR requests are readable only by the requester and administrators; request details and comments are not copied into the general audit log. See [employee service requests](docs/employee-service-requests.md).

**Approvals:** review and publication decisions use the existing append-only approval table and scoped grants. The claim author cannot approve their own claim; review and publication require different people. A publication approval remains a governance record and does not make an uncurated claim searchable.

**Research and training agents:** Internal research plans up to three evidence questions; employee training creates learning objectives, documented procedures, and a short-answer knowledge check. Both use the PostgreSQL ACL-filtered RAG path, disable general-knowledge fallback, and return validated citations. Optional public market research uses Gemini's native Google Search grounding, is off by default (`RAG_EXTERNAL_RESEARCH_ENABLED=false`), and requires separate user confirmation before sending the entered topic to Google. It sends no company profile or internal document content, bounds results to 20 validated HTTPS citations and a 4,096-token model output, and requires the `market_research` license feature when license enforcement is enabled. Each consented request is audited before transmission using provider/model identifiers and topic length; the topic itself is not written to the audit record. File ingestion remains in the durable worker queue with leases, retries, cancellation and isolated parsing. Provider endpoints retain the existing HTTPS/SSRF policy; state changes require CSRF protection, and security actions are audited without storing document content or invitation tokens.

**Reporting:** Usage reports summarize metadata-only traces and feedback and can be printed or exported as CSV from the admin UI. The report endpoint is admin-only.

All mutations require the header `X-Requested-With` (CSRF defence) and `expected_version` (optimistic concurrency; a stale version returns **409**).

## Project layout

```
app/
  main.py                 App factory, lifespan (DB engine, cipher, LLM runtime), middleware
  cli.py                  generate-master-key, create-user
  core/config.py          Typed settings + safety validators
  core/crypto.py          AES-256-GCM credential encryption bound to its DB row
  core/security.py        Argon2id passwords, session token hashing
  core/errors.py          Domain errors -> consistent JSON responses
  db/models.py            Tables; invariants enforced with DB constraints
  providers/registry.py   Provider catalog (models, settings schema, adapter)
  providers/openai_compatible.py  DeepSeek adapter (chat + live verification)
  providers/endpoint_security.py  Custom-endpoint URL normalization, SSRF policy, origin-bound HTTP client
  services/llm.py         Provider-independent LLMClient interface
  services/llm_runtime.py Per-request resolution of the active provider (no restart)
  services/provider_admin.py  Configure / stage / activate / rotate lifecycle
  services/auth.py, audit.py
  rag/access.py           Who may read/modify which document (single source of truth, evaluated in SQL)
  services/access_control.py  Groups, grants, review/publish decisions (separation of duties)
  services/users.py           Invitations, role and department assignments, account deactivation
  services/handoffs.py        Durable department-scoped employee knowledge preservation
  services/memory.py      Personal memory: chat commands, supersession, bounded prompt context
  services/claims.py      Unverified organizational claims (never published into knowledge)
  services/source_acl.py  File-server ACL mapping; unknown/unsupported ACLs quarantine the document
  rag/storage.py          Private file storage (random names, size/signature checks)
  rag/parsers/             Parser registry: content-based format detection, one parser per format -> Sections + Locators
  rag/pdf.py, chunking.py pypdf page text extraction, pypdfium2 page images; chunking with overlap
  rag/embeddings.py       Local CPU embeddings (sentence-transformers)
  rag/vector_store.py     VectorStore interface + FAISS implementation (atomic, checksummed persistence)
  rag/qdrant_store.py     Optional cold/on-disk Qdrant provider with resumable rebuild aliases
  rag/ingestion.py        Worker loop (DB job queue, leases, retries, cancellation) + PostgreSQL→FAISS sync/reconcile
  rag/isolation.py        Parser child processes with time and memory limits
  worker.py               `python -m app.worker`: the document worker process
  rag/documents.py        Upload / replace / reprocess / delete
  rag/retrieval.py        Permission-filtered hybrid search; documents ranked before passages; search_options()
  rag/discovery.py        Document descriptors (filename words, dates, title, headings) and document resolution
  rag/query_analysis.py   Question terms, IDF weights, typo corrections, full-text candidates
  rag/query_expansion.py  Urdu / Roman Urdu query vocabulary for the English embedding model
  rag/chat.py             Routing: overview, tables, charts, page images, follow-ups, safe fallback
  rag/answer.py           Grounded prompt, citation parsing, insufficient-evidence and general-answer handling
  rag/sufficiency.py      Evidence checks on cited answers; credential redaction
  rag/numbers.py          Locale-aware numbers and amounts ("Rs. 18,450/-", "PKR 1,25,000")
  rag/tables.py           Exact calculations over spreadsheet and stored document tables
  rag/tracing.py          Per-question traces (metadata only)
  rag/evaluation/         Retrieval/answer metrics, golden and workload corpora, evaluation gate, reports, CLI
  api/deps.py             DB session, auth, CSRF, admin RBAC
  api/routes/             health, auth, onboarding, users, departments, governance, handoffs, service requests, reports, rag, conversations
migrations/               Alembic through 0027 employee service requests (earlier migrations cover providers, RAG, ACLs, claims, drafts, workers, workflows and agents)
evals/                    Golden and enterprise-workload cases, runners, run summaries, reports/
docs/                     Design notes (retrieval, discovery, conversation context, evidence validation)
data/                     Runtime only, git-ignored: uploads/, vector_index/, models/
frontend/                 React + TypeScript admin UI (Vite)
tests/                    pytest against a real PostgreSQL test DB; providers faked
```

**Adding a native provider integration:** an API that is not OpenAI-compatible needs an adapter class implementing `LLMClient` (`chat`, `verify`, `close`) and a `ProviderDefinition` in `providers/registry.py`. OpenAI-compatible endpoints can be added directly from the admin UI.

## Retrieval evaluation

The deployed FAISS index and its embedding model (MiniLM by default) can be measured without
replacing either component. Golden cases are JSON files with a question, expected document,
expected passage, language (`en`, `ur` or `roman_ur`) and a permission persona. Account emails
live in a separate persona map. Both files, and the JSON and text reports, stay under
`data/evaluation/` for this deployment and are never served over HTTP.

```bash
python -m app.rag.evaluation --dataset billing.json --personas local.json --k 1 --k 5 --k 10
```

Stop the API process first if it is running, so the report reads a settled FAISS index.

`billing.json` is read from `data/evaluation/datasets/` and `local.json` from `data/evaluation/personas/`.
Each case is run through `retrieve()`: PostgreSQL decides which chunks the persona may read, then
FAISS searches only those ids, using the deployment's similarity floor and context budget.
Recall@K, MRR, nDCG, citation precision, correct abstention and retrieval latency are written to
`data/evaluation/reports/`. The report fingerprint covers the ranking metrics and the dataset
hash, so a repeated run against an unchanged index produces the same fingerprint.

## Evaluation gate

`python -m app.rag.evaluation.gate` fails (exit 1) when quality drops below `evals/gate.json`. Every run
rebuilds the `*_evalgate` database, indexes the synthetic Harborline corpus (`app/rag/evaluation/golden.py`:
every format, English, Urdu and Roman Urdu, near misses, a withdrawn draft, private and group-restricted
documents, a multi-passage memo) with the production parsers and embedder, and runs `evals/golden.json`.

```bash
python -m app.rag.evaluation.gate                           # retrieval + permission leaks; no LLM, deterministic
python -m app.rag.evaluation.gate --answers                 # + production answers with the active provider
python -m app.rag.evaluation.gate --answers --provider env  # provider from EVAL_LLM_PROVIDER/MODEL/API_KEY
```

The retrieval gate checks recall@1/@5, nDCG@5, MRR, Urdu and Roman Urdu recall, retrieval latency, and that
no persona retrieves a document it cannot read. It measures the retrieval chat uses (`retrieval.search_options`:
hybrid search, query analysis, document discovery); semantic search alone, the configuration it measured before,
is reported beside it for comparison and is not gated. The answer gate adds correctness, abstention, groundedness,
citation accuracy, unsupported claims, permission isolation and false refusals. Reports go to `evals/reports/`.
A bug that reached users becomes a case in `evals/golden.json` (follow-ups use `history`); thresholds move
up when a change improves the baseline, never down to make a run pass.

### Enterprise workload baseline

`evals/run_enterprise_workloads.py` measures what the golden gate does not: documents found by a filename, month,
date or site that appears only in the filename; answers inside one table of a small document; follow-ups and topic
switches (history and focus documents rebuilt as a saved conversation builds them); Urdu and Roman Urdu questions
about documents in another language; questions that need two documents; superseded values, near misses and general
questions; restricted and private documents; and a 2,000-row repetitive log beside a short policy. The 55 cases in
`evals/enterprise_workloads.json` run through production `run_chat` with this deployment's settings over the
Harborline corpus plus the synthetic Northwind Traders corpus (`app/rag/evaluation/workloads.py`).

```bash
python evals/run_enterprise_workloads.py              # retrieval: passages handed to the model, no LLM
python evals/run_enterprise_workloads.py --answers    # + answers with the active provider (synthetic data only)
```

Retrieval metrics: document hit rate, evidence hit rate (the answer-bearing passage arrived), document-without-
evidence rate, history contamination on topic switches, repetitive-document share and ACL leaks. Answer metrics: pass
rate, answer correctness, refusal accuracy, general answers to document questions, history leaks and the failing
stage (retrieval, validation, generation, fallback, permission), read from each answer's trace decision. The report
also compares the golden gate's retrieval call with production's. It is a baseline, not a gate: no thresholds yet.
Reports go to `evals/reports/enterprise-workloads-*.json`.

Answers vary between runs of the same model. `python evals/summarize_runs.py --workloads <reports> --gate <reports>`
prints the median and range of each metric across repeated runs, with false refusals, unsupported claims and security
failures separately, and how often each case failed (every run: a defect; one run: variance).

CI (`.github/workflows/ci.yml`) runs the tests, the frontend build and the retrieval gate on every push and
pull request, and the answer gate nightly or on demand with the `EVAL_LLM_*` repository secrets.

## Observability

Each knowledge-chat question writes one trace (`rag_traces`): strategy, retrieved chunk IDs with scores and
which were cited, the evidence decision (for example `refused_verifier` or `general_after_unsupported`),
stage timings, LLM calls and tokens, and for a failure the exception type and stack frames. No question,
answer, prompt, passage or exception message is stored, so admins can diagnose answers without reading
conversations. Admins see them on the **Traces** page with error and refusal rates, p50/p95 latency and the
👍/👎 feedback users give on answers. Traces expire after `RAG_TRACE_RETENTION_DAYS`.

An unexpected error returns its request ID to the browser ("Ref 1a2b3c4d"); the same ID is on the trace and
on the full traceback in `logs/infraspine.log`.

## Tests

```bash
pytest                           # needs the *_test database (TEST_DATABASE_URL to override)
cd frontend && npm run build     # type-check + production build
```

For explicit Windows environment setup and a database guard that rejects the configured application database,
see [`docs/windows-development.md`](docs/windows-development.md).

## RAG architecture

```
Upload ─► validate (size; format detected from content and matching the extension; not encrypted; ZIP limits)
       ─► data/uploads/<random>.<ext> ─► documents row (pending, file_format)
Worker ─► claim job (FOR UPDATE SKIP LOCKED + lease) ─► parser: sections + locators ─► chunks ─► local embeddings
       ─► one PostgreSQL transaction (old chunks out, new chunks + embedding bytes, tables, document descriptor in)
       ─► then FAISS update
Chat   ─► resolve the question: documents it names (descriptors), or the conversation's tracked documents
       ─► PostgreSQL: chunks this user may read ─► FAISS + full-text candidates, fused by rank
       ─► documents ranked first (name match + best passage); small chosen documents whole, large ones' best passages
       ─► re-check + load text from PostgreSQL ─► active LLM ─► evidence check on every cited claim ─► answer + citations
```

- **PostgreSQL is the source of truth** (metadata, permissions, chunk text, embedding bytes). The FAISS index is
  derived: if it is missing, corrupt or stale it is reconciled or rebuilt from PostgreSQL, without re-reading PDFs.
- **Authorization before retrieval:** the vector search only considers chunks the user may read; content is loaded
  with the same filter again. Private = owner only; shared = all users; restricted = the owner plus the users and
  groups in `resource_acl`. Only admins can share. Synced (file-server) documents are readable only through their
  synced ACL, and an unknown or unsupported source ACL quarantines the document for everyone, admins included.
- **No authorization cache:** the read rule is a SQL condition inside every query, so removing a group member,
  narrowing an ACL or quarantining a document applies on the next request. A saved answer that used a document the
  user can no longer read is withheld (text, chart, snippets, images) and never reused as chat context.
- **Grounding:** the LLM receives only the retrieved chunks, must cite `[S<n>]`, and must answer
  `INSUFFICIENT_EVIDENCE` when the sources don't cover the question. Uncited answers are withheld, and so is a cited
  reply that only says the sources do not state the fact (it is insufficient evidence, not a verified answer). Every
  number, code and amount must come from the cited text; a document's filename and section tell what a passage is
  about but never supply a value. Source text is treated as untrusted (prompt-injection rule).
- **Document discovery:** each document has a descriptor built at ingestion (filename split into words, dates
  written out, title, headings, sheet, slide and table titles). A question naming "the July internet bills", "the
  15 September minutes" or "the Lahore DC audit" finds the document even when that name is only in its filename.
  Descriptors are matched inside the user's ACL scope, are never sent to the model, and are rebuilt from stored chunks
  when the builder changes.
- **Conversations:** each saved answer records the documents its question named and the ones it cited. A follow-up
  ("aur August ka?", "this file") is resolved to those documents; it borrows an earlier question only when it has no
  topic of its own, and only from the turn it refers to. A question naming documents, or resolved to the
  conversation's, is never answered from general knowledge, and general answers never see earlier document answers.
- **Credentials:** passwords, door and access codes, PINs, keys and tokens are replaced before any text reaches the
  model and are never shared; business codes (ticket, rate, reference) are.
- **Replacement/deletion:** a replacement keeps the old version searchable until the new one is indexed (a failed
  replacement keeps the old version). Deletion removes the row, chunks, vectors and file. A job whose document was
  replaced or deleted mid-processing discards its result.
- **Personal memory:** "remember that …", "forget …" and "what do you remember?" are handled in chat without the LLM,
  or on the Memory page. Memories are owner-only, never indexed, never cited; one active item per content and per
  subject (new values supersede old ones), at most 100 per user, and at most 2,000 characters / 20 items per question
  (preferences first, then the most relevant).
- **Unverified claims:** users can submit proposed organizational knowledge or corrections. Claims record the
  claimant, statement, scope, sources and status. They are owner-only until withdrawn, never indexed, never cited
  and never treated as approved facts. Review and publish are not implemented yet.
- **Draft organizational knowledge:** an author can record an unpublished statement pinned to an exact indexed
  passage (quote, prefix, suffix, offsets, document revision). A new file version, deletion or passage removal
  orphans that evidence. Drafts are never embedded; a PostgreSQL outbox records the index mutation and the
  consumer leaves the derived knowledge index empty until a later publication workflow.
- **Privacy:** ingestion never calls an external LLM; logs contain IDs and counts only, never content or questions.

## Security model

- **Encryption at rest:** AES-256-GCM with a random nonce. The row ID and provider are bound in as associated data, so tampered or row-swapped ciphertext fails to decrypt. The master key lives only in the environment or a secret manager.
- **No unverified credentials:** a key is written only after a live provider call succeeds, and is verified again just before activation. DB constraints guarantee at most one active provider, one active key and one pending key per provider.
- **Safe rotation:** a failed verification leaves the active key untouched. Retired keys have their ciphertext deleted.
- **Concurrency:** optimistic `version` checks, plus row locks taken after the (unlocked) network verification. Concurrent edits get 409 instead of lost updates.
- **Endpoint-bound custom keys:** a custom provider's key is encrypted with its verified endpoint origin as associated
  data, so it decrypts only for that origin. A new API base URL requires a new key (test included); keys for the old URL
  are shredded. Keys saved before this binding are re-encrypted to their saved URL's origin on startup.
- **Outbound policy for custom endpoints:** HTTPS only; private, loopback, link-local/metadata, multicast and reserved
  addresses are refused (private ones only for listed trusted origins). Every connection re-resolves the host, checks
  all answers and connects to the checked address (DNS rebinding); redirects are never followed, requests to any other
  origin are refused, and environment proxies are ignored. URL changes and rejections are audited by origin only.
- **Secrets never leave the server:** no response schema contains a key (only the last 4 characters). Logs carry only codes and metadata, SQL parameters are hidden from errors, and validation errors don't echo input.
- **Authentication:** server-side sessions (only a SHA-256 of the token is stored) in an `HttpOnly`, `SameSite=Strict` cookie, plus a custom-header CSRF check and account lockout. Admin-only RBAC on every configuration endpoint.
- **Audit:** logins and every provider action (including failures) are recorded. A DB trigger blocks UPDATE/DELETE on `audit_logs`.
- **Separation of duties (PostgreSQL-enforced):** no self-grants and no self-approval (single-row CHECKs), one person per
  item and one approval per stage (UNIQUE), a decision can only use the approver's own grant (composite FK), and
  triggers check at insert that the grantor is an active admin and that the grant is active and in scope; grants can
  only be revoked once and decisions are append-only.

### Known gaps before production

- Login throttling is per account; add IP-based rate limiting at the gateway.
- Behind a reverse proxy, configure trusted `X-Forwarded-For` handling for accurate audit IPs.
- Master-key rotation (re-encrypting existing rows) is not implemented yet; `key_id` is stored to support it.
- Expired sessions are cleaned up only on the next login; add a periodic cleanup job.
- The `/api/ai/chat` endpoint is cookie-authenticated; service-to-service API tokens are not implemented.
- Custom-provider traffic ignores `HTTPS_PROXY`/`HTTP_PROXY` (a proxy would resolve names itself and bypass the address
  checks). Deployments that require an outbound proxy for custom endpoints are not supported yet.
- The application role owns the schema, so it could disable the separation-of-duties triggers. Production needs a
  separate migration-owner role; the app role then gets INSERT/SELECT on decisions and only the revocation UPDATE on grants (DBA step).

### Table analytics

Questions that need every row counted ("which report did she open most?", "kis din sab se zyada logs the?")
cannot be answered from a few retrieved passages. For an XLSX or CSV document, or a PDF or DOCX whose tables were
stored at ingestion (the one picked, one the question names, the one the conversation is about, or the closest
match), `app/rag/tables.py` finds the tables, the LLM writes one JSON query (group by, count/sum/mean/min/max,
filters, top N) that is validated against the real columns, and Python computes it over all rows. Amounts are read
in Western and South Asian forms ("Rs. 18,450/-", "PKR 1,25,000", "(1,200)"); a malformed value is not counted. The
result, with each group's share of the total, is the answer's only cited source. If the model's wording of it fails
the evidence check, the computed table itself is shown. The LLM never runs code.

### RAG limitations

- **Single API process:** the FAISS index lives in one API process (`--workers 1`; a second process on the same data
  directory refuses to start). Document workers can scale out; multiple API processes/servers need a shared vector
  store (Qdrant/pgvector) via the `VectorStore` interface.
- **Worker limits:** parser timeouts and memory caps apply to an isolated child process. Browser uploads use
  `RAG_MAX_UPLOAD_MB`; folder imports use `RAG_IMPORT_MAX_FILE_MB` (audio/video also obey
  `RAG_MEDIA_MAX_FILE_MB` and `RAG_MEDIA_MAX_DURATION_SECONDS`). Embedding runs in the worker process itself,
  bounded by `RAG_MAX_EXTRACTED_CHARS`, and is not time-limited. Custom deployments without a shared data directory
  need one (workers read the uploaded files).
- **Scale:** exact FAISS search keeps all float32 vectors in API memory. A 600 GB source corpus has not been benchmarked or capacity-qualified; use a disk-oriented index before that rollout.
- **Formats:** PDF, DOCX, XLSX, PPTX, TXT, CSV, PNG/JPEG images, and optional audio/video speech transcription with video-frame OCR.
  Image descriptions use the LLM provider only when an admin allows it. Audio/video transcription runs locally with
  an explicitly staged model; video frames are sampled and OCRed locally under configured interval and count limits. The format is detected from the file's content and must
  match its extension. Scanned PDF pages use local RapidOCR through ONNX Runtime when no selectable text is present. Legacy Office (.doc/.xls/.ppt),
  macro-enabled, password-protected and other files are rejected with an explicit reason.
- **Citations:** each chunk stores a structured `locator` (PDF page, Word heading path, sheet + cell range, slide,
  TXT line range, CSV row range, audio transcript time range, or sampled video-frame timestamp); citations return it with a readable `location`. Page images exist for PDFs, and for Word, PowerPoint and Excel files when LibreOffice is installed (`RAG_OFFICE_CONVERTER`; converted to PDF once and cached).
- **Spreadsheets:** values are read as last calculated by Excel (formulas are not evaluated). Rows are grouped per
  sheet with their column names and cell references.
- **Untrusted files:** parsing is isolated in a child process by default, with timeout and memory limits. Office files
  are checked first (part count, unpacked size via `RAG_MAX_UNPACKED_MB`, no encrypted parts, no XML DTDs/entities).
- **Licences:** all parsing components are permissive and free for commercial use (pypdf BSD-3-Clause,
  pypdfium2/PDFium Apache-2.0/BSD-3-Clause, python-docx/openpyxl/python-pptx MIT, lxml BSD-3-Clause,
  Pillow MIT-CMU, XlsxWriter BSD-2-Clause, et-xmlfile MIT).
- **Languages:** the default embedding model (MiniLM) is English-centric. Roman Urdu and Urdu questions work through
  a query vocabulary and full-text matching; an Urdu-script question about an English document (or the reverse) is
  often not found. A multilingual model needs a full re-index.
- **Conversations:** follow-ups are resolved through saved source state; histories sent by stateless
  `/api/rag/chat` clients carry none and borrow the previous question as before. Only the last `CHAT_CONTEXT_TURNS`
  turns are used.
- **Dates in names:** numeric dates are read with `RAG_DATE_ORDER` for the whole deployment; ISO dates and dates
  whose day is above 12 are always unambiguous.
- Retrieval quality depends on the similarity threshold (`RAG_MIN_SIMILARITY`).
- The upload limit is enforced via `Content-Length` in the app; also set a body-size limit at the reverse proxy.
- Rebuild the index with `POST /api/rag/index/rebuild` or `python -m app.cli rag-rebuild-index` (stop the server first).

## Technology stack, what is not used, and adapting it

This section is for anyone reusing InfraSpine AI for another organization or another system: what it is built on,
which common technologies it deliberately does not use (and when you would add them), its limitations, and where to
change it. It describes a local (on-premises) deployment: the API, worker, PostgreSQL and data directory run on the
organization's own machines, and the only outside call is to the LLM provider an admin configures.

### Stack in use

| Layer | Used | Notes |
|---|---|---|
| API | Python 3.12+, FastAPI, Uvicorn, Pydantic v2 | One API process owns the vector index (`--workers 1`) |
| Database | PostgreSQL 14+ (17 in development), SQLAlchemy 2 async + asyncpg, Alembic | Source of truth: metadata, ACLs, chunk text, embedding bytes, tables, descriptors, traces; `pg_trgm` and full-text search (`tsvector`, `simple` configuration) |
| Job queue | PostgreSQL (`FOR UPDATE SKIP LOCKED` + leases) | Document worker: `python -m app.worker` |
| Vector search | FAISS by default; Qdrant is optional (`RAG_VECTOR_STORE=qdrant`) | PostgreSQL remains authoritative; Qdrant stores cold/on-disk vectors and payloads, FAISS remains the rollback provider |
| Embeddings | sentence-transformers `all-MiniLM-L6-v2` on CPU (PyTorch) | 384 dimensions, local, ~90 MB; no document leaves the server to be embedded |
| Lexical search | PostgreSQL full-text + corpus vocabulary (`rag_terms`) | Fused with vector search by reciprocal rank |
| Parsing | Docling (PDF, DOCX, PPTX), pypdf, pypdfium2, python-docx, openpyxl, python-pptx | Each file in an isolated child process with time and memory limits |
| OCR | Local RapidOCR through ONNX Runtime | Full-page OCR for scanned PDFs; model files are pre-staged and downloads are disabled during parsing |
| Images | PNG and JPEG documents, pictures in PDFs | Described by the active LLM provider only when an admin allows it |
| Page images | pypdfium2; LibreOffice (optional) for Office files | `RAG_OFFICE_CONVERTER` |
| LLM | OpenAI-compatible chat API via the `openai` SDK: DeepSeek built in, any OpenAI-compatible endpoint added in the UI | Answers, evidence verification, table queries; provider and model chosen at runtime |
| Security | AES-256-GCM (`cryptography`) for provider keys, Argon2id passwords, server-side sessions | Master key from the environment only |
| Frontend | React 19, TypeScript, Vite | Served by the API at `/admin/` |
| Tests and evaluation | pytest against real PostgreSQL; golden gate and enterprise workload suites | `evals/`, CI in `.github/workflows/ci.yml` |
| Observability | PostgreSQL traces (`rag_traces`), log file `logs/infraspine.log` | Metadata only: no question, answer or document text |

### Not used in this system

Each was left out on purpose for the current deployment size and requirements. The last two columns say when it
becomes worth adding and where it plugs in.

| Not used | Used instead | Add it when | Where it plugs in |
|---|---|---|---|
| pgvector, Milvus, Weaviate, Elasticsearch | FAISS default; Qdrant server is an optional disk-oriented provider | Larger corpora or separate vector-service operations | `VectorStore` interface; select `RAG_VECTOR_STORE` explicitly |
| Multilingual embedding model (bge-m3, multilingual-e5) | MiniLM + Urdu / Roman Urdu query vocabulary | Users write in Urdu script about English documents (or the reverse); measured gain in `data/bge-m3-eval/reports/comparison.json` | `RAG_EMBEDDING_MODEL`, `RAG_EMBEDDING_DIMENSION`; needs a full re-index and ~2 GB more RAM |
| Cross-encoder reranker | Reciprocal-rank fusion, per-document cap, document ranking | Large libraries where many passages tie | After fusion in `retrieve()` (`app/rag/retrieval.py`); a local model keeps data on the server |
| LLM query rewriting, HyDE, LLM-written chunk context | Deterministic query analysis, descriptors, conversation source state | Only if measured gains outweigh an extra LLM call per question and sending more text to the provider | `app/rag/chat.py` |
| Orchestration frameworks (LangChain, LlamaIndex, Haystack) | Plain Python modules | Not needed: every stage is explicit and tested | — |
| GraphRAG, knowledge graphs, agentic multi-hop | Hierarchical document → passage retrieval | Questions that chain facts across many documents | — |
| Message broker (Celery, RabbitMQ, Redis, Kafka) | PostgreSQL job queue | Very high upload volume or many worker sites | `app/rag/ingestion.py` |
| Cache (Redis) | None: ACLs are evaluated in SQL on every request | Measured latency problems; never cache authorization | — |
| Single sign-on (OIDC, SAML), user directory sync | Local accounts and server-side sessions | An enterprise login policy | `app/services/auth.py`, `app/api/deps.py` |
| Multi-tenancy in one database | One deployment (database + data directory) per customer | Hosting many customers on shared infrastructure | A tenant key on every table and in `access.readable_by` |
| Streaming answers (SSE) | The complete answer after the evidence check | Long answers where waiting is a problem | An answer is checked before it is shown, so only checked text may stream |
| Metrics stack (OpenTelemetry, Prometheus, Grafana) | PostgreSQL traces + the Traces page | Central monitoring | `app/rag/tracing.py` |

### Limitations of this system

The details are in **RAG limitations** and **Known gaps before production** above. In short:

- **Scale:** one API process owns an exact FAISS index. A 600 GB source corpus is outside the validated capacity of this backend; see `docs/on-premises-deployment.md`.
- **Languages:** English-centric embeddings; Urdu-script questions about English documents are often missed.
- **Dates:** numeric dates in filenames are read with one deployment-wide order (`RAG_DATE_ORDER`).
- **Answers depend on the LLM:** the same question can pass in one run and be refused in another; judge a change on
  repeated runs (`evals/summarize_runs.py`), not on one.
- **Evidence checks are rules plus an LLM verifier:** a cited reply that says a fact is absent, worded like a stated
  fact ("the register does not list X"), is left to the verifier.
- **Golden gate:** with production retrieval, recall@1, nDCG@5 and MRR are just under their thresholds; three
  door-code cases expect a value the credential policy withholds by design.
- **Before production:** IP rate limiting, master-key rotation, a separate migration-owner database role, outbound
  proxy support for custom providers (see **Known gaps before production**).

Run the reproducible vector-only sample with temporary synthetic data; it does not touch the configured database or RAG files:

```bash
python scripts/benchmark_vector_store.py --vectors 100000 --dimension 384 --queries 50
```

The report includes exact-index build time, full and 10%-scope search latency, serialized index size and process RSS. It characterizes only the local machine and is not a 600 GB capacity claim.

### Adapting to another system

| You need | Change | Effort |
|---|---|---|
| Another LLM provider | Add it in **AI Providers** if it is OpenAI-compatible; otherwise an `LLMClient` adapter + a `ProviderDefinition` in `app/providers/registry.py` | Small |
| Another language | A multilingual embedding model (re-index); review `app/rag/query_expansion.py` and the Roman Urdu stopwords in `app/rag/query_analysis.py` | Medium |
| Another date convention | `RAG_DATE_ORDER=mdy` | Setting |
| More text per answer | `RAG_MAX_CONTEXT_CHARS`, `RAG_SMALL_DOCUMENT_CHARS` | Setting; more tokens per question |
| Documents only, never general knowledge | `RAG_GENERAL_ANSWERS=false` | Setting |
| More than ~1M chunks or several API servers | A `VectorStore` implementation on pgvector or Qdrant | Medium; rebuild the index from PostgreSQL |
| Another file format | A parser in `app/rag/parsers/`, registered in `registry.py` (it returns sections + locators) | Small to medium |
| Company login | OIDC / SAML instead of password login | Medium |
| An evaluation on your own documents | Cases in the format of `evals/enterprise_workloads.json` with your corpus; keep real customer data out of the repository | Small |

Changes to retrieval, answers or evidence checks should be measured on both suites (golden gate and enterprise
workloads) over at least three answer runs, before and after.
