# Step J — AI Workforce OS: what changed

Step J is code-complete: slices 1–8 are done. Every agent in the company —
Studio agents, governance agents and legacy profile agents — now lives in
one registry with one lifecycle, verified skills, delegation rules, budget
limits and spend reporting, all behind the same platform gates as the rest
of the system. An agent whose owner is revoked is paused, never deleted,
and its verified skills are frozen, never silently reassigned.

Hard rule, kept throughout: workforce controls never grant permissions,
never move money and never reassign ownership. A budget refusal stops a
run before it is created; a delegation denial is audited, never silently
retried; a retired agent is terminal.

## What was built, slice by slice

### Slice 1 — Assignment evaluator (Agent 121)
Pure functions in `app/workforce/assignment.py`: `assign_task()` picks an
agent from a roster — status, scope, action, skills, capacity,
spend-remaining and task-cost-cap filters, load-balanced ranking,
deterministic tie-break, full rejection reasons — plus `skill_match()` and
`validate_roster()`. No migration, no route, no production code changed.
37 tests passed.

### Slice 2 — Skill registry (Agent 121)
Verified skills: `WorkforceSkill` (catalog, unique normalized name) +
`WorkforceAgentSkill` per-agent rows (verified_by, verified_at,
expires_at, default 365 days); verification rule is plan §7.3 (agent
owner, admin, or `workforce.manage` holder; new platform permissions
`workforce.manage` / `workforce.read`); expired skills never match
assignment. Migration `0104_workforce_skills`. Skills never grant
permissions. Deliberate deviation, recorded in the service: no
`execute_once` for verification — it is an idempotent, reversible DB
state change with no external dispatch; the unique constraint + row lock
+ read-back gives the same exactly-once guarantee. 19 DB tests passed,
54 pure regressions passed.

### Slice 3 — Org registry + lifecycle (Agent 159)
Unified registry view over the three islands (Studio / governance /
legacy profiles) built on AK's `unify.py` read path — read, not forked.
Lifecycle states active/paused/retired (active↔paused, active→retired,
paused→retired, retired terminal); effective status is the most
restrictive of registry / island / owner-revoked (retired > disabled >
paused > active). `WorkforceRegistryAgent` org-level lifecycle row +
append-only `WorkforceLifecycleEvent` history (reason required, audit
`workforce.lifecycle_*`). API `GET/POST /api/workforce/agents…`
(list/get/history/pause/resume/retire/owner-revocations) gated via
`app/platform` (404 non-members, 403 no-permission). Migration
`0105_workforce_registry`.

THE OWNER-REVOCATION ANSWER (plan §7.7, decided here): when an agent's
owner is revoked/removed (`User.is_active` false), the agent is PAUSED,
never deleted, and its verified skills are FROZEN (freeze = `expires_at`
moved to now, so the skill reads "expired" and drops out of
`current_skills()`; verification history is preserved, so a new owner may
re-verify later); ownership is NEVER silently reassigned. Profile agents
are department-owned, so one user's revocation does not pause them.
Gates: pause/resume — owner, admin, or `agents.manage` /
`workforce.manage` holder on org scope; retire — admin or manage-holder
only (owner may ask, cannot retire); reads — `workforce.read` /
`workforce.manage` / `agents.manage` or owner/admin. Same deliberate
deviation as slice 2: no `execute_once` for lifecycle writes. Real bug
fixed: a registry row read after rollback raised MissingGreenlet (state
is now captured before rollback). 14 pure tests passed, 24 DB tests
passed, 68 pure regressions passed, slice-2 DB 19 passed.

### Slice 4 — Delegation policy (Agent 176)
`app/workforce/delegation.py` (pure): versioned `DelegationPolicy` with
rules (from/to kind and agent, allowed actions/scopes, allows_sensitive)
and `evaluate_delegation` — checks in order: active policy → depth →
child active → rule match → sensitive → tools ⊆ child's own tools →
separated duties (originator ≠ approver). Default max depth 2 (plan
§7.6). `WorkforceDelegationPolicy` + append-only
`WorkforceDelegationAudit` (migration `0106_workforce_delegation`),
`DelegationService` (create/list/active, dry-run evaluate, audit for
allowed AND denied delegations, `commit=False` savepoint mode for
Studio), API `POST/GET /delegation-policies`,
`GET /delegation-policies/active`, `POST /delegations/evaluate`,
`GET /delegations`. Studio `_delegate` is gated by the policy after its
existing checks; a denial raises ValueError like other denials; a
policy-check exception fails closed. With no policy, delegations are
allowed and audited as `no_active_policy`.

Recorded deviation: the plan's "child run on the unified engine
(origin='workforce')" is a MERGE-TIME integration, not built here —
`wf_definitions.origin` admits only workflow/automation/system/studio/
governance, and a triggered WorkflowRun would be claimed by the engine
worker and executed a second time unless the engine gains a delegation
handler. On this branch the child executes inline in the parent's Studio
graph; audit records `child_run_kind='inline'` + a fresh
delegation-execution UUID as `child_run_id`. Real bug fixed: `depth >
max_depth` raised TypeError on a non-int max_depth (guarded). 16 pure
tests passed (including a hostile-injection-text case), 13 DB tests
passed.

### Slice 5 — Budget enforcement (Agent 176)
`app/workforce/budget.py` (pure): `BudgetLimits`, UTC calendar day/month
windows, `check_budget` (daily/monthly spend, daily/monthly calls, org
monthly spend, org agent quota). Default: USD 10 per agent per month
(plan §7.1, AK proposal adopted for J), no daily caps, no org quotas
(plan §7.5). `WorkforceBudget` + `WorkforceOrgQuota` (migration
`0107_workforce_budget`). `budget_service.evaluate_start` derives spend
from `AgentStudioRun.estimated_cost_usd` (never invented prices; the AK
spend ledger is the merge-time source) and governance calls from
`AgentIdentity.calls_used`; profile agents report 0/0 with a note.
Wiring: `AgentStudioService.start_run` refuses over-budget starts with
`InvalidInputError` BEFORE run creation (idempotent replays still return
early first); registry-paused/retired agents never start inline (their
runs stay queued); `execute_claimed` hands a refused claim back
(queued, attempt not counted; budget → retry in 1h, lifecycle → retry
now); `claim_agent_studio_run` skips registry-paused/retired agents;
`revoke_user_access` pauses the owner's agents and freezes their skills
after commit (best-effort hook, derived pause still protects). API
`GET/PUT/DELETE /agents/{kind}/{id}/budget`,
`GET /agents/{kind}/{id}/budget/check`, `GET/PUT /org-quota`. 10 pure
tests passed, 15 DB tests passed (13 in the first run + 2 fixed and
re-verified green: the revocation hook's module-level entry point was
missing — `revoke_user_access` swallowed the ImportError and the hook
silently did nothing — and the hand-back test created its run after
the budget was already exceeded, while `start_run` refuses over-budget
creation outright; the run is now created first, then the budget is
exceeded).

### Slice 6 — Workforce reporting (Agent 176)
Read-only `WorkforceReportingService`: `agent_report` (runs by status,
success rate, avg duration, cost, tokens, calls, cost/task, skills,
delegation counts, can_start + reason, monthly cap), `org_report`
(per-agent rows + totals + by-status + org quota), `spend_report`
(per-agent cost, feeds Step N). Days param 1–365, default 30.
Governance/profile agents show zero runs with an explicit "No run rows
on this branch" note, never estimated. API `GET /report/org`,
`GET /report/spend`, `GET /report/agents/{kind}/{id}` (all `?days=`).
6 DB tests passed.

### Slice 7 — Admin UI (Agent 176)
Skill routes the slice-2 service never got: `GET/POST /skills`,
`GET/POST /agents/{kind}/{id}/skills`, `POST …/skills/{skill_id}/verify`,
`POST …/skills/{skill_id}/revoke`, `DELETE …/skills/{skill_id}`.
`frontend/src/workforce/` — api client, types, pure view helpers
(`workforceView.mjs` + `.d.mts`: status tones/labels, agent filter/sort,
money/duration formatting, org summary) and `WorkforcePage.tsx`
(Registry tab: search + status filter, lifecycle pause/resume/retire
with a required reason, skills attach/verify/revoke/remove, budget form
+ can-start check, 30-day report, lifecycle history; Delegation tab:
policy list + create-and-activate form, delegation audit; Spend tab:
org quota form, org stats, per-agent spend table). Wired as admin-only
view `#/workforce` ("AI Workforce" under Administration in the sidebar).
Real bug fixed while wiring: skill status is `unverified` / `verified` /
`expired` — the page first used nonexistent `pending` / `revoked`
labels. Frontend: 7 new node tests passed, 29 total passed, `tsc
--noEmit` clean, `vite build` exit 0 (WorkforcePage chunk emitted and
lazy-loaded). 3 DB tests passed for the skill routes.

### Slice 8 — Final (Agent 176)
This file, plan §5 slices 4–8 marked DONE with test counts, plan §7
decisions 1 (USD 10/month default implemented), 2 (max concurrent
default 1, slice 1), 6 (depth 2 implemented) settled, Alembic roundtrip
+ check green (downgrade base → upgrade head → `alembic check` clean →
`current` 0107_workforce_budget, single head; the first check failed
because `migrations/env.py` never imported the delegation/budget
models — fixed by adding the two imports), focused regressions green
(slices 1–3 DB re-run: 51 passed in one process —
`test_workforce_registry_service.py` 24 +
`test_workforce_skills.py` 19 + `test_agent_studio_pause.py` 4 +
`test_agent_studio_queue.py` 4; pure regressions 94 passed), full
backend suite owed (load control — Step E verification owns the merge
gate).

## Decisions settled in this step (plan §7)
1. Default spend limit per agent: **USD 10/month** (implemented, slice 5).
2. Default max concurrent tasks per agent: **1** (slice 1).
3. Who may verify a skill: agent owner, admin, or `workforce.manage`
   holder (implemented, slice 2).
4. Sensitive-task flag policy: admin only (proposed — the delegation
   `allows_sensitive` rule is the enforcement point; no separate flag
   table was needed on this branch).
5. Org quotas: none until the owner sets them (implemented, slice 5).
6. Delegation max depth: **2** (implemented, slice 4).
7. Lifecycle: retire is admin or `agents.manage` / `workforce.manage`
   holder only; owner may pause/resume but cannot retire (decided
   slice 3). Owner-revocation answer: revoked owner → agent PAUSED,
   verified skills FROZEN, ownership never silently reassigned.

## What is still owed (not blocking this step's completion)
- Full backend suite before merge (load control; Step E verification
  owns the merge gate).
- Slices 1–3 DB re-run in one process on a quiet VM
  (`test_workforce_registry_service.py`, `test_workforce_skills.py`,
  `test_agent_studio_pause.py`, `test_agent_studio_queue.py`).
- Delegation origin='workforce' unified-engine integration at merge
  (see slice 4 deviation above).
- AK spend-ledger join for budget spend (slice 5 derives spend from
  `AgentStudioRun.estimated_cost_usd` until AK merges) and AK
  control-record join for the registry read view (slice 3).

## Merge notes
- Migrations 0104 (`0104_workforce_skills`), 0105
  (`0105_workforce_registry`), 0106 (`0106_workforce_delegation`),
  0107 (`0107_workforce_budget`) ALL collide — renumber into the single
  linear chain at merge (0104 claimed by HR, WhatsApp AE, AI freshness,
  AB, G, AA, AM, Z, R, AF, AL, O, J, F, Y, W; 0105 by AK, AI, AB, AA, G,
  W, J; 0106 by K, M, G, L, W, J; 0107 by K, J).
- AK's `unify.py` read path was read, not forked — coordinate at merge;
  do not fork it.
- `app/control_room/__init__.py` + `ModuleWorkspaces.tsx` conflicts are
  merge-time reconciliations (J did not touch either file).
- Step T cost estimates feed slice 5; Step N consumes slice 6's
  per-agent facts.
- Alembic head on this branch: `0107_workforce_budget` (single head).
