# Step J — AI Workforce OS (plan)

## 1. Plain language

Every company AI agent has **one identity** in an org-wide registry: a name, an
accountable human owner, a role, a scope, a tool list, a spend/call budget that
actually stops it, and a lifecycle (create, enable, pause, disable, revoke,
retire). Work is **assigned** to agents by skill, capacity and cost — not by
whoever happens to be free. Agents may hand work to each other, but only when
a **policy** allows it, and the receiving agent never gains power from the
request. This is org-wide, not "one user's toys."

Step J also delivers **Agent-to-Agent Governance** (master-plan-v2 §2): delegation
is a policy decision with an audit trail, not a parent-configured allow-list.

Source of truth for the spec: `assessment/master-plan-v2.md` §1 (AI Workforce OS)
and §2 (Agent-to-Agent Governance). Acceptance tests below are quoted from there.

## 2. Overlap check (read 2026-11-28, base `ca956c2` + agent branches)

### What already exists — reuse, do not rebuild

| Piece | Where | What it does |
|---|---|---|
| Agent Studio agents | `app/db/agent_studio_models.py` (`AgentStudioAgent`: name, instructions, `tool_permissions`, `approval_tools`, `delegate_agent_ids`, `max_steps` 1–8, `enabled`, `paused`, `version`, per-user `owner_id`, `provider_id`, `model`, `rag_enabled`) + `app/agent_studio/service.py` + `worker.py` (claim: oldest queued run, agent enabled + not paused, limit 1) | Per-user agent identity, queue, pause |
| Studio analytics | `app/agent_studio/analytics.py` (`agent_studio_analytics`) | Per-user run counts, durations, tokens, `estimated_cost_usd`, success rate — **per-user scope only**, not org-wide |
| Governance island identity | `app/governance/models.py` (`AgentIdentity` / `security_agents`: tools, `enabled`, `max_calls` / `calls_used` window budget, `window_seconds`, `timeout_seconds`) + `app/governance/actions.py` (`agent_budget_denied`) | Call-window budget, enforced — **but governance is not used by Agent Studio or WhatsApp** |
| Governance roles | `app/governance/models.py` (`Assignment` = human → role, `PolicyVersion` = versioned rule lists) | Human governance, not agent workforce |
| Department (human) budgets | `app/services/budgets.py` (`BudgetUse`, `check_budgets_for_approval`) | Budgets for service requests, not agents |
| Legacy agent profiles | `app/services/agents.py` (`AgentProfile`, versions, `disable_profile`) | Third agent identity island |
| Human expertise directory | `app/services/expertise.py`, `app/api/routes/expertise.py` | Skills of **people**, not agents — no agent skill registry exists |
| Platform core | `app/platform/` (`resolve_principal`, `authorize`, `require`, exact-action approvals, `execute_once` + read-back) | The enforcement spine every slice must use |
| Unified workflow engine | `app/workflows/unified.py` (mirror/origin runs; Gap 1 mirrors Studio runs as `origin='studio'`) | Where a delegated task becomes a child run |
| Step AK agent controls (branch `agent/agent-controls`, HEAD `5989d13`, slices 1–5 DONE — Agent 108 finished 2026-11-28 ~07:27 PKT) | `app/agent_controls/` (`evaluator.py`: allow/deny/needs-approval, spend limits, kill-switch, `check_delegation`; `store.py`: `AgentControlRecord` / `AgentControlEvent` / `AgentSpendLedger`, kill cancels queued runs + pending approvals, idempotent spend via `execute_once`; `gate.py`: killed/disabled/paused agents refused at login, re-check, Studio dispatch, governance execution; `unify.py`: **read-only** `LegacyControlView` adapters for the Studio + governance islands — a real control record always wins, a call-window budget is never mapped onto money) | Per-agent enforcement. **AK is the control plane; J is the workforce that is controlled.** |
| Step Q control room (branch `agent/control-room`) | `app/control_room/` overview API | Live status; J's registry feeds it, it does not replace it |
| Step AF live alerts (branch `agent/control-room-live`) | `app/control_room/alerts.py`, charts | Alerting on workforce events |
| Step T model router (branch `agent/model-router`) | `app/services/model_router.py` (`route`, `estimate_cost_usd`) | Cost **estimation** for J's budget checks; J enforces, T prices |
| Step V registry (branch `agent/integrations`) | `app/integrations/registry.py` | Connector registry pattern to copy, not an agent registry |

### What is genuinely new for Step J

1. **Org-wide workforce registry.** Studio agents are per-user (`owner_id` is the
   creator, invisible to the org). AK control records are per-agent *controls*,
   not a registry with an ownership ledger. There is no single place that lists
   every agent, its accountable owner, role, scope and lifecycle state.
2. **Assignment.** Nothing assigns work to an agent. The Studio worker claims the
   oldest queued run for any enabled, unpaused agent — no skill match, no
   capacity check, no cost check. There is no "who should do this task" decision
   anywhere in the code.
3. **Skill registry.** No agent skill exists anywhere (only human expertise).
   The v2 spec requires *verified* skills reusable across departments.
4. **Capacity / concurrency.** No per-agent concurrency, queue depth or load
   concept (only `max_steps` per run). No load balancing across agents.
5. **Delegation policy engine.** Delegation is a parent-configured allow-list
   (`delegate_agent_ids`); the subagent's tools are never re-checked against the
   *subject* of the task (department, document, payment). There is no policy
   object, no delegation audit (parent run → child run → policy version →
   decision). AK's `check_delegation` is a control check (depth limit), not a
   policy engine.
6. **Budget enforcement across entry points.** Cost is *estimated*
   (`estimated_cost_usd` on runs) and **not enforced**. Pause can be bypassed by
   inline/WhatsApp runs (v2 current-state assessment, module 02). No org quotas
   (max agents, total monthly spend).
7. **Owner revocation stops the owner's agents.** `revoke_user_access` stops the
   human; whether a Studio worker claim for that owner's agents is refused is
   unverified — AK gates the agent's own record, not the owner's status.
8. **Workforce reporting.** Analytics are per-user. No org-wide utilization,
   success rate, or cost-per-agent/task view. (Step N Cost Intelligence
   aggregates ROI; J produces the per-agent facts, N consumes them — no
   duplication.)

## 3. Unique value

InfraSpine already has agents (Studio), controls (AK), status (Q/AF) and pricing
(T) — but nobody is *in charge of the workforce*. J is the missing layer: one
registry, one assignment decision, one delegation policy, one budget that stops
work everywhere. Without it, every new agent is another unowned island, and
"pause this agent" is a request, not a guarantee.

## 4. Safety / security rules (non-negotiable)

- Every lookup/dispatch through `app/platform` (`resolve_principal`, `authorize`,
  `require` → 404 non-members, 403 members without permission).
- Documents, web pages, messages, voice transcripts and **task descriptions are
  untrusted text**: text can never grant permissions, change payees, change what
  an approval covers, or raise a delegate's permissions. Assignment and
  delegation match on **structured fields only** (skills, scope, action) — never
  on free text.
- Approvals bound to the exact action hash, expire, single-use, separation of
  duties (originator ≠ approver; `invoice.verify` ≠ `payment.approve` ≠
  `funds.transfer`).
- Side effects through `execute_once` with read-back verification.
- **Never bypass AK controls.** J reads AK status/spend as inputs and asks the AK
  gate to decide; J never re-implements allow/deny, spend limits or kill.
  A call-window budget (`max_calls`) is never treated as money.
- Sensitive tasks are assigned only to agents flagged to handle them; a sensitive
  task is never auto-assigned to an unflagged agent.
- Kill / revoke / retire always leaves an audit trail (who, when, why) and a
  lifecycle history. A killed agent can never revive itself.

## 5. Slices

1. **Assignment evaluator (pure functions) — THIS AGENT.** `app/workforce/assignment.py`:
   deterministic `assign_task()` over a roster — status, scope, action, skills,
   capacity, spend-remaining and task-cost-cap filters, load-balanced ranking,
   deterministic tie-break, full rejection reasons. `skill_match()` helper and
   `validate_roster()` helper. No migration, no route, no production code changed.
   Tests: `tests/test_workforce_assignment.py` (incl. hostile-description cases).
2. **Skill registry — DONE (Agent 121).** Verified skills: `WorkforceSkill`
   (catalog, unique normalized name) + `WorkforceAgentSkill` per-agent rows
   (skill, verified_by, verified_at, expires_at); verification rule is plan
   §7.3 (agent owner, admin, or `workforce.manage` holder — new platform
   permissions `workforce.manage` / `workforce.read`); expiry default 365
   days, expired skills never match assignment. Migration
   `0104_workforce_skills` (down_revision 0103; renumber at merge — 0104 is
   claimed on several sibling branches) + `SkillRegistryService` + tests.
   Deliberate deviation, recorded in the service module: no `execute_once`
   for verification — it is an idempotent, reversible DB state change with
   no external dispatch; the unique constraint + row lock + read-back gives
   the same exactly-once guarantee, and `execute_once`'s duty check cannot
   express the owner-verification rule. Skills never grant permissions.
   AK's `unify.py` read path was read, not forked; slice 3 builds on it.
   Tests (2026-11-28, Agent 121): DB `tests/test_workforce_skills.py`
   19 passed, 0 failed in 3689.31s (1:01:29) on throwaway
   `infraspine_j121_test` (cluster 127.0.0.1:55444); pure regressions
   54 passed (37 slice-1 assignment + 17 skill rules); Alembic roundtrip
   clean — downgrade base → upgrade head → `alembic check` ("No new
   upgrade operations detected.") → `current` 0104_workforce_skills (head).
   Two test bugs fixed along the way (unknown agent kind is
   `InvalidInputError`, not 404; department managers must hold the
   `manager` role) — no production-code change needed for either.
3. **Org registry + lifecycle — DONE (Agent 159).** Unified registry view
   over the three islands (Studio / governance / legacy profiles) built on
   AK's `unify.py` read path — read, not forked (AK's
   `app/agent_controls/` does not exist on this branch, so the view follows
   its pattern: frozen view + notes, real record wins, deny-by-default; every
   view carries the note "AK control record is not joined on this branch; the
   AK join lands at merge"); lifecycle states active/paused/retired with
   allowed transitions active↔paused, active→retired, paused→retired,
   retired terminal; effective status is the most restrictive of registry /
   island / owner-revoked (retired > disabled > paused > active);
   `WorkforceRegistryAgent` org-level lifecycle row + append-only
   `WorkforceLifecycleEvent` history (reason required, audit
   `workforce.lifecycle_*`); `WorkforceRegistryService` + `GET/POST
   /api/workforce/agents…` (list/get/history/pause/resume/retire/
   owner-revocations) gated via `app/platform` (404 non-members, 403
   no-permission). Migration `0105_workforce_registry` (down_revision
   0104_workforce_skills; downgrade refuses if rows exist; renumber at merge
   — 0105 is claimed on several sibling branches).
   THE OWNER-REVOCATION ANSWER: when an agent's owner is revoked/removed
   (`User.is_active` false), the agent is PAUSED, never deleted, and its
   verified skills are FROZEN (freeze = `expires_at` moved to now, so the
   skill reads "expired" and drops out of `current_skills()`; verification
   history `verified_at`/`verified_by` is preserved, so a new owner may
   re-verify later); ownership is NEVER silently reassigned. The read view
   derives the pause even before the hook runs. The hook refuses a
   still-active user and an unknown user. Limitation: profile agents are
   department-owned, so one user's revocation does not pause them.
   Gates (plan §7.7 decided): pause/resume — owner, admin, or
   `agents.manage`/`workforce.manage` holder on org scope; retire — admin or
   manage-holder only (owner may ask, cannot retire); reads —
   `workforce.read`/`workforce.manage`/`agents.manage` or owner/admin.
   Deliberate deviation, same rationale as slice 2: no `execute_once` for
   lifecycle writes — idempotent, reversible DB state changes with row lock
   + read-back; `execute_once` is for irreversible external dispatches.
   Tests (2026-11-28, Agent 159): pure `tests/test_workforce_registry.py`
   14 passed; DB `tests/test_workforce_registry_service.py` 24 passed,
   0 failed (full run 22 passed + 2 fixed and re-verified green);
   regressions — pure 68 passed (37 slice-1 assignment + 17 slice-2 skill
   rules + 14 slice-3 registry), slice-2 DB
   `tests/test_workforce_skills.py` 19 passed, 0 failed (18 in the full run,
   which a VM reboot killed in teardown, + the last test re-run green);
   Alembic roundtrip clean — downgrade base → upgrade head → `alembic
   check` ("No new upgrade operations detected.") → `current`
   0105_workforce_registry (head), on throwaway `infraspine_j121_test`
   (cluster 127.0.0.1:55444; cluster + `agent121` OS user rebuilt twice
   after VM reboots wiped them). Real bug fixed: reading a registry row's
   state AFTER `rollback()` in the refused-transition branch raised
   `MissingGreenlet` (rollback expires the row; expired-attribute access on
   an AsyncSession has no greenlet context) — the state is now captured
   before the rollback. Two test bugs fixed (department managers must hold
   the `manager` role; `current_skills` is a tuple, not a list).
   Env note: the Step E fifth full-suite pass and Agent 161's integration
   run were live during slice-3 testing; the suite ran in one detached
   process (51 min) and the skills regression in one detached process
   (~50 min) — both survived one VM reboot each.
4. **Delegation policy engine — DONE (Agent 176, 2026-11-28).** Versioned
   policy (rules: who may delegate to whom, for which actions/scopes,
   max depth 2) + pure `evaluate_delegation` (active policy → depth →
   child active → rule match → sensitive → tools ⊆ child's own tools →
   separated duties) + `WorkforceDelegationPolicy` /
   append-only `WorkforceDelegationAudit` (migration
   `0106_workforce_delegation`) + service + API + Studio `_delegate`
   gate (denial raises ValueError; policy-check exception fails closed;
   no policy → allowed + audited as `no_active_policy`). Tests incl.
   "text inside the delegated task cannot raise B's permissions" and
   "B cannot use a tool A has if B's own role forbids it" — 16 pure +
   13 DB passed. Deviation: the plan's "child run on the unified engine
   (`origin='workforce'`)" is a MERGE-TIME integration — `wf_definitions`
   admits no 'workforce' origin and a triggered run would double-execute;
   on this branch the child runs inline in the parent's Studio graph
   (audit `child_run_kind='inline'`). Real bug fixed: `depth >
   max_depth` TypeError on non-int max_depth.
5. **Budget enforcement wiring — DONE (Agent 176, 2026-11-28).**
   Daily/monthly spend + call quotas hard-stop at start time (reuse AK
   spend ledger at merge — spend is derived from
   `AgentStudioRun.estimated_cost_usd`, never invented; T cost estimates
   not yet wired) + org quotas; pause/revoke stops queued, running and
   API starts before the next tool call (`start_run` refuses before run
   creation; `execute_claimed` hands refused claims back, queued,
   attempt not counted; worker claim skips paused/retired agents;
   `revoke_user_access` pauses the owner's agents + freezes skills).
   Default USD 10/agent/month (§7.1). Migration
   `0107_workforce_budget`. 10 pure + 15 DB passed. Real bug fixed: the
   revocation hook's module-level entry point
   `apply_owner_revocation_for_platform` was never defined — the broad
   `except` in `revoke_user_access` swallowed the ImportError and the
   hook silently did nothing.
6. **Workforce reporting — DONE (Agent 176, 2026-11-28).** Read-only
   org-wide utilization, success/failure, cost per agent/task
   (`agent_report` / `org_report` / `spend_report`, feeds Step N);
   governance/profile agents show zero runs with an explicit note, never
   estimated. API `GET /report/org|spend|agents/{kind}/{id}`. 6 DB tests
   passed.
7. **Admin UI + API — DONE (Agent 176, 2026-11-28).** `WorkforcePage`
   (Registry tab: search/filter, lifecycle pause/resume/retire with
   required reason, skills attach/verify/revoke/remove, budget form +
   can-start check, 30-day report, lifecycle history; Delegation tab:
   policy list + create-and-activate, delegation audit; Spend tab: org
   quota form, org stats, per-agent spend) + skill routes the slice-2
   service never got (`GET/POST /skills`, agent-skill attach/verify/
   revoke/remove). Admin-only `#/workforce`. Frontend: 7 new node tests
   (29 total), `tsc --noEmit` clean, `vite build` exit 0; 3 DB tests
   passed for the skill routes. Real bug fixed while wiring: skill
   status is `unverified`/`verified`/`expired` — the page first used
   nonexistent `pending`/`revoked` labels.
8. **CHANGES-step-J.md + gates + merge readiness — DONE (Agent 176,
   2026-11-28).** `assessment/CHANGES-step-J.md` (plain language, file
   list, test counts, zero [PENDING]), Alembic roundtrip + check green
   (single head `0107_workforce_budget`), frontend gates green, pure
   regressions 94 passed, slices 1–3 DB re-run green (51 passed in one
   process), full-backend-suite gate owed (load control). Merge readiness
   note: migrations 0104/0105/0106/0107 all collide — renumber at merge.

## 6. Acceptance tests (from master-plan-v2 §1–§2 — must all pass before merge)

- Creating an agent requires an accountable owner, a role, a scope and a tool
  list; it is visible in the org registry, not only to its creator.
- A daily/period budget (tokens or cost) hard-stops the next run; a further
  WhatsApp or inline run is also refused.
- Pause and revoke stop queued, running, WhatsApp and API starts before the next
  tool call; credentials issued to that agent fail.
- Disabling or retiring an agent leaves an audit trail (who, when, why) and a
  lifecycle history.
- One `revoke_user_access` on the owner also stops that owner's agents from acting.
- Agent A can delegate to B only if B is allowed and B's `authorize()` permits
  that action and scope; otherwise the child run never starts.
- B cannot use a tool A has if B's own role forbids it.
- Text inside the delegated task cannot raise B's permissions.
- The same person/agent cannot both originate and approve a separated duty.
- Audit shows the parent run, child run, policy version and decision.

## 7. Owner decisions needed (safe defaults in place)

1. Default spend limit per agent (AK proposes USD 10/month — confirm for J too).
   SETTLED in slice 5 (Agent 176, 2026-11-28): USD 10/month default implemented.
2. Default max concurrent tasks per agent (slice 1 uses the agent's own
   `max_concurrent`; propose default 1 until the owner says otherwise).
   SETTLED: default 1 (slice 1).
3. Who may verify an agent skill (proposed: owner of the agent + an admin).
4. Sensitive-task flag policy: which agents may handle sensitive tasks, and who
   grants the flag (proposed: admin only).
5. Org quotas: max agents in the registry, total monthly workforce spend cap
   (proposed: none until the owner sets them — per-agent limits still apply).
6. Delegation max depth (AK `check_delegation` takes a depth; propose 2).
   SETTLED in slice 4 (Agent 176, 2026-11-28): max depth 2 implemented.
7. Lifecycle: who may retire an agent — its accountable owner, or admin only
   (proposed: admin only; owner may request retirement). DECIDED in slice 3
   (Agent 159, 2026-11-28): retire is admin or `agents.manage` /
   `workforce.manage` holder only; owner may pause/resume but cannot retire.
   Owner-revocation answer (slice 3): revoked owner → agent PAUSED, verified
   skills FROZEN, ownership never silently reassigned; profile agents are
   department-owned, so one user's revocation does not pause them.

## 8. Merge notes

- AK's `unify.py` (Agent 108, DONE) is the read path slice 3 builds on —
  coordinate at merge; do not fork it.
- Step T cost estimates feed slice 5; Step N consumes slice 6's per-agent facts.
- Migration numbers 0104/0105 are claimed by HR, WhatsApp AE, AI freshness, AK,
  AB and others — renumber at merge.
- `app/control_room/__init__.py` is created by both Step Q and Step AF — J must
  not create a third copy; reconcile at merge if J touches it.
