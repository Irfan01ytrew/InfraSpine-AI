# Step Q — Multi-agent control room: plan

> Prepared by Agent 22 (control-room), branch `agent/control-room`, HEAD `ca956c2`
> (step-e-gaps-wip). Slices 1–5 implemented (see §4, §6, §7); no slices remain.
> Status: **all 5 slices done, tests written** (see §5); merge held for the
> full-suite gate (see `assessment/CHANGES-step-Q.md`).

## 1. What the control room is

One screen for the owner / department manager showing, per department scope:

- **Agent registry** — every Agent Studio agent owned by a member of the scope:
  name, enabled / paused, latest run (status, started/finished, failure code,
  LLM calls, tokens), so the owner sees at a glance which agents are alive,
  stuck, or failing.
- **Workflow-run roll-up** — unified-engine runs (`wf_runs`) in the scope:
  counts by status (`queued`, `running`, `paused`, `completed`, `failed`,
  `cancelled`, `compensating`, `compensated`) and by origin (`workflow`,
  `automation`, `system`, `studio`, `governance`), plus the most recent runs
  with phase, blocked reason, and timestamps.
- **Blockers** — runs that need a human: paused runs with a `blocked_reason`,
  Studio runs `awaiting_approval`, failed runs with a `failure_code`.
- **Worker health** — whether the unified workflow-engine worker is on
  (`WORKFLOW_ENGINE_WORKER_ENABLED` / `workflow_engine_on`; default on,
  Gap 5), so "nothing is moving" is distinguishable from "nothing is queued".
- **Controls (later slices, NOT slice 1)** — pause / resume / assign a run or
  agent in one click, through the unified engine only, each control action
  bound to an exact action with approval, expiry, single use, and separation
  of duties (HANDOVER-PROMPT.md §5). No control action may bypass
  `app/workflows/unified.py` or `execute_once`.

## 2. What already exists (mapped 2026-11-28)

| Need | Existing surface | Gap |
|---|---|---|
| Workflow run state | `wf_runs` / `wf_steps` (`app/db/workflow_engine_models.py`); `WorkflowService` (`app/workflows/service.py`); engine tick in `app/workflows/engine.py` | No roll-up / counts query; no per-scope summary endpoint |
| Studio agents + runs | `agent_studio_agents` / `agent_studio_runs` (`app/db/agent_studio_models.py`); Agent Studio API (`app/api/routes/agent_studio.py`); mirror to unified runs with `origin='studio'` (`app/agent_studio/unified.py`, Gap 1) | No registry view joining agents to their latest run; no cross-agent status |
| Governance / automation runs | Same `wf_runs` table with `origin='governance'` / `'automation'` (Gap 1b, Step E core) | Not surfaced together anywhere |
| Worker health | `app/worker.py --check` (Gap 5, `workflow_engine_on`); `/health` and `/health/ready` (`app/api/routes/health.py`) | `/health/ready` reports DB + ingestion only, not the workflow-engine worker |
| Authorization | `app/platform/access.py` (`resolve_principal`, `authorize`, `require` → 404 non-members, 403 no permission); `app/platform/modules.py` `group_gate` (department scope → platform decision, permission `workflow.access`); `app/workflows/security.py` `require_scope` wraps it | Nothing to add — reuse as-is |
| Admin run retention | `app/api/routes/admin_agent_runs.py` (preview/purge of old agent runs) | Retention only; not a live view |

Key finding: **no new tables or migrations are needed for slice 1.** Every
field the overview shows already exists in `wf_runs`, `wf_steps`,
`agent_studio_agents`, `agent_studio_runs`, and settings. The control room is
a read-only aggregation over the unified engine — it must never become a
second execution path.

## 3. Authorization model

- Every control-room read takes a department `scope_id` (an `access_groups`
  id, same as the workflow-engine routes).
- The route resolves the caller with the standard `CurrentUser` dependency,
  then calls `require_scope(db, user, scope_id)` (`app/workflows/security.py`
  → `app/platform/modules.py` `group_gate`, permission `workflow.access`).
  - Non-member or unknown scope → **404** (the scope is not revealed).
  - Member without the permission → **403**.
  - Admin / manager bridge: `workflow.access` is granted to `group.member`,
    `group.manager`, and `org.admin` (`app/platform/sync.py`).
- The registry lists **only** Studio agents owned by members of the scope
  (join through `access_group_members`); an agent owned by someone outside
  the scope never appears. Run rows are filtered by `scope_id` in SQL.
- No sealed payloads are decrypted or returned: run `payload` (AES-GCM
  envelope) and Studio run `state_*` ciphertext are never exposed; only
  status, phase, timestamps, failure codes, and token/LLM counters are shown.
- Control actions (later slices) additionally require `workflow.manage`
  and an exact-action approval through `app/platform/approvals.py`; they are
  out of scope for slice 1 and must not be smuggled into a read endpoint.

## 4. Slice 1 — read-only status API (done)

- `app/control_room/service.py` — `control_room_overview(db, *, scope_id,
  limit=10)`: one function, pure reads.
  - `worker`: `workflow_engine_on` from settings.
  - `runs_by_status`, `runs_by_origin`: `COUNT(*) … GROUP BY` on `wf_runs`
    in the scope.
  - `recent_runs`: latest N runs with workflow name (join `wf_definitions`),
    status, phase, origin, `blocked_reason`, timestamps.
  - `agents`: Studio agents owned by scope members, each with its latest
    run (status, created/finished, failure code, LLM calls, tokens).
  - `blockers`: paused runs with a reason, `awaiting_approval` Studio runs,
    failed runs with a failure code (latest 10, most recent first).
- `app/api/routes/control_room.py` — `GET
  /api/control-room/scopes/{scope_id}/overview?limit=10`, authorized with
  `require_scope`, registered in `app/main.py`.
- Tests: `tests/test_control_room_overview.py` (see §6).

## 5. Test list

Slice 1 (implemented):
- overview returns counts, recent runs, worker flag, and the agent registry
  for a scope member (200).
- unknown scope → 404; unauthenticated → 401.
- paused run with `blocked_reason` and failed run with `failure_code`
  appear in `blockers`; a completed run does not.
- Studio agent owned by a scope member appears with its latest run; an
  agent owned by a non-member does not.

Slices 2–3 (implemented, `tests/test_control_room_controls.py`, 12 tests):
- run pause + resume end to end needs an exact-action approval: manager
  requests, a different manager approves, the requester executes; the
  approval cannot be reused (409), and an approval for pause cannot
  resume (409).
- the requester cannot approve their own control request (403); a plain
  member gets 403 on request and execute; unknown scope/run → 404.
- pausing a studio-origin run pauses the unified mirror (Studio run row
  stays `queued`); resume restores it.
- agent pause / resume through the control room; agent assign transfers
  ownership to a scope member; assign to an outsider → 403, assign of
  the Master Agent → 400; assign without an assignee, or pause with an
  assignee → 400.
- `/health/ready` reports the workflow-engine block (`engine_on`,
  `status`, `last_tick_at`, `due_runs`); engine-off is a valid operator
  choice (readiness stays 200); `last_tick_at` is present after a
  trigger and `due_runs >= 1` when a run is due.

Still to test (later slices):
- frontend: control-room page renders the overview, polls it, and shows
  blockers first; `npm test` + build + typecheck clean. **Done (slice 4,
  Agent 129): 8 new pure-helper tests; `npm test` 30 passed, 0 failed;
  `tsc --noEmit` clean; `vite build` clean (39.71s).**

## 6. Slices 2–3 — controls + worker health (done, Agent 88)

Slice 2 — controls (`app/control_room/controls.py`,
`app/api/routes/control_room.py`):
- `POST …/runs/{run_id}/control-request` + `POST …/runs/{run_id}/control`
  (pause / resume); `POST …/agents/{agent_id}/control-request` +
  `POST …/agents/{agent_id}/control` (pause / resume / assign). Both
  endpoints require `workflow.manage` (`require_scope(..., manage=True)`).
- Runs go through `WorkflowService.control` (unified engine); resume
  continues from the last verified step and never executes a step twice.
  Agents are paused/resumed by flipping the agent's own `paused` flag
  (the unified `agent_studio.execute` handler honours it before every
  attempt); assign transfers ownership to another member of the scope.
  The Master Agent (deterministic uuid5 id
  `infraspine-agent-studio-master:<owner_id>`) cannot be reassigned.
- Every control needs an exact-action platform approval: the request
  endpoint creates a `PlatformApprovalRequest` bound to the SHA-256 hash
  of the exact action (run/agent id + command + assignee); a different
  manager approves it through `POST /api/approvals/{id}/decide`
  (separation of duties); the execute endpoint consumes it once. The
  run/agent state is pre-validated *before* the approval is consumed, so
  a stale request never burns a good approval.
- Audit records via `app.services.audit.record`
  (`control_room.run_paused/resumed`, `control_room.agent_paused/resumed/
  assignd`).

Slice 3 — worker health (`app/control_room/service.py`,
`app/api/routes/health.py`): `/health/ready` now includes a
`workflow_engine` block — `engine_on` from settings
(`workflow_engine_on`, Gap 5 kill switch), `status` on/off,
`last_tick_at` (newest `wf_events` row; the engine keeps no heartbeat
row, so an idle engine honestly reports an old timestamp), and
`due_runs` (active runs with `wake_at <= now`). Engine-off is a valid
operator choice (readiness stays 200); a health-query failure degrades
to `status: "unknown"` instead of masking a healthy DB.

Design decisions (recorded 2026-11-28):
- Action specs are scoped to the *platform* scope id for the department
  (resolved with `platform_scope_for`), not the legacy group id — the
  platform approval machinery (`authorize`, `_scope_chain`) only knows
  platform scopes; passing the group id made every approval request
  404 with `scope_missing`.
- `AgentStudioRun.status` has no `paused` value by design (check
  constraint; migration 0095 only added `paused` to
  `agent_studio_agents`). Pausing a studio-origin unified run pauses
  the mirror: the engine stops dispatching `agent_studio.execute`, the
  Studio run makes no further attempt (an in-flight attempt finishes,
  like a cancel), and resuming the mirror lets it continue; the Studio
  run row itself is left untouched.
- `request_approval`'s internal `authorize("approval.request")` is
  effectively a membership check (`permission_missing` does not raise),
  so any scope member may request; the approver must hold
  `workflow.manage` (`decide_approval` requires the row's
  `required_permission`).
- No new tables or migrations were needed for slices 2–3.

## 7. What remains for Step Q

1. **Slice 4 — frontend (done, Agent 129, commit `8a1b37b`).**
   Control-room page `frontend/src/control-room/ControlRoomRegistryPage.tsx`
   (registry table, run roll-up, blockers first, one-click controls with
   confirmation, request → approve → execute, 30s polling) + pure helpers
   `controlRoomView.mjs` + API client `api.ts`, wired as the
   `control-room` view in AdminConsole + AppSidebar. Named
   `ControlRoomRegistryPage` (not `ControlRoomPage`) on purpose: the Step
   AF branch has its own `ControlRoomPage.tsx` (charts + alerts) at the
   same path — at merge the two pages become two tabs of one Control Room
   view instead of one file overwriting the other.
2. **Slice 5 — docs + merge (done, Agent 129).**
   `assessment/CHANGES-step-Q.md` (plain language, file list, test
   counts), Alembic roundtrip + check (see CHANGES), merge to `main`
   only when green. **Full backend suite on this branch was not
   completed** (VM thrash — see CHANGES); the merge gate owns it.
3. **Out of scope for Q, belongs to J–N:** cross-agent delegation and
   Agent-to-Agent Governance (Step K) — the control room *shows* agents;
   governing them stays in K.
