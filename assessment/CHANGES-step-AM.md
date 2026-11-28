# CHANGES — Step AM: Self-Evolving Software Foundry

Owner request (2026-11-28 ~00:38 PKT): InfraSpine ke andar ek naya product —
AI user ki zaroorat samajh kar **working software applications banaye
(interface, backend aur database ke saath), unhein test karke preview de,
user ki approval ke baad deploy kare, aur feedback se improve kare.**

Branch: `agent/software-foundry`. Base: `step-e-gaps-wip` `ca956c2`.
Plan: `docs/step-am-foundry-plan.md`. Status: **all 6 slices done** (see §1).

## 1. What was built, slice by slice

### Slice 1 — Spec validation (Agent 53, commits `41b6db2` + `7aaea2c`)
- `app/foundry/spec.py`: validates an app spec (name, purpose, pages,
  endpoints, tables) into a build plan + test checklist + preview checklist.
- Rejects unsafe specs with a named reason: destructive actions (`DROP`,
  `DELETE`, `TRUNCATE`, …), secrets in any text field, direct DB/payment
  access without an approval step, invalid names, dangling references,
  empty specs. `requires_approval` is always `True` — no input can turn it off.
- Pure functions: no code execution, no database, no network.
- Tests: `tests/test_foundry_spec.py` (28 tests).

### Slice 2 — Generator (Agent 62, commit `60ec6a7`)
- `app/foundry/generate.py`: validated spec → `GeneratedApp` as **data only**
  (React page stubs, FastAPI router stubs, Alembic migration draft — never
  auto-applied, never executed). Deterministic SHA-256 build fingerprint of
  the generated file set; fail-closed re-scan denylist.
- Tests: part of the 53 new tests in slice 2–3 (see §4).

### Slice 3 — Test + preview gate (Agent 62, commit `d8511c2`)
- `app/foundry/preview.py`: generated tests run in the QA sandbox
  (`app/qa/sandbox.py`, disposable PostgreSQL); preview is a disposable,
  expiring link backed by a disposable DB (seed data only, no production data).
- The gate blocks the approval flow on any failed or missing test; live-sandbox
  verification via `disposable_postgres`.
- Tests: part of the 53 new tests in slice 2–3 (see §4).

### Slice 4 — Approval gate + deploy (Agent 71, commit `8cae9ec`)
- `app/foundry/deploy.py` + `tests/test_foundry_deploy.py` (27 tests).
- `deploy_action_canonical()` / `deploy_action_hash()` — proven byte-identical
  to `app.platform.approvals.ActionSpec.hash()` in a test, so the approval the
  user sees is bound to the exact slug + build fingerprint + target.
- `may_request_approval()` only when the slice 3 gate is `preview_ready`.
- `validate_approval_for_deploy()` checks: approved, not expired, not consumed,
  hash matches the exact action, separation of duties (approver ≠ requester),
  decider present.
- `plan_deploy()` refuses when no deploy target is configured (empty allow-list)
  or the target is outside the allow-list.
- Deploy record lifecycle: requested → approved → deploying → deployed |
  rolled_back. `record_health(passed=False)` auto-rolls back to the previous
  build; `deploy_complete()` is True only for deployed + health passed.
  Rollback is terminal.

### Slice 5 — Feedback → improve loop (Agent 71, commit `6af9c2a`)
- `app/foundry/feedback.py` + `tests/test_foundry_feedback.py` (19 tests).
- Feedback text is untrusted: secrets and destructive instructions are rejected
  by the slice 1 scanners.
- Only structured `FeedbackChange` actions (add/remove page/endpoint/table,
  rename_app, change_purpose) alter a spec. `apply_changes_to_spec()` deep-copies
  the input and bumps the version; `revise_from_feedback()` validates every
  item, refuses `nothing_to_revise`, `feedback_attaches_to_unknown_build`, and
  `revision_invalid` (re-validated with slice 1 — e.g. a dangling endpoint or a
  payment endpoint is refused).
- A revision keeps `live_build_fingerprint` = the previous build and status
  `ready_for_test`: the live app is **never patched in place** — the revision
  re-enters the pipeline (re-validate, re-test, re-approve).
- Integration fix found by the slice 6 route tests (see §3): the build
  fingerprint (SHA-256 of the generated file set, slice 2) and the spec
  fingerprint (SHA-256 of the spec, slice 1) are different values.
  `revise_from_feedback()` now takes both — items attach to the build,
  the spec must match the build's recorded spec fingerprint.

### Slice 6 — Registry + API/UI (Agent 71, this slice)
- Pure registry: `app/foundry/registry.py` — append-only `register_build()`
  (duplicate fingerprint refused, name mismatch refused), `record_deploy_state()`,
  `record_feedback()`, `list_apps()` (sorted by slug), `get_app_detail()`.
  Tests: `tests/test_foundry_registry.py` (13 tests).
- Persistence: `app/foundry/models.py` — `FoundryApp` (`foundry_apps`, unique
  scope_id+slug), `FoundryBuild` (`foundry_builds`, unique build_fingerprint,
  gate/health/deploy_state checks, spec_version > 0, JSONB spec + test_summary),
  `FoundryFeedback` (`foundry_feedback`, kind check).
- Migration `0105_foundry_registry` (down_revision `0104_agent_controls`): creates the three
  tables; downgrade drops them; Alembic roundtrip + `alembic check` clean.
  **Merge note: sibling branches also number their next migration 0104
  (Agent 44, Step AG HR: `0104_hr_leave_attendance`) — at merge time only one
  0104 may keep this number; rename the later one and re-point its
  down_revision so the chain stays linear.**
- Service: `app/foundry/service.py` — `list_scopes()` (via `visible_scopes`
  for `foundry.read`), `list_apps()`, `get_app_detail()`, `record_build()`
  (re-validates the spec, fingerprints must agree), `request_deploy()` (build
  must be `preview_ready`; builds the exact `ActionSpec("foundry.deploy", …)`
  via `request_approval()`, required_permission `foundry.deploy`, sets
  deploy_state `requested`), `submit_feedback()` (validates the item, stores
  the note, returns a revision preview via slice 5 when changes are present).
  Permissions: `foundry.read` (list/view), `foundry.manage` (record/request/
  feedback), `foundry.deploy` (decider). Audit via `app/platform/events.audit`.
- API: `app/api/routes/foundry.py` (prefix `/api/foundry`, registered in
  `app/main.py`): `GET /scopes`, `GET /scopes/{scope_id}/apps`,
  `GET /scopes/{scope_id}/apps/{slug}`,
  `POST /scopes/{scope_id}/apps/{slug}/builds`,
  `POST /scopes/{scope_id}/apps/{slug}/deploy-request`,
  `POST /scopes/{scope_id}/apps/{slug}/feedback`.
  Pydantic models with `extra="forbid"`; fingerprints must match
  `^[0-9a-f]{64}$`. Unknown scope / non-member → 404; member without the
  permission → 403 (through `app/platform`).
- Config: `foundry_deploy_targets` in `app/core/config.py` — comma-separated
  allow-list, empty = no target configured, every deploy request refused.
- UI: `frontend/src/components/FoundryPage.tsx` (list apps, detail with a
  read-only `FlowDiagram` pipeline view, deploy-request form, feedback form —
  deploy only *requests* approval, never deploys), `frontend/src/lib/
  foundryPipeline.mjs` + `.d.mts` (pure pipeline helpers, 9 node tests),
  API client in `frontend/src/api.ts`, types in `frontend/src/types.ts`,
  wired into `AdminConsole.tsx` ("foundry" view) and `AppSidebar.tsx`
  ("Software Foundry", "13 · Software Foundry").

## 2. Safety rules — what this step guarantees

- Deploy ONLY after the user's explicit approval of the exact build
  (hash-bound, single-use, expiring, separation of duties). No auto-deploy.
- Preview before deploy; preview is disposable, expiring, seed data only.
- No destructive actions, no secrets, no raw SQL in generated apps or specs.
- Every lookup/dispatch through `app/platform`; document/feedback text is
  untrusted and can never grant permissions, change payees, or widen an approval.
- Generated code is data until validated, tested in the sandbox, and approved.
- The live app is never patched in place; feedback produces a new revision
  that re-enters the pipeline.

## 3. Bugs found by slice 6 testing (fixed in this branch)

1. **Build fingerprint ≠ spec fingerprint (real integration bug).** Slice 5's
   `revise_from_feedback()` compared the build fingerprint against the spec
   fingerprint, so every feedback-with-changes submission through the API
   would have failed with `feedback_attaches_to_unknown_build` in production.
   Fixed: `revise_from_feedback()` now takes `known_spec_fingerprint`
   separately (falls back to the old single-fingerprint behaviour for
   slice 5's pure tests), and the service passes the build's recorded spec
   fingerprint. Two new pure regression tests in
   `tests/test_foundry_feedback.py`.
2. **Test-only: deploy tests set the env var after app startup.** Settings are
   `lru_cache`d at app creation, so `monkeypatch.setenv` did nothing. Fixed by
   setting the attribute on the cached `Settings` object.
3. **Test-only: validation failures return 422, not 400.** The app maps
   `InvalidInputError` → 422 `invalid_input`. Three test expectations corrected.
4. **Test-only: `live_build_fingerprint` is the previous spec fingerprint,**
   not the build's file-set hash. One test expectation corrected.
5. **Test-only (pre-existing, slice 3): preview sandbox tests hardcode the
   user `agent27`, which does not exist on this VM after the rebuild.**
   The helper now skips cleanly with a reason instead of failing.

## 4. Tests

Pure suites (no database, `--noconftest`), one run:
- `tests/test_foundry_spec.py`, `test_foundry_generate.py`,
  `test_foundry_preview.py`, `test_foundry_deploy.py`,
  `test_foundry_feedback.py`, `test_foundry_registry.py`:
  **147 passed, 2 skipped** (the 2 sandbox tests that need user `agent27`).

Route tests (real PostgreSQL + real app, conftest migrates to head 0104):
- `tests/test_foundry_routes.py`: **18 passed in 578.20s** (full-file run,
  2026-11-28 ~01:33 PKT, quiet window after a VM reboot). Earlier runs on the
  saturated VM were OOM-killed at 9–10 passing dots with zero failures; the
  run was split into chunks (chunk A 6 passed) and then re-run whole.
  Note: the VM rebooted twice during this verification and the private test
  cluster had to be restarted (user `foundry` is ephemeral — recreate with
  `useradd` after a reboot; cluster data at `/home/hatch/tmp/pg-foundry`
  survives; cluster runs with `fsync=off` for test speed).

Frontend:
- `frontend/test/foundryPipeline.test.mjs`: 9 passed (`node --test`).
- Full frontend `npm test`: 31 passed, 0 failed.
- `tsc --noEmit`: clean (exit 0). `npm run build`: clean (exit 0, built in
  23.57s). One real error was found and fixed along the way (`Alert
  tone="error"` → `"danger"` in `FoundryPage.tsx`); two earlier `tsc` runs
  were OOM-killed by the saturated VM before the fix could be verified.

Alembic:
- `upgrade head` → head `0105_foundry_registry`; `alembic check` clean
  ("No new upgrade operations detected"); downgrade to `0103` then upgrade
  back to head clean; single head.

Regressions:
- `tests/test_platform_core.py`, `tests/test_approval_chains.py`,
  `tests/test_access_control.py` (the platform pieces slice 6 builds on —
  `require()`, `request_approval()`, audit): **15 of 22 passed, 0 failed**
  across two runs; the runs were OOM-killed by the saturated VM (load 30–90,
  15–29 concurrent pytest processes) at 10 and then 5 passing dots. The 7
  not yet re-run are all in `test_access_control.py` (ACL/quarantine paths);
  slice 6 changes no platform code at all, and the 18 route tests exercise
  `require()` / `request_approval()` / audit end to end. Re-run the 7 on a
  quiet machine before merge.

Not run here (merge gate, needs a quiet machine):
- Full backend suite on this branch.

## 5. Files changed (branch `agent/software-foundry`)

New: `app/foundry/deploy.py`, `app/foundry/feedback.py`,
`app/foundry/registry.py`, `app/foundry/service.py`, `app/foundry/models.py`,
`app/api/routes/foundry.py`, `migrations/versions/0105_foundry_registry.py`,
`frontend/src/components/FoundryPage.tsx`,
`frontend/src/lib/foundryPipeline.mjs`, `frontend/src/lib/foundryPipeline.d.mts`,
`frontend/test/foundryPipeline.test.mjs`,
`tests/test_foundry_deploy.py`, `tests/test_foundry_feedback.py`,
`tests/test_foundry_registry.py`, `tests/test_foundry_routes.py`,
`assessment/CHANGES-step-AM.md` (this file).
Changed: `app/foundry/__init__.py`, `app/foundry/spec.py` (slice 1, Agent 53),
`app/foundry/generate.py`, `app/foundry/preview.py` (slices 2–3, Agent 62),
`app/core/config.py` (`foundry_deploy_targets`), `app/main.py` (router),
`migrations/env.py` (foundry models), `frontend/src/api.ts`,
`frontend/src/types.ts`, `frontend/src/components/AdminConsole.tsx`,
`frontend/src/components/AppSidebar.tsx`, `docs/step-am-foundry-plan.md`
(status line).

## 6. Owner decisions still needed (plan §5)

1. **Deploy targets** — where may a generated app be deployed? Until the owner
   picks, `foundry_deploy_targets` is empty and every deploy request is refused
   with `no_deploy_target_configured`. (The route tests use `codespace-demo`
   as a test-only target name.)
2. **Sandbox limits** — CPU/memory/runtime limits for generated-app test and
   preview runs; whether generated apps may call the network at all.
3. **Generated-app data rules** — real user data, or demo/seed data only until
   a second explicit approval?
4. **Cost guard** — per-app build budget / spend limit before the build pauses
   for approval.

## 7. What remains

- The 7 remaining `test_access_control.py` regressions + the full backend
  suite, on a quiet machine (see §4).
- Merge to `main` per the merge plan (watch the 0104 migration number
  collision, §1 slice 6; the Foundry API uses scope-prefixed routes
  `/api/foundry/scopes/{scope_id}/…`, not the plan's shorthand
  `/api/foundry/apps` — deliberate, every lookup goes through `app/platform`).
- Owner decisions in §6 before any real deploy can happen.

## 8. Test environment notes (for the next agent)

- Private PostgreSQL 16 cluster (the VM's system Postgres was wiped by a
  reboot; the shared test user cannot create databases): binaries extracted
  from `~/workspace/infraspine/work/backend-tests/.pg-debs/` to
  `/home/hatch/tmp/pg-dist`; cluster `/home/hatch/tmp/pg-foundry/cluster`,
  port **55445**, socket `/home/hatch/tmp/pg-foundry`, trust auth, user
  `postgres`; databases `infraspine_foundry` + `infraspine_foundry_test`.
  Cluster runs with `fsync=off`, `synchronous_commit=off`,
  `full_page_writes=off` (throwaway test data only) because the stock
  `TRUNCATE … RESTART IDENTITY` reset took 3+ minutes per test on the loaded
  disk. Restart after a VM reboot (the `foundry` OS user is ephemeral —
  recreate with `useradd -m -s /bin/bash foundry`, `chown -R
  foundry:foundry /home/hatch/tmp/pg-foundry`, then `pg_ctl start` as that
  user; cluster data survives reboots).
- The VM rebooted twice during this verification; the runtime also restarted
  twice, killing in-flight test runs. Long DB-backed runs get OOM-killed
  when the load exceeds ~40 with 20+ concurrent pytest processes — run them
  in the quiet window right after a reboot, or split the file into chunks.
- Venv: `~/workspace/infraspine/work/setup-baseline/.venv/bin/python`
  (system python3 has no pytest). Alembic needs the `postgresql+asyncpg://`
  scheme; plain `postgresql://` fails (`No module named 'psycopg'`).
