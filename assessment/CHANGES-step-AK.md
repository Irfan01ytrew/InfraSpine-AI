# Step AK — AI agent controls: what changed

Plain-language summary of the work in this branch (`agent/agent-controls`,
base `step-e-gaps-wip` `ca956c2`).

## The problem

Before this step, every AI agent in InfraSpine had its own separate limits,
and the three places never talked to each other:

- Agent Studio agents had `tool_permissions` and a pause switch.
- Governance agents (`security_agents`) had a call-window budget (how many
  calls in how many seconds).
- Nothing had a spend limit in money, and nothing had a kill-switch: there
  was no way for an administrator to stop one agent immediately, everywhere,
  and prove it stayed stopped.

## What this step adds

**One control record per agent.** Each record says: is the agent active,
paused, disabled or killed; which data scopes it may touch; which
permissions and actions it is allowed; a spend limit in US dollars
(`NULL` = no limit); a warning threshold (default 80% of the limit);
how many steps it may run; and which other agents it may delegate to.

**A decision function.** Before an agent acts, the evaluator answers
`allow`, `deny`, or `needs_approval`. A killed, disabled or paused agent
is denied first, before anything else is checked. Actions not on the
allow-list, data outside the agent's scopes, and missing permissions are
denied. Spending past the limit is denied before the provider is called.
Sensitive actions (moving money, sharing documents, changing permissions,
shutting down or killing an agent, deleting data, rotating secrets) and
high/critical-risk actions always need an independent human approval —
even when the agent would otherwise be allowed.

**A terminal kill-switch.** An administrator kills an agent with a reason.
The kill cancels the agent's queued work and pending approvals, writes an
audit event, and publishes `agent.killed`. A killed agent can never revive
itself, and the backend refuses to un-kill it by editing the record.
Revival (if the owner wants it) means an administrator deliberately
creating a new record, with a new audit event.

**Spend ledger.** Every costed call is recorded exactly once
(idempotency key + `execute_once`): a call that fails to record is
retried, never double-counted. `spend_used_usd` is always the ledger sum.
Money is stored and shown with 4 decimal places, never as a float.

**Enforcement everywhere it matters.** A controlled-stopped agent is
refused at login and at every re-check, cannot claim new Studio work, and
cannot execute a governance proposal even if it was approved earlier.

**Admin UI (slice 4).** A new "Agent controls" page in the admin console
(`#/agent-controls`, admins only): the list of controlled agents with
status and a spend bar (ok / near limit / at limit / over limit), a
detail view with the spend report from the ledger and the audit trail,
pause / resume / disable with a reason, a spend-limit editor, and the
kill dialog (reason required, terminal warning, no revive button).

**Unification adapters (slice 5).** The two old islands are now visible
to the evaluator as control records — read-only, no database writes:
`app/agent_controls/unify.py` maps a Studio agent and a governance agent
to an `AgentControl` view. Anything an old island does not express stays
empty (deny by default) and is listed in the view's notes. A call-window
budget is **not** money and is never converted into a spend limit. A real
control record always wins over a legacy view. Writing real control
records from legacy agents (a backfill) is a deliberate owner decision
left open at merge time — it would change what existing agents are
allowed to do, so it is not done silently here.

## Files changed (whole step)

- `app/agent_controls/evaluator.py` — allow/deny/needs-approval, spend
  limits, kill-switch, delegation checks (slice 1).
- `app/agent_controls/store.py` — CRUD, audit events, kill (cancels queued
  work, publishes `agent.killed`), idempotent spend (slice 2).
- `app/agent_controls/gate.py` — enforcement at login/re-check, Studio
  claim, governance execution (slice 3).
- `app/agent_controls/unify.py` — legacy-island adapters (slice 5).
- `app/db/agent_controls_models.py` — `AgentControlRecord`,
  `AgentControlEvent`, `AgentSpendLedger` (slice 2).
- `migrations/versions/0104_agent_controls.py` — creates the three tables
  (slice 2). **Merge note:** `down_revision` is `0103`; rebase to `0104`
  if Agent 44's HR migration lands first. `0105` is also minted by Step
  AI's contradiction-scan migration on another branch — renumber at merge.
- `app/api/routes/agent_controls.py` — `GET/POST/PATCH /api/agents/controls`,
  `POST /{id}/kill`, `GET /{id}/events`, `GET /{id}/spend` (slice 2).
  `agents.manage` to change, `agents.read`/`agents.manage` to view; the
  new `agents.read` permission is **not** implied by the legacy admin role.
- `app/api/deps.py`, `app/main.py`, `app/platform/models.py`,
  `app/db/models.py` — wiring and registration (slices 2–3).
- `app/agent_studio/service.py`, `app/governance/unified.py` — gate wiring
  (slice 3).
- `frontend/src/components/AgentControlsPage.tsx` — admin UI (slice 4).
- `frontend/src/agent-controls/agentControlsView.mjs` (+ `.d.mts`) — pure
  view helpers (slice 4).
- `frontend/src/api.ts`, `frontend/src/types.ts`,
  `frontend/src/components/AdminConsole.tsx`,
  `frontend/src/components/AppSidebar.tsx` — client + navigation (slice 4).
- `docs/step-ak-agent-controls-plan.md` — plan (slice 1).
- Tests: `tests/test_agent_controls_evaluator.py`,
  `test_agent_controls_store.py`, `test_agent_controls_gate.py`,
  `test_agent_controls_unify.py`, `frontend/test/agentControlsView.test.mjs`.

## Tests

- Slice 1 evaluator: **45 passed**.
- Slice 2 store: **19 passed**; slice 3 gate: **14 passed**;
  regressions (evaluator 45 + Studio queue 4 + governance unified
  runtime 8): **57 passed** — all on throwaway `*_test` databases.
  Re-run on the rebuilt cluster (port 55460, 2026-11-28): OOM-killed
  partway — `STORE_GATE_RC=137`, **24/33 passed, 0 failed**; the
  remaining 9 tests never ran. A quiet-VM re-run is still owed before
  merge; the earlier full 33-test run on the old cluster was green.
- Slice 4 frontend helpers: **5 passed**; full frontend suite:
  **27 passed, 0 failed**; `tsc --noEmit` clean; `vite build` clean
  (first attempt OOM-killed by the shared VM, clean on retry).
- Slice 5 unify adapters: **8 passed** (pure functions, no database).
- Alembic: roundtrip upgrade head → downgrade base → upgrade head +
  `alembic check` clean, head `0104_agent_controls`. Re-run on the
  rebuilt cluster (port 55460, 2026-11-28 03:01:40 UTC): roundtrip +
  `alembic check` clean (`No new upgrade operations detected`),
  `ALEMBIC_RC=0` — `GATES DONE`.

## Environment notes (this VM)

The VM restarted during this session and wiped the system PostgreSQL
the earlier slices used (`.test-env.sh` pointed at port 5432, which no
longer exists). A private PostgreSQL 16 cluster was rebuilt for this
step at `/home/hatch/.pg-ak108/cluster`, port **55460**, role `aktest`,
databases `infraspine_ak_test` / `infraspine_ak` (same role password as
before, kept in the worktree's untracked `.test-env.sh`, never committed).
The store/gate regressions and the Alembic roundtrip were re-run
against that cluster (2026-11-28 03:01:40 UTC): the Alembic roundtrip
is green (`ALEMBIC_RC=0`, `GATES DONE`); the store+gate re-run was
OOM-killed at 24/33 passed, 0 failed, and the remaining 9 tests still
owe a quiet-VM re-run before merge.

## Known limits / open items

- The full backend suite was **not** run on this branch (the shared VM
  cannot complete it right now — see the Step E verification gate,
  Agent 104/110). Only the focused Step AK suites, the frontend suite,
  and Alembic were run here.
- No migration backfill writes control records for existing Studio /
  governance agents; they keep their previous behaviour until an
  administrator creates a record for them (slice 3 gate rule).
- Kill revival policy, the default spend limit, and the sensitive-action
  list are owner decisions (see below).

## Owner decisions needed (proposed defaults, not yet answered)

1. **Default spend limit:** USD 10/month per agent; `NULL` (no limit)
   for owner-created agents until a limit is set.
2. **Sensitive-action list:** money movement, sharing, privilege
   changes, agent shutdown, data deletion (implemented as
   `funds.transfer`, `payment.approve`, `document.share`,
   `agent.shutdown`, `agent.kill`, `permission.grant`, `user.revoke`,
   `data.delete`, `secret.rotate`).
3. **Kill revival policy:** yes, by an administrator only, with a new
   audit event; a killed agent can never revive itself.
