# Agent 1 — Setup + Baseline Report (InfraSpine, step-e-gaps-wip)

- Worktree: `~/workspace/infraspine/work/setup-baseline`
- Branch: `agent/setup-baseline`, based on `step-e-gaps-wip` (`2fb387d`)
- Date: 2026-11-27 (UTC). No code changes were made — setup and baseline only.
- Full baseline log: `/home/agent1/agent1-baseline2.log` (751 lines)
- First (failed) baseline log: `/home/agent1/agent1-baseline.log` (42,462 lines)

## 1. Baseline result (WIP branch `2fb387d`)

Command (run as user `agent1`, see §3 for why):

```bash
su - agent1 -s /bin/bash -c 'export DATABASE_URL="postgresql+asyncpg://infraspine_agent1:<password>@127.0.0.1:5432/infraspine_ai" PYTHONDONTWRITEBYTECODE=1 TMPDIR=/home/agent1/pg-tmp; cd /home/hatch/workspace/infraspine/work/setup-baseline && .venv/bin/python scripts/irfanix_postgres_tests.py --pg-bin /usr/lib/postgresql/16/bin -- tests -q -rfEs'
```

**Result: 15 failed, 1330 passed, 14 skipped, 326 warnings in 2952.90s (0:49:12). Exit code 1.**

Expected on `main` (4984f7a) per the handover: 1347 passed, 12 skipped, 0 failed.
This WIP branch collects 1359 tests (1330 + 15 + 14). Because failures are > 5,
no code work was done by this agent; the failures are reported below for the
gap agents / master agent to triage.

### The 15 failing tests

| # | Test | Cause (see §4) |
|---|---|---|
| 1 | `tests/test_docling_ingestion.py::test_worker_extracts_pdf_pages_with_docling` | HuggingFace unreachable — Docling layout model download fails, document ends `failed` not `ready` |
| 2 | `tests/test_docling_ingestion.py::test_pdf_chart_is_described_indexed_and_cited_by_page_under_its_acl` | Same — DoclingFailed, 0 image calls |
| 3 | `tests/test_docling_ingestion.py::test_pictures_are_sent_only_with_authorization_to_a_vision_model` | Same — DoclingFailed, 0 image calls |
| 4 | `tests/test_document_tables.py::test_crm_leads_pdf_is_stored_chunked_and_counted_exactly` | Docling failed → 0 tables extracted (`ValueError: not enough values to unpack`) |
| 5 | `tests/test_document_tables.py::test_a_count_that_is_not_computed_is_never_read_off_the_retrieved_rows` | Docling failed → table facts text missing from sources |
| 6 | `tests/test_document_tables.py::test_coverage_notes_name_the_real_size_of_partly_retrieved_and_unread_tables` | Docling failed → `assert (0 == 1)` (no tables) |
| 7 | `tests/test_document_tables.py::test_a_table_continued_over_pages_is_one_table_with_rows_cited_to_their_page` | Pinned embedding model not staged |
| 8 | `tests/test_folder_sync_worker_process.py::test_real_worker_process_restart_recovers_interrupted_folder_sync` | Pinned embedding model not staged |
| 9 | `tests/test_folder_sync_worker_throughput.py::test_bounded_native_worker_ingests_a_small_synthetic_source` | Pinned embedding model not staged |
| 10 | `tests/test_voice_question_upload_unit.py::test_upload_is_bounded_transcribed_and_removed_before_helper_returns` | PermissionError — test creates a temp dir under the root-owned worktree, run as `agent1` |
| 11 | `tests/test_voice_question_upload_unit.py::test_upload_enforces_size_while_streaming_and_closes_upload` | Same PermissionError |
| 12 | `tests/test_voice_question_upload_unit.py::test_parser_errors_are_sanitized_and_upload_is_closed[no_text-FileRejectedError]` | Same PermissionError |
| 13 | `tests/test_voice_question_upload_unit.py::test_parser_errors_are_sanitized_and_upload_is_closed[media_model_unavailable-InvalidInputError]` | Same PermissionError |
| 14 | `tests/test_worker_healthcheck.py::test_worker_check_uses_lightweight_config_and_database_probe` | `AttributeError: 'types.SimpleNamespace' object has no attribute 'workflow_engine_on'` (`app/worker.py:111`) |
| 15 | `tests/test_worker_healthcheck.py::test_worker_check_accepts_a_staged_local_model_without_loading_it` | Same `AttributeError` (`app/worker.py:111`) |

Failure grouping: 9 are HuggingFace/model-download failures (Docling layout model
+ pinned `all-MiniLM-L6-v2` embedding model never staged because its download
fails with `InvalidURL`); 4 are the worktree-permission artifact of running as
`agent1` (see §3); **2 are a real code/test mismatch on this WIP branch**
(`app/worker.py:111` reads `workflow_engine_on` from a config namespace that
does not provide it — likely Gap 5 worker-enable work, needs the gap agent to
check whether the test or the code is out of date).

Skips (14) include the expected ones: Windows-only, Chrome unavailable,
Qdrant URL not set, real-model / real-audio / Phase 7 benchmarks opt-in, and
2× `test_rag_real_embeddings.py` ("embedding model unavailable: InvalidURL").

Sanity check that passed: a single-file run
`tests/test_approval_delegation.py` via the same runner: **5 passed in 37.62s**.

## 2. Environment (verified)

- OS: Ubuntu 24.04.5 LTS, x86_64. Shell user is `root` (uid 0).
- git 2.43.0, Python 3.12.3, Node v24.22.1, npm 10.9.4, no Docker.
- PostgreSQL **16.15** (Ubuntu `postgresql` 16+257build1.1). The repo targets
  PG 17/18, but Ubuntu 24.04 only ships PG 16 — the test runner only needs the
  binaries (`initdb`, `pg_ctl`, `psql` in `/usr/lib/postgresql/16/bin`).
  A system cluster `16/main` is online on port 5432 (started by the package
  install); the test runner does **not** use it — it builds its own throwaway
  cluster per run and deletes it afterwards.
- RAM 7.7 GB, ~1 GB free while 10 agents run concurrently; /tmp is a
  512 MB tmpfs; /home has ~144 GB free.

## 3. Setup — exact commands that worked

### PostgreSQL

```bash
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql
ls /usr/lib/postgresql/16/bin/   # initdb, pg_ctl, psql
pg_lsclusters                    # 16 main 5432 online
```

Notes: `apt-get update` took ~7 minutes (slow mirror; one mirror,
`mirror.cogentco.com`, failed with a connection error and was skipped).
The apt lists lock is contended — all 10 agents run apt concurrently; if
`apt-get` reports "Could not get lock", wait for the other agent to finish
and retry. Do not kill another agent's apt process. The PostgreSQL binaries
are installed system-wide, so whichever agent installs them first provides
them for all. After a VM/runtime restart the apt lists are gone
(`/` is ephemeral) and `apt-get update` must be re-run.

### Python venv + dependencies

```bash
cd ~/workspace/infraspine/work/setup-baseline
python3 -m venv .venv
.venv/bin/pip install -q -U pip
TMPDIR=/home/hatch/pip-tmp .venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/python -c "import fastapi, sqlalchemy, asyncpg, pytest, alembic; print('imports ok')"
# imports ok: 0.143.1 2.1.4 9.1.1
```

The lock install took ~19 minutes (slow downloads, ~500 kB/s; torch CPU wheel
is 196 MB). It installs 173 packages including torch 2.14.1+cpu,
sentence-transformers 6.1.0, docling 2.140.0, faiss-cpu, alembic 1.20.0.

### Frontend

```bash
cd ~/workspace/infraspine/work/setup-baseline/frontend
npm ci --no-audit --no-fund     # added 224 packages in 6s; node_modules/.bin/vite present
```

### Databases (throwaway, local only — never live)

```bash
# System cluster (role password generated, stored in /home/hatch/.agent1-db-pw, chmod 600 — NOT in this report)
su postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE ROLE infraspine_agent1 LOGIN PASSWORD '<password>';\" -c 'CREATE DATABASE infraspine_setup_test OWNER infraspine_agent1;' -c 'CREATE DATABASE infraspine_ai OWNER infraspine_agent1;'"
```

- `infraspine_setup_test` — the throwaway test database for manual runs.
- `infraspine_ai` — the "application" database name, created empty so
  `conftest.py` has a configured `DATABASE_URL` to compare against; nothing
  ever connects to it (validation only — see below).
- `TEST_DATABASE_URL` pattern for manual pytest runs:

```text
postgresql+asyncpg://infraspine_agent1:<password>@127.0.0.1:5432/infraspine_setup_test
```

- The canonical runner overrides `TEST_DATABASE_URL` itself with its own
  throwaway cluster database `infraspine_ai_test` and deletes the cluster
  afterwards, so the `infraspine_setup_test` database above is the manual-run
  target, not the runner's target.
- Smoke test (passed): asyncpg connect to `infraspine_setup_test` as
  `infraspine_agent1` → server version `16.15`, `current_database()` =
  `infraspine_setup_test`.

### Why the tests run as user `agent1` (not root)

`initdb` refuses to run as root. This VM's shell is root, so the runner must
run as an unprivileged user:

```bash
useradd -m -s /bin/bash agent1
usermod -aG postgres agent1   # lets agent1 create the socket lock file in /var/run/postgresql
chmod a+rX /home/hatch /home/hatch/workspace /home/hatch/workspace/infraspine /home/hatch/workspace/infraspine/work
chmod -R a+rX ~/workspace/infraspine/work/setup-baseline
```

`chown` does **not** work in this environment ("Operation not permitted" —
the same reason the earlier `cp -a` of the repo could not preserve ownership),
so the worktree stays root-owned and is made world-readable/executable
instead. `PYTHONDONTWRITEBYTECODE=1` is set for the test run so no
`__pycache__` writes are needed. `DATABASE_URL` for the runner uses a dummy
password — `conftest.py` only parses it for the "test DB ≠ app DB" check and
never connects to it, so no real credential is passed.

## 4. Problems solved

1. **pip failed: "No space left on device"** — pip unpacks wheels into `/tmp`
   (512 MB tmpfs), which the 10 concurrent agents fill with `pip-unpack-*`
   dirs. Fix: `TMPDIR=/home/hatch/pip-tmp` (on the 144 GB home disk) for the
   pip install. The first attempt also lost its installed packages in a
   runtime restart (the `.venv` was back to pip-only); the retry with
   `TMPDIR` on the home disk succeeded.
2. **Runtime restart wiped `/tmp` and apt lists** — after the restart,
   `/tmp` was empty and `apt-get install postgresql` failed with "Unable to
   locate package". Fix: re-run `apt-get update` first. Keep logs on
   `/home/hatch` (persistent), not `/tmp`.
3. **`initdb: error: cannot be run as root`** — run the test runner as the
   unprivileged user `agent1` (created above).
4. **`pg_ctl: could not start server` — `FATAL: could not create lock file
   "/var/run/postgresql/.s.PGSQL.<port>.lock": Permission denied`** — the
   temp cluster's socket lock goes in `/var/run/postgresql` (Debian default).
   Fix: `usermod -aG postgres agent1` (verified writable afterwards).
5. **First full baseline: 84 passed, 13 skipped, 1262 errors in 204s** —
   every DB test errored at setup with
   `asyncpg.exceptions.InsufficientPrivilegeError: could not extend file
   "base/16385/...": Operation not permitted` (hint: "Check free disk
   space"). The temp cluster lived in `/tmp` (512 MB tmpfs), which was
   71–83% full from sibling agents' pip-unpack dirs and temp clusters when
   the run started. Fix: run the runner with `TMPDIR=/home/agent1/pg-tmp`
   (agent1-owned dir on the home disk). The rerun then executed tests
   normally (see §1). The single-file run
   (`tests/test_approval_delegation.py`, 5 passed) also confirmed the code
   path works when disk pressure is gone.
6. **HuggingFace unreachable from this VM** — Docling's layout-model
   download and the pinned `all-MiniLM-L6-v2` embedding-model download both
   fail (`InvalidURL` / `DoclingFailed`); `data/models/` was never created.
   PyPI works through the egress proxy, HuggingFace does not. This is an
   environment/network limitation, not code: it causes 9 of the 15 failures
   and 2 of the skips. If the model files can be provided another way
   (pre-staged `data/models/`), those 9 tests should be re-run.
7. **4 voice-upload failures are a runner artifact** — the tests create
   temp dirs under the worktree root, which is root-owned and not writable
   by `agent1`. They would likely pass with a writable worktree (or run as
   the worktree owner). Not a code failure.

## 5. What is still needed (for the master agent)

- The 2 `test_worker_healthcheck.py` failures (`app/worker.py:111`,
  `workflow_engine_on` missing from the test config namespace) look like a
  real WIP-branch mismatch — assign to the Gap 5 (worker) agent.
- The 9 HuggingFace/model failures need either network access to
  HuggingFace or pre-staged models in `data/models/` (`all-MiniLM-L6-v2`
  snapshot `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, Docling layout model).
- The 4 voice-upload failures need a writable worktree for the test user
  (or run the suite as the worktree owner) to confirm they are environmental.
- No real credentials were needed or used. No live folder, live DB, `.env`,
  uploads, or data were touched. No live push was attempted.
