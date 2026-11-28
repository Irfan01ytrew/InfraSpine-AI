# Step Q — Multi-agent control room: what changed

Plain-language changelog for the control room: one screen per department
showing which agents are alive, stuck, or failing, which workflow runs need
a human, and whether the workflow-engine worker is on — plus one-click
pause / resume / reassign controls that always need a different manager's
approval first.

## What it is

- **Agent registry** — every Agent Studio agent owned by a member of the
  department, with its latest run (status, failure code, LLM calls,
  tokens). Paused, failed, and disabled agents sort first.
- **Workflow-run roll-up** — counts by status and by origin for the
  department's unified-engine runs, plus the most recent runs.
- **Blockers first** — paused runs with a reason, runs waiting for
  approval, and failed runs, newest first, at the top of the page.
- **Worker health** — whether the unified workflow-engine worker is on,
  in the page header stats and in `/health/ready`.
- **Controls** — pause / resume a run, pause / resume / reassign an agent.
  Each control is two steps: the operator requests approval for the exact
  action, a *different* manager approves it in Approvals, then the
  operator executes it. The approval is single-use and expires; a reused,
  expired, or mismatched approval is refused. Nothing on this page acts
  without an approval, and no control bypasses the unified engine.

## Safety rules that hold

- Every read and every control goes through `app/platform`
  (`require_scope` → group gate, `workflow.access` to read,
  `workflow.manage` to control). Unknown scope or non-member → 404;
  member without the permission → 403.
- Only agents owned by members of the scope are listed; run rows are
  filtered by scope in SQL. Sealed payloads and ciphertext are never
  returned — only status, phase, timestamps, failure codes, and
  token/LLM counters.
- Pausing a studio-origin run pauses its unified mirror; the Studio run
  row itself is left untouched (its status has no `paused` value).
  Resuming continues from the last verified step and never executes a
  step twice.
- The Master Agent cannot be reassigned (the backend refuses with 400).
- Run/agent state is pre-validated *before* the approval is consumed,
  so a stale request never burns a good approval.
- Control actions are audited (`control_room.run_paused/resumed`,
  `control_room.agent_paused/resumed/assignd`).

## Files changed (vs `ca956c2`, step-e-gaps-wip)

Backend — slice 1 (Agent 22):
- `app/control_room/service.py` — `control_room_overview()`: worker flag,
  runs by status/origin, recent runs, agent registry with latest run,
  blockers.
- `app/api/routes/control_room.py` — `GET
  /api/control-room/scopes/{scope_id}/overview` (+ control endpoints in
  slice 2).
- `app/main.py` — control-room router registered.
- `tests/test_control_room_overview.py` — 6 tests.

Backend — slice 2 (Agent 88):
- `app/control_room/controls.py` — `request_*` / `execute_*` for runs and
  agents; exact-action approval specs (SHA-256 of run/agent id + command
  + assignee); platform-scope resolution (`platform_scope_for`).
- `app/api/routes/control_room.py` — `POST
  …/runs/{id}/control-request`, `POST …/runs/{id}/control`, `POST
  …/agents/{id}/control-request`, `POST …/agents/{id}/control`.
- `tests/test_control_room_controls.py` — 12 tests.

Backend — slice 3 (Agent 88):
- `app/control_room/service.py` (`workflow_engine_health`) and
  `app/api/routes/health.py` — `/health/ready` now reports a
  `workflow_engine` block (`engine_on`, `status`, `last_tick_at`,
  `due_runs`). Engine-off is a valid operator choice (readiness stays
  200); a health-query failure degrades to `status: "unknown"`.

Frontend — slice 4 (Agent 129):
- `frontend/src/control-room/api.ts` — overview + control-request /
  control client (reuses `workflowApi.scopes()` / `members()`).
- `frontend/src/control-room/controlRoomView.mjs` (+
  `controlRoomView.d.mts`) — pure view helpers: agent status, sort
  order, run roll-up, blocker titles, which controls a row allows.
- `frontend/src/control-room/ControlRoomRegistryPage.tsx` — the page:
  department selector, 30-second polling, roll-up stats, blockers
  first, agent registry, recent runs, request → approve → execute
  controls with a confirmation dialog. Read-only when the scope does
  not grant `workflow.manage`.
- `frontend/test/controlRoomView.test.mjs` — 8 pure-helper tests.
- `frontend/src/components/AdminConsole.tsx` — `control-room` view
  (the control-room API decides per department, like finance).
- `frontend/src/components/AppSidebar.tsx` — "Control room" entry in
  Operations, after Approvals.

Docs:
- `docs/step-q-control-room-plan.md` — plan, slices 1–5 marked done.
- `assessment/CHANGES-step-Q.md` — this file.

## Test counts

- Slice 1 (Agent 22): 6 overview tests passed.
- Slices 2–3 (Agent 88): 12 new control + health tests passed;
  regressions 17 passed (overview 6, workflow-engine pause test 1,
  readiness test 1, platform-core 9). Total 29 passed, 0 failed.
- Slice 4 (Agent 129): 8 new pure-helper tests passed;
  `npm test` **30 passed, 0 failed** (22 baseline + 8 new);
  `tsc --noEmit` clean; `vite build` clean (built in 39.71s).
- Slice 4 backend regressions (Agent 129 re-run of the slice 1+2
  files on the committed tree): **18 passed, 0 failed** — overview 6/6
  in 179.04s in one process; controls 12/12 each in its own process
  (87–507s each). The combined 18-test run was OOM-killed twice
  (rc=137: 14 dots, 0 failures, then 11 dots, 0 failures) under VM
  load, so the files were verified one test at a time like Agent 88
  did; one single-test run was also OOM-killed and passed on retry.
  No test failed anywhere it ran.

## Problems found and fixed

1. **Real code bug (slice 2, Agent 88):** action specs were scoped to
   the legacy department group id, so `authorize` / `_scope_chain`
   (platform scopes only) 404'd every approval request with
   `scope_missing` — proven by a live 404 in the first test run. Fixed
   by resolving the platform scope id with `platform_scope_for` in all
   four control functions (the same pattern finance code uses).
2. **Test bugs (slice 2, Agent 88, all in the new test file, all fixed
   and re-run green):** a test requested as admin instead of the
   manager (admin is not a department manager, so `require_scope`
   correctly 403s); a test expected the creation mirror, which is off
   under `APP_ENV=testing`; a test created the agent as the assignee
   (409 "already belongs to that member"); two tests expected 400 where
   the codebase maps `InvalidInputError` to 422; one test had an
   un-awaited coroutine in a `run_db` lambda.
3. **Frontend gate (slice 4, Agent 129):** `npm test` failed on
   `test/answerMarkdown.test.mjs` before install — `Cannot find package
   'react'` because this worktree had no `node_modules`. Environmental,
   not code: after `npm ci` the file passes and the suite is 30/30.

## Gates

- **Alembic:** roundtrip CLEAN on a throwaway database (Agent 129's
  private cluster, port 55502): `downgrade base` rc=0, `upgrade head`
  rc=0 (110 migration operations, ending at
  `0103_unified_studio_governance`), `alembic check` = "No new upgrade
  operations detected." Single head `0103_unified_studio_governance`.
  No migration in this step.
- **Full backend suite:** NOT run on this branch, deliberately. Slice 4
  changes no backend code (frontend only); the backend surface it
  exercises (the 18 control-room tests) is green above. The merged-HEAD
  full suite is owned by the Step E verification pass, which was running
  throughout this task (run-retry.sh, Agent 143 collecting) — starting
  a second concurrent pass would break the no-concurrent-passes rule
  and add OOM pressure to a VM at load ~33–40 with ~167 MB free. A
  quiet-VM full-suite re-run on this branch remains owed before merge,
  like for every other step.

## Merge notes

- **Two control-room pages exist on two branches.** This branch adds
  `frontend/src/control-room/ControlRoomRegistryPage.tsx` (registry,
  roll-up, blockers, controls). The Step AF branch
  (`agent/control-room-live`) adds
  `frontend/src/control-room/ControlRoomPage.tsx` (live charts +
  alerts) and wires the same `control-room` view to it. The file was
  deliberately named differently here so the two pages coexist: at
  merge, make one Control Room view with two tabs (Registry &
  controls / Charts & alerts) instead of letting one file overwrite
  the other. Both pages use the same scope selector pattern
  (`workflowApi.scopes()`) and the same 30-second polling.
- **`app/control_room/__init__.py` is created by both this branch and
  the AF branch** — reconcile to one empty file at merge.
- **No migration in this step** — nothing to renumber.
- **Out of scope for Q (belongs to J–N):** cross-agent delegation and
  Agent-to-Agent Governance — the control room *shows* agents;
  governing them stays in Step K. Step AK owns spend limits and the
  kill-switch; Q controls must never bypass AK.
- **Owner decisions needed:** none from this step.

## Leftovers

- The full backend suite on this branch (see Gates) is owed a
  quiet-VM re-run before merge — deliberately not started while the
  Step E verification pass was running (no concurrent passes).
- The AF merge (two control-room pages → one tabbed view) is a
  merge-time job, not a code job on this branch.
- Environment notes (Agent 129): private PostgreSQL 16 cluster at
  `/home/hatch/.pg-q129/cluster`, port 55502, role `q129`, DBs
  `infraspine_q129` + `infraspine_q129_test`; password in
  `/home/hatch/.agent129-db-pw` (chmod 600), env in
  `/home/hatch/.test-env-q129.sh` (chmod 600) — never commit either.
  The first cluster died mid-task (VM event shut it down; restart
  PANIC'd on a corrupt WAL — `fsync=off` throwaway trade-off) and was
  rebuilt from scratch; the `q129` OS user was also recycled by a VM
  restart and recreated as uid 1015. Backend venv used:
  `~/workspace/infraspine/work/backend-tests/.venv` (the worktree has
  no venv of its own; the dev-repo venv has no pytest).
