# InfraSpine — Step E changes (2026-11-26, updated 2026-11-27)

> **Status — updated by Agent 14 (docs-final), branch `agent/docs-final`, based on
> `step-e-gaps-wip` HEAD `ca956c2` (27 Nov 2026).**
> Gaps 1, 1b, 2, 4 and 5 are merged into `step-e-gaps-wip` and their counts below
> are filled in from the agents' reports and verified against git.
> **Gap 3 is still open** (blocked, owner permission pending) and the full-suite
> verification on the merged HEAD is still running (Agents 8 and 11), so this file
> still carries two **[PENDING: Agent 11/8 result]** markers. **Do not merge to
> `main` until those are filled and green** (see `REVIEW-CHECKLIST.md`).
> Nothing here has been pushed live. Step E is **not live** on the PC
> (`127.0.0.1:8000`, live code `356094c`, live DB head `0100`).
>
> **Where this file lives:** repo-root `assessment/CHANGES-step-E.md`. The repo
> previously had no `assessment/` folder — the earlier `CHANGES-step-A/C/D.md`
> files live in the handover package's `assessment/`, not in the repo. This file
> is placed at repo-root `assessment/` (a new top-level folder in the repo)
> because `HANDOVER-PROMPT.md` §6 names exactly that path, the handover zip
> ships `assessment/` alongside the repo, and `package_after_step_e.py` requires
> `assessment/CHANGES-step-E.md` inside the repo checkout. The alternative
> (`docs/CHANGES-step-E.md`) was considered and not used, so the final zip
> layout matches the handover zip.

Step E puts every multi-step process on **one workflow runtime** and builds the founder's
**invoice verification and payment flow** on top of it.

This is the box copy only. Nothing was pushed to the PC or deployed, no live data was used, and no real bank was called (the payment connector is a mock or a configured HTTP bank API).

- Base: `9129e45` (end of step D).
- Merged with `main` at `356094c` (ops/docker) before the final test run.
- Final head on `main` (Step E core): `4984f7a`.
- Gaps branch: `step-e-gaps-wip` — was `2fb387d` (untested WIP at handover), now
  HEAD `ca956c2` with all gaps except Gap 3 merged (17 commits: 10 gap commits,
  6 merges, 1 deploy commit; 18 files changed, +1426/−10 vs `2fb387d`).
- Alembic head on `main`: `0102_finance_invoices`.
- Alembic head on `step-e-gaps-wip`: `0103_unified_studio_governance`
  (`migrations/versions/0103_unified_studio_governance.py` is present in the WIP branch).

## What was built

### 1. One workflow runtime (module 05 engine)
- Definitions → versions → runs → steps.
- Each run records its plan and its origin (`workflow`, `automation`, `finance`, `studio`, `governance`).
- Each run moves through the phases **plan → execute → verify → recover → done**.
- **Action steps** call registered handlers. A handler does one slice of work, then a separate
  read-back check marks the step "verified". Progress checkpoints are sealed.
- Between items, the runtime re-checks:
  - the run status (paused or stopped);
  - the step lease;
  - the acting person's permission.
- **Recovery and control:**
  - retries with backoff, step timeouts and a run timeout;
  - a crashed worker's lease is reclaimed and the step continues;
  - **pause / stop / resume** continue from the last verified step;
  - if the acting person loses permission or would break separation of duties, the step goes back to the people queue (`workflow.blocked` event).
- **Built on the platform core:**
  - `authorize` on every scope;
  - approvals bound to the exact action hash, with expiry and single use; the requester cannot approve;
  - separation of duties;
  - `execute_once` for every side effect;
  - outbox events.
- **Old APIs kept through adapters:**
  - The automation API and worker still work; each automation run is mirrored as a workflow run.
  - Agent Studio and governance actions appear in the handler catalog.
  - New endpoints:
    - `POST …/runs/{r}/steps/{id}/perform` (take an action step as yourself);
    - run control `stop`;
    - generic approvals inbox `/api/approvals`.
- Migration `0101_unified_workflow_runtime` (reversible).

### 2. Invoice verification and payments (`app/finance`)
- **Ingest:** PDF, DOC or text (reuses step A's "Label: value" parsing, 10 MB cap) or JSON rows (only known fields).
  Invoice text is treated as untrusted data. It can fill invoice fields only. It can never grant a permission, change the payee or change what an approval covers.
- **Checks (17 reason codes):**
  - required fields and amount;
  - vendor in the vendor master, not blocked;
  - bank account on the invoice matches the vendor master;
  - currency;
  - PO exists, is open and belongs to the same vendor;
  - goods receipt exists;
  - amount within PO, receipt and over-billing tolerance (absolute or %);
  - future or stale date;
  - duplicates (against history and within the batch).
- **Results:**
  - **"32 of 50 verified"** headline;
  - each exception with plain-language reasons;
  - remaining invoices listed by id;
  - **exceptions queue**: *correct* (the invoice is re-verified next round) or *reject*.
- **Separate permissions per department:** `invoice.ingest`, `invoice.verify`, `payment.approve`, `funds.transfer`.
  - Roles: clerk, verifier, approver, treasury, viewer, admin.
  - Separation of duties: the same person cannot verify, approve and transfer for one batch.
- **Payments:**
  - Grouped per vendor. The payee account always comes from the vendor master.
  - Approval is bound to the exact batch, payment, payee, account, amount, currency and invoice list, with expiry and single use.
  - The approval hash is recomputed from the database before the transfer. Any change → `approval_invalid` and no transfer.
  - Transfer via `execute_once` with an idempotency key. An invoice is marked paid **only after the bank connector read-back**.
  - An unclear submit result is reconciled by looking the transfer up by idempotency key, so it is never paid twice.
  - Connectors: mock (refused in production unless explicitly allowed), HTTP bank API, and HTTP with fallback.
- **Finance spend chart API:** month × department × currency.
- Migration `0102_finance_invoices` (6 tables, roles; reversible).

### 3. Admin UI (React/Vite, existing UI kit)
- **Operations → Invoices & payments:**
  - department selector and your permissions;
  - batches with the "X of Y verified" headline;
  - invoice table with reasons and a "remaining" filter;
  - upload or paste invoices;
  - start verification, verify as me, transfer approved payments;
  - payments with bank reference;
  - exceptions queue (correct/reject);
  - payment approvals (approve this exact payment, with its hash);
  - spend chart.
- **05 · Workflow engine:** runs panel showing origin, status, phase, last verified step and plan, with pause / resume / stop / perform per step.

## Step E gaps — closed on `step-e-gaps-wip` (4 of 5 done, 1 blocked)

| # | Gap (from `HANDOVER-PROMPT.md` §4) | Owner agent / branch | Status |
|---|---|---|---|
| 1 | Agent Studio's own runs and governance actions run as real workflow runs on `app/workflows/unified.py`: pause/stop/resume from last verified step, exact-action approvals, `execute_once`. Existing Agent Studio APIs and step A pause enforcement keep working (adapters). | gap1-studio (`cba9cb1`) + gap1b-governance (`4180566`) | **Done, merged** (`ca956c2`, `a5c0a27`). No production code change was needed — the WIP branch already wired the adapters; what was missing was tests, and the agents added them (9 + 8 new tests). |
| 2 | Invoice intake from documents already indexed in Knowledge (`app/finance/documents.py`), only documents the user may read (`documents.read`); document text is untrusted. | gap2-invoice-knowledge (`8c9a4d5`, `95d4c5c`) | **Done, merged** (`2e822eb`). Backend already existed untested; agent tested it (no backend bugs found) and wired the frontend picker. 8 new tests. |
| 3 | Migration test converting existing automation rules (seed legacy rows → upgrade to 0103 → verify → downgrade). Migration `0103_unified_studio_governance.py` is present in the WIP branch. | gap3-migration-0103 | **Blocked — no files, no commits, no tests run.** Agent 5 stopped at a system execution-risk check and needs explicit owner permission to write/run in its worktree. Migration `0103` already exists and needs no change; the conversion lives in `0101`. A complete test file is prepared in Agent 5's report (not written anywhere). Owner decision pending since 27 Nov 2026 ~22:15 PKT. |
| 4 | Bank connector stays mock / HTTP / HTTP-with-fallback; show the active connector in admin settings. A real bank needs owner credentials later. | gap4-bank-admin (`9dbbbcf`) + frontend (`d61ee0e`) | **Done, merged** (`8df3f9f`, `c4d27f6`). Backend fix: in production, selecting http+mock no longer errors when an HTTP bank is configured (an unbuildable fallback is dropped, the usable primary is kept). Frontend: active connector shown in Settings → Finance tab. Mock stays default; no real bank contacted. |
| 5 | Workflow engine worker on by default for the PC and Docker deployments (`WORKFLOW_ENGINE_WORKER_ENABLED` stays as a kill switch); `run-worker.ps1` and the Docker worker start it. | gap5-worker (`5352de9`, `38191fb`, `cb738dd`, `521f6fe`) | **Done, merged** (`a0a8e60`). Kill switch confirmed working; default-on confirmed end to end. 19 new tests. |

### Gap files (verified against git, `2fb387d..ca956c2`)
- Gap 1 files: `tests/test_agent_studio_unified.py` (new, 325 lines, 9 tests); `tests/test_governance_unified_runtime.py` (new, 375 lines, 8 tests). No production code changed. Note: `app/agent_studio/service.py` in the WIP branch differs from `main` by a CRLF→LF conversion (1984-line diff, only 12 real lines) — left as-is; consider normalising at merge time.
- Gap 2 files: `tests/test_finance_documents.py` (new, 284 lines, 8 tests); `frontend/src/finance/FinancePage.tsx` ("From Knowledge documents" picker); `frontend/src/finance/api.ts` (`knowledgeDocuments()` / `addInvoicesFromDocuments()`); `frontend/src/finance/financeView.mjs` + `financeView.d.mts` (`skippedSummary()` helper); `frontend/test/financeView.test.mjs`. Behaviour locked in by test: ingesting from a folder with no readable documents returns 201 with empty invoices/skipped.
- Gap 3 files: none. No migration change needed (`0103_unified_studio_governance.py` already present); the prepared-but-unwritten test is `tests/test_migration_0103_automation_rules.py` (in Agent 5's report only).
- Gap 4 files: `app/finance/connectors.py` (fix in `get_connector()`); `tests/test_finance_units.py` (6 connector tests added); `frontend/src/finance/api.ts` (`ConnectorStatus` type + `connector()` / `setConnector()`); `frontend/src/finance/BankConnectorSection.tsx` (new, 123 lines); `frontend/src/components/SettingsPage.tsx` (new "finance" tab; other 6 tabs untouched).
- Gap 5 files: `compose.onprem.yaml` (`WORKFLOW_ENGINE_WORKER_ENABLED: ${WORKFLOW_ENGINE_WORKER_ENABLED:-true}`); `.env.docker.example` (flag documented); `app/worker.py` (`getattr(settings, "workflow_engine_on", False)` in the three worker spots — fixes `--check` crash on lightweight/fake settings); `tests/test_workflow_engine_worker_default.py` (new, 110 lines, 19 tests). Note: `run-worker.ps1` was verified by file checks only — PowerShell cannot run on this Linux VM.

## Commits (step-e)

| Commit | What |
|---|---|
| `450dcf8` | Unified workflow runtime + finance module (models, migrations 0101/0102, handlers, service, API) |
| `4edeb96` | Finance roles created on demand; migration constraint names via `op.f`; invoice-flow acceptance tests |
| `4447b9f` | Keep `config.py` CRLF line endings |
| `3ac8535` | Payment conflict path keeps ids across rollback; department-scope outsider test |
| `16cda7b` | Approvals inbox API; unit tests; unified-runtime tests |
| `4e0886b` | Finance workspace UI, runs panel, finance view tests |
| `58bba9e` | Docs: `docs/unified-workflows-finance.md`, `HANDOVER.md` head → 0102, `docs/platform-core.md` link |
| `d42a1e6` | Merge `main` (`356094c`, ops/docker) into step-e |
| `4984f7a` | Finance connector settings documented in `.env.example` |
| `cba9cb1` | Gap 1: tests proving Agent Studio runs on the unified workflow runtime (9 tests) |
| `4180566` | Gap 1b: tests proving agent action proposals run on the unified engine (8 tests) |
| `8c9a4d5` | Gap 2: wire Knowledge document intake into the finance workspace |
| `95d4c5c` | Gap 2: tests for invoice intake from Knowledge documents (8 tests) |
| `9dbbbcf` | Gap 4: unusable payment fallback must not break a usable primary |
| `d61ee0e` | Gap 4 (frontend): show active payment connector in admin Settings |
| `5352de9` | Gap 5: enable workflow engine worker by default in on-prem compose |
| `38191fb` | Gap 5: document workflow engine worker flag in Docker env example |
| `cb738dd` | Gap 5: tolerate settings objects without `workflow_engine_on` in worker |
| `521f6fe` | Gap 5: tests for workflow engine worker on by default (19 tests) |
| `8df3f9f` | Merge Gap 4 backend (`9dbbbcf`) into `step-e-gaps-wip` |
| `c4d27f6` | Merge frontend Gap 4 admin display (`d61ee0e`) into `step-e-gaps-wip` |
| `a5c0a27` | Merge Gap 1b governance tests (`4180566`) into `step-e-gaps-wip` |
| `a0a8e60` | Merge Gap 5 worker default-on (`521f6fe`) into `step-e-gaps-wip` |
| `2e822eb` | Merge Gap 2 invoice-from-Knowledge (`95d4c5c`) into `step-e-gaps-wip` |
| `35bb36b` | Deploy: Render blueprint + PORT support in API entrypoint (not a gap; `render.yaml` added, `docker/api-entrypoint.sh` binds `${PORT:-8000}`) |
| `ca956c2` | Merge Gap 1 Studio tests (`cba9cb1`) into `step-e-gaps-wip` — current HEAD |
| [PENDING: Agent 11/8 result] | Merge of the gaps into `main` — only when the full suite is green (see REVIEW-CHECKLIST.md) |

## Files

- **Backend:**
  - `app/finance/{extraction,verification,connectors,workflow,handlers,service}.py`
  - `app/db/finance_models.py`
  - `app/api/routes/{finance,approvals}.py`
  - `app/workflows/{action_runtime,actions,builtin_actions,unified}.py`
  - changes to `app/workflows/{adapters,definition,engine,service}.py`, `app/db/workflow_engine_models.py`, `app/api/routes/workflow_engine.py`, `app/services/automation_worker.py`, `app/main.py`, `app/core/config.py`, `migrations/env.py`
  - `migrations/versions/0101_unified_workflow_runtime.py`, `0102_finance_invoices.py`
  - Gap additions: `app/finance/documents.py` (Gap 2, already in WIP), `migrations/versions/0103_unified_studio_governance.py` (Gap 3, already in WIP), `app/finance/connectors.py` fix (Gap 4), `app/worker.py` `getattr` fix (Gap 5)
- **Frontend:**
  - `frontend/src/finance/{FinancePage.tsx,api.ts,financeView.mjs,financeView.d.mts}`
  - `frontend/src/workflows/RunsPanel.tsx`
  - changes to `workflows/{api,types}.ts`, `components/{AdminConsole,AppSidebar,ModuleWorkspaces}.tsx`
  - Gap additions: `frontend/src/finance/BankConnectorSection.tsx` (new, Gap 4 admin display), `frontend/src/components/SettingsPage.tsx` (new "finance" tab, Gap 4), `frontend/src/finance/FinancePage.tsx` + `api.ts` Knowledge picker (Gap 2)
- **Tests:**
  - `tests/finance_fixture.py`
  - `tests/test_finance_invoice_flow.py`, `tests/test_finance_units.py`, `tests/test_unified_workflow_runtime.py`
  - `frontend/test/financeView.test.mjs`
  - Gap additions: `tests/test_agent_studio_unified.py` (Gap 1), `tests/test_governance_unified_runtime.py` (Gap 1b), `tests/test_finance_documents.py` (Gap 2), `tests/test_finance_units.py` (6 connector tests, Gap 4), `tests/test_workflow_engine_worker_default.py` (Gap 5); Gap 3 migration test `tests/test_migration_0103_automation_rules.py` **not written yet** (blocked)
- **Docs:** `docs/unified-workflows-finance.md`, `docs/platform-core.md`, `HANDOVER.md`, `.env.example`, plus this file, `REVIEW-CHECKLIST.md`, and `PACKAGING-INFRA-SPINE-AFTER-STEPE.md` / `package_after_step_e.py` in `assessment/`.

## Tests (all on throwaway `*_test` PostgreSQL clusters)

- **New step E backend tests: 36 passed.**
  - **Invoice flow acceptance (10):**
    - end to end 50 → 32 verified, with permissions, untrusted text and spend;
    - separation of duties;
    - approval bound to the exact payment;
    - expiry, re-request and single use;
    - revoke plus time-limited replacement;
    - crash mid-verification plus lease reclaim (50 distinct executions, no doubles);
    - ambiguous bank submit reconciled by read-back;
    - pause/resume;
    - department scope (outsider 404, member without permission 403);
    - exception correction in round two.
  - Unit tests (21) and unified-runtime tests (5).
- **Full backend suite on `main` (known baseline, 26 Nov 2026): 1347 passed, 12 skipped, 0 failed.**
  - The full run had 1336 passed. The remaining 11 were caused by the run setup, not the code:
    - 9 tests need pytest's log capture, which I had switched off by mistake with `-p no:logging`;
    - 2 tests ran before the local embedding model was downloaded again after the box was replaced.
  - All 11 passed when re-run correctly (42 passed in those files).
  - Logs: `stepE/full-suite.log`, `stepE/rerun-logging-and-model-tests.log`.
- **New gap tests added (50 backend tests, all passed in their agents' runs):**
  - Gap 1: 9 passed (`tests/test_agent_studio_unified.py`); 31 relevant passed in total (9 new + 22 existing).
  - Gap 1b: 8 passed (`tests/test_governance_unified_runtime.py`); 28 relevant passed in total.
  - Gap 2: 8 passed (`tests/test_finance_documents.py`); finance regressions 31 passed; document ACL/extraction 12 passed.
  - Gap 4: 6 connector tests passed (`tests/test_finance_units.py`, connector-focused run; whole-file run was 22 passed, 1 failed — the failure, `test_file_extraction_uses_document_parsers`, is a pre-existing environmental failure, verified identical on pristine WIP via `git stash`).
  - Gap 5: 19 passed (`tests/test_workflow_engine_worker_default.py`); relevant regressions 36 passed; 55 relevant passed in total.
- **Baseline on the unmerged WIP base (Agent 1, 27 Nov 2026, `2fb387d`, 49 min): 1330 passed, 15 failed, 14 skipped.**
  - 9 failed: HuggingFace unreachable from this VM (Docling / embedding models not downloadable).
  - 4 failed: voice-upload `PermissionError` (worktree permissions — runner artefact, not code).
  - 2 failed: `app/worker.py:111` `workflow_engine_on` `AttributeError` on `SimpleNamespace` — on the **pre-merge base**; the Gap 5 `getattr` fix is merged in `a0a8e60`, so these are expected resolved — **[PENDING: Agent 11 verification]** to confirm on the merged HEAD.
- **Full backend suite after the 5 gaps: [PENDING: Agent 11/8 result — passed / failed / skipped, exact numbers, log file].**
  - Must be at least 1347 passed, 0 failed before any merge to `main`. Agent 8 is running the suite on the unmerged base; Agent 11 is verifying the merged HEAD `ca956c2`.
- **Alembic:** upgrade head → downgrade base → upgrade head (109 revisions each way on `main`); `alembic check`: no new upgrade operations. Log: `stepE/alembic-roundtrip.log`.
  - After the gaps: **[PENDING: Agent 11/8 result — round-trip result with head 0103, `alembic check` result, log file].**
- **Frontend:** `npm test` 21 passed (4 new); `npm run build` OK (typecheck clean).
  - After the gaps: **`npm test` 22 passed, 0 failed** (was 21; +1 from Gap 2's `financeView.test.mjs`), `npm run build` clean (5.29s), typecheck clean — verified on merged HEAD `2e822eb` (27 Nov 2026). The Gap 1 merge (`ca956c2`) touched only `tests/test_agent_studio_unified.py`, so this frontend result stands for the current HEAD. Gap 4 frontend alone had verified 21 passed, build + typecheck clean before merging.

## Known gaps — updated

- ~~Agent Studio and governance adapters are only exposed and tested through the handler catalog. Agent Studio's own runs do not yet execute as workflow runs.~~ → **Gap 1 — resolved.** Studio runs mirror as workflow runs with origin `studio`, governance actions with origin `governance`; pause/stop/resume from the last verified step, exact-action approvals, `execute_once` — all proven by the 9 + 8 new tests. One cosmetic caveat: a stopped run leaves the governance proposal status `approved` (no `cancelled` value exists in `ACTION_STATUSES`); execution is still blocked by the guard.
- ~~Invoices from already-indexed knowledge documents (`extract_financial_payload`) are not wired yet. Upload, paste and JSON are covered.~~ → **Gap 2 — resolved.** Knowledge intake is wired to the finance workspace UI and tested end to end.
- ~~No test yet for mirroring existing automation rules during the migration itself. Runtime mirroring is tested.~~ → **Gap 3 — still open (blocked).** No test written, no commits. Owner permission pending since 27 Nov 2026 ~22:15 PKT.
- The real bank/ERP API is not connected. The HTTP connector is built and unit-tested against a fake server. This needs the owner's bank API details, or confirmation that the mock is enough for now. → Gap 4 (connector display only; real bank still needs owner credentials).
- ~~The engine worker is still off in `.env.example` (`WORKFLOW_ENGINE_WORKER_ENABLED=false`, the existing conservative default). It must be turned on for invoice batches and mirrored automations to move.~~ → **Gap 5 — resolved.** Worker is on by default (`config.py`, `.env.example` `=true`, `compose.yaml` `:-true`, `compose.onprem.yaml`, `run-worker.ps1`, Docker worker); the kill switch `WORKFLOW_ENGINE_WORKER_ENABLED=false` still turns it off.
- Not yet tested on the PC. That is the separate live-push step — **do not live-push from this draft**. Live push needs owner approval, a verified DB backup first, and the method in `assessment/live-push/README.md`.

## Still needs the owner

- Bank/ERP API credentials (a mock is used for now), or confirmation the mock is enough.
- Meta WhatsApp Cloud API credentials (Step F, not Step E).
- LLM keys or a GPU / hardware (Step H).
- Rotation of the DB credential that was exposed in older docs.
- Explicit approval for the Step E live-push, after the full suite is green.
- **Decision pending: Gap 3 migration test** — run the prepared test (Agent 5's report has the full file) or skip Gap 3 for now.
- **Decision pending: this docs package** — the 4 `assessment/` files are written on branch `agent/docs-final`; whether to merge them into `step-e-gaps-wip` / `main` is for the Master Agent and owner to decide (they create a new top-level `assessment/` folder in the repo — see the note at the top of this file).
