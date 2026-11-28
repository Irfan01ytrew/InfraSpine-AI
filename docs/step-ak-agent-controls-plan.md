# Step AK — AI agent controls: plan

Owner-approved step (2026-11-28): manage every AI agent's data access, permissions,
spending limits and allowed actions — with a kill-switch per agent. An agent can never
grant itself more access; controls are enforced by the platform, not by the agent.

## 1. What exists (mapped at `step-e-gaps-wip` `ca956c2`, not rewritten)

| Piece | Where | What it already does |
|---|---|---|
| Principal + authorization | `app/platform/access.py` | `Principal` kinds `user` / `agent` / `service`; `resolve_principal(agent_id=...)` refuses disabled/paused `AgentStudioAgent`s; `authorize` / `require` (404 non-members, 403 members without permission); `ADMIN_BRIDGE_PERMISSIONS`, `APPROVAL_REQUIRED` (`funds.transfer` → `payment.approve`), `SEPARATED_DUTIES`; `revoke_user_access` stops a user everywhere at once |
| Approval machinery | `app/platform/approvals.py` | `ActionSpec` bound to the exact action by hash, expires, single-use, never decided by its requester or by someone who did another separated duty on the same subject |
| Exactly-once execution | `app/platform/execution.py` | `execute_once` with idempotency key + read-back verification |
| Agent Studio agents | `app/db/agent_studio_models.py` (`agent_studio_agents`) | `enabled`, `paused`, `tool_permissions` (JSONB), `approval_tools` (JSONB), `delegate_agent_ids` (JSONB), `max_steps` 1–8, `rag_enabled`, `provider_id`/`model` |
| Agent profiles | `app/services/agents.py` | `AgentProfile` / `AgentProfileRun`, fixed tool policy per agent type (`_TOOLS_BY_TYPE`), stale-run recovery with audit events |
| Internal agent catalog | `app/services/agent_catalog.py` | Publisher-granted, independently reviewed, revocable agent templates; publish/review grants (`InternalAgentCatalogGrant`) |
| Governance agents | `app/governance/models.py` (`security_agents`) + `app/governance/actions.py` | `AgentIdentity`: `tools`, `enabled`, `max_calls` / `calls_used` window budget, `window_seconds`, `timeout_seconds`; `agent_budget_denied` event; `agent.shutdown` action exists; `ActionRequest` with action hash, risk `low|medium|high|critical`, `required_approvals` 0–2; `security_approval_decisions` |
| Agent actions on unified engine | `app/services/agent_action_runtime.py` | Governance proposals run as workflow runs (`origin='governance'`) with independent approval + `execute_once` |
| LLM spend + quotas | `app/services/operational_health.py` | Spend attribution by provider/model (`calculate_token_cost`), department monthly token quotas with threshold alerts — **in-memory quota overrides only** |
| Department budgets | `app/services/budgets.py` | `block` / `warn` enforcement pattern (final approval refused when a blocking budget breaks) — the pattern spend limits follow |
| Agent management permission | `app/core/permission_catalog.py` | `agents.manage` — create, version and disable department agents in scope |

## 2. What is missing (the gaps Step AK closes)

1. **No unified per-agent control record.** Today an agent's limits live in three
   islands that do not talk to each other: Studio (`tool_permissions`, `enabled`,
   `paused`), governance (`security_agents`: call-window budget only), profiles
   (fixed tool policy per type). There is no single record that says, for one agent:
   what data it may touch, which permissions it holds, what it may spend, which
   actions it may take, and whether it is alive.
2. **No per-agent spend limit.** Spend is tracked per department (tokens, in-memory
   overrides). An individual agent can burn the whole department quota; nothing
   stops it, warns about it, or attributes the cost to the agent.
3. **No kill-switch.** Studio agents have `enabled`/`paused`, governance has an
   `agent.shutdown` action, but there is no single, audit-logged, immediate
   kill operation that stops an agent everywhere at once (runs, delegation,
   scheduled work) with a reason, and no rule that a killed agent stays dead
   until an administrator deliberately re-creates its control record.
4. **No runtime allow-list check.** `tool_permissions` is validated when an agent
   is configured, never when it acts. Nothing evaluates
   (agent record, requested action) → allow / deny / needs-approval at the moment
   of the request, checking scope, permission, spend and risk together.
5. **No spend alerts per agent.** No warn-at-80% / block-at-100% behaviour for an
   agent's own limit.
6. **No delegation limits.** `delegate_agent_ids` exists, but there is no depth
   limit, no check that the delegate is itself alive and controlled, and no ban
   on self-delegation loops.
7. **Governance is not wired to Agent Studio or WhatsApp** (master plan v2 §2):
   the governance budget/risk machinery only governs its own island.

## 3. Design rules (non-negotiable)

1. Controls are enforced by the platform, never by the agent. An agent's own text,
   instructions, or tool output can never widen its scope, permissions, spend
   limit, or action list.
2. Deny by default: anything not explicitly listed in the control record is denied.
   No wildcard permission (`*`) and no wildcard action for agents — ever.
3. The kill-switch is terminal: a killed agent's decisions are always `deny`
   (`agent_killed`), whatever else the record says. Only an administrator creating
   a new control record can bring an agent back; the kill event stays in the audit
   trail forever.
4. Spend is money: `Decimal`, never float. Cost estimates are upper bounds; a
   request whose estimated cost would take spend over the limit is denied before
   it runs (`spend_limit_exceeded`). Near the limit (≥80% by default) the decision
   is still `allow` but carries a `spend_near_limit` alert so the owner is warned.
5. Sensitive actions (money movement, sharing, privilege changes, shutting down
   another agent, deleting data) always need an independent human approval, even
   when the agent is otherwise allowed to ask for them. Risk `high`/`critical`
   always needs approval.
6. Every control change (create, change, pause, disable, kill, spend-limit change)
   is an audit event with actor, reason, and before/after values. Control changes
   themselves need `agents.manage` and are never performed by the agent they affect.
7. Delegation is bounded: depth limit, delegate must be alive and controlled, no
   self-delegation, no delegation by a killed/disabled/paused agent.
8. All dispatch through `app/platform` (`resolve_principal`, `authorize`,
   `require`); every side effect through `execute_once` with read-back
   verification; approvals bound to the exact action hash, expiring, single-use,
   separation of duties (handover §5).

## 4. Control record (slice 2 data model)

`agent_control_records` (one row per agent, all three agent kinds):

- `agent_id` (UUID, the Studio/profile/governance agent id), `agent_kind`
  (`studio` | `profile` | `governance`), `owner_id`, `scope_id` (platform scope
  the agent acts in)
- `status`: `active` | `paused` | `disabled` | `killed`
- `data_scopes`: JSONB list of scope keys the agent may read
  (e.g. `knowledge:finance`, `documents:dept-sales`) — empty = reads nothing
- `permissions`: JSONB list of platform permission strings it may exercise
- `allowed_actions`: JSONB list of action names it may request
  (e.g. `knowledge_search`, `document.metadata`) — empty = requests nothing
- `spend_limit_usd` (numeric, NULL = no limit), `spend_used_usd` (numeric),
  `alert_threshold` (default 0.80)
- `max_steps` (1–8), `delegate_to` (JSONB list of agent ids),
  `max_delegation_depth` (default 1)
- `killed_at`, `killed_by`, `kill_reason`, `version`, timestamps

`agent_control_events` (append-only audit): every control change + every denied
or approval-gated decision that matters, with actor, reason, and before/after.

`agent_spend_ledger` (append-only): one row per costed call (agent, provider,
model, tokens, cost USD, idempotency key) — the source `spend_used_usd` is summed
from, never trusted from input.

## 5. Slices

1. **(done now)** Pure-function evaluator — `app/agent_controls/evaluator.py`:
   `evaluate()` (allow/deny/needs-approval), `spend_status()`,
   `kill_switch()`, `check_delegation()`, `validate_control()` — with tests.
2. Persistence + API: Alembic migration (`0105_agent_controls.py`) for the three
   tables above; CRUD API under `app/platform` authorization (`agents.manage`
   to change, `agents.read` to view); Alembic roundtrip + check clean.
3. Enforcement wiring: gate in `resolve_principal` re-check + action-runtime
   gate (Studio runs, governance dispatch, WhatsApp intake) calling `evaluate()`
   before dispatch; denied decisions audited; spend recorded in the ledger
   through `execute_once` (a costed call that fails to record is retried, never
   double-counted — idempotency key).
4. Kill-switch endpoint + admin UI: immediate kill with reason, audit event,
   queued work for the agent cancelled (same pattern as `revoke_user_access` /
   `cancel_queued_work`); spend dashboard per agent with warn/block states.
5. Unification: migrate governance `security_agents` call-window budget and
   Studio `tool_permissions` into control records (adapters, no breaking change);
   `CHANGES-step-AK.md` + full suite / Alembic / frontend gates, then merge to main.

## 6. Test list

- Evaluator: killed/disabled/paused precedence; action not in allow-list;
  scope not covered; permission missing; spend over limit / near limit /
  exactly at limit; high/critical risk → approval; sensitive action → approval
  even when allowed; wildcard permission/action rejected by validation;
  negative spend, limit < used, bad status, max_steps out of range rejected;
  delegation depth, self-delegation, dead delegate, delegation by killed agent.
- Slice 2: migration roundtrip + check; API permission matrix (admin can
  change, owner can view own agents, agent cannot change its own record);
  kill-switch cancels queued work; ledger sums match `spend_used_usd`.
- Slice 3: end-to-end denied Studio run is audited and never dispatches;
  over-limit LLM call is refused before the provider is called.

## 7. Owner inputs needed

- Default per-agent spend limit (proposed: USD 10/month per agent, `NULL` = no
  limit for owner-created agents until set).
- Which actions count as sensitive (proposed list in the plan: money movement,
  sharing, privilege changes, agent shutdown, data deletion).
- Whether a killed agent may ever be revived by re-creating its record
  (proposed: yes, by an administrator only, with a new audit event).
