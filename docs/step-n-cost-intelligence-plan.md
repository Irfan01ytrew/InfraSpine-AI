# Step N — Cost Intelligence and Control Plane (plan)

**Status:** slices 1–4 done (Agent 126, Agent 128, Agent 134, Agent 142). Slices 5–8 remain.
**Branch:** `agent/stepn` · **Base:** `step-e-gaps-wip` `ca956c2` · **v2 estimate:** 16–24h
**v2 source:** `assessment/master-plan-v2.md` §1 step table (step N, features 13 + 14)
and §2 features 13 (Agent Economy / Cost Intelligence) and 14 (Enterprise Digital
Control Plane).

## 1. What Step N is (master-plan citation)

Step N is the **last** of the v2 new steps J–N. The v2 step table defines it as:

> **N. Cost intelligence and control plane** — ROI, cost per task, success/failure
> rates, human vs AI. One admin control plane for integrations, agents, workflows,
> knowledge, identities and policies. (Features 13, 14.)

It has **two halves that share one rule — numbers come from ledgers, and a control
change made in the plane is the same change the API enforces**:

- **Feature 13 — Agent Economy / Cost Intelligence (plain language).** For each
  agent: ROI, cost per task, success rate, failure rate, and a comparison with human
  effort where the company records it.
- **Feature 14 — Enterprise Digital Control Plane (plain language).** Integrations,
  agents, workflows, knowledge, identities and policies are managed from one central
  layer, with the same permissions everywhere.

### Letter-mapping note (recorded so nobody re-litigates it)

The v2 doc letters J–N cover 14 features, not five. The team's working mapping
drifted from the v2 table: team K is the twin only (v2 K also held prediction and
simulation), team L is Predictive Continuity (v2 L is Memory and Risk), team M is
Decision Memory (v2 M is Autonomous Ops / self-healing / verification). The
**remaining unowned v2 content is exactly features 13 + 14**, which the v2 table
assigns to step N. Nothing else claims N. Step N therefore covers features 13 + 14
and nothing else; the other v2 features already have owners (see §2).

## 2. Overlap check (read from code, base `ca956c2` + sibling branches)

### Exists — reuse, don't rebuild

| Thing | Where it lives | What N reuses |
|---|---|---|
| Per-run usage | `AgentStudioRun` (`app/db/agent_studio_models.py`): `prompt_tokens`, `completion_tokens`, `llm_calls`, `estimated_cost_usd` (Numeric 18,8), `usage_by_model` JSONB | The ledger slice 1 reads. Cost is *estimated* per run, never a model claim. |
| Per-user analytics | `app/agent_studio/analytics.py` (`agent_studio_analytics`, per-agent summary): run counts, durations, tokens, `estimated_cost_usd` totals, `success_rate` (`completed / terminal`, `None` when no terminal runs) | The per-user pattern; N rolls the same facts up org-wide. |
| Analytics UI | `frontend/src/components/AgentStudioAnalyticsPanel.tsx` ("Estimated model cost", "runs have no configured rate", "Provider invoice costs are not inferred from token counts") | The honesty convention: unpriced runs are shown as unpriced, never as zero. |
| Token pricing | `app/services/operational_health.py` (`PROVIDER_RATES` per-1k table, `calculate_token_cost` — float, returns `0.0` for unknown provider; `get_provider_costs` from `RagTrace`; `get_operational_dashboard`) | Raw price data only. N never trusts a float estimate as a ledger fact; slice 2 reconciles it against run records. |
| Rate card | `Settings.llm_model_pricing_json` (`"provider:model"` → input/output USD per 1M tokens) | The admin-configured rate card; see owner decision §7.1. |
| Human budgets | `app/services/budgets.py` (`BudgetUse`, `check_budgets_for_approval`, `BudgetService` — `block`/`warn` enforcement) | Human spend only, not agent ROI. The `block`/`warn` pattern is the model for budget surfacing (slice 7). |
| Platform core | `app/platform/*` — principals, scopes, memberships, approvals, execution, outbox, org settings (`docs/platform-core.md`) | The real control layer. Every N read/write goes through `require()` / `visible_scopes`. |
| Module Control Center UI | `frontend/src/components/ModuleControlCenterPage.tsx`, `ModuleWorkspaces.tsx`, `docs/infraspine-master-control-center.md` | A specialist-chat desk, **not** a control plane. N's surface replaces its role for admin control; the page itself is untouched. |
| Admin console / access admin | `frontend/src/components/AdminConsole.tsx`, `app/api/routes/access_admin.py`, `admin_reports.py` | Existing admin surfaces N's plane links into; N does not fork them. |
| Connectors | `app/api/routes/cloud_connectors.py`, migration `0096_integration_connectors.py` | Integration connections listed by the plane (slice 4). |

### Exists on sibling branches — integrate with, don't duplicate

| Sibling | Status | Boundary with N |
|---|---|---|
| Step T model router (Agent 25/59/105, `agent/model-router`) | Slices 2–3 in progress: `route` / `estimate_cost_usd`, routing records + cost/latency report + budget guard | **T prices; N accounts.** T estimates a call's cost before it runs; N totals what runs actually cost from ledgers and reports ROI. T's routing records are one of N's ledger inputs (slice 2). |
| Step AK agent controls (Agent 47/56/108, `agent/agent-controls`, slices 1–5 DONE) | Evaluator, store (`AgentControlRecord` / `AgentControlEvent` / `AgentSpendLedger`, idempotent spend via `execute_once`), gate, kill-switch, `unify.py` read-only legacy-island views | **AK enforces; N reports.** Spend limits, kill-switch and idempotent spend stay in AK. N reads the AK spend ledger as a ledger input and never re-implements enforcement. N's plane *displays* AK controls and routes changes through AK's API. |
| Step J AI Workforce OS (Agent 116/121, `agent/workforce-os`) | Slice 1 assignment done; slice 2 skill registry in progress; slices 3–8 planned incl. budget enforcement (slice 5) and workforce reporting (slice 6) | **J assigns and enforces; N accounts.** J's plan states explicitly: "No org-wide utilization, success rate, or cost-per-agent/task view. (Step N Cost Intelligence consumes J's per-agent facts, no duplication.)" N consumes J's per-agent facts; J's slice 6 and N's slice 3 must agree on the same figures (differential test at merge). |
| Step Q control room (Agent 22/88) + AF live (Agent 39/50/91) | Status API, charts, alerts, dispatch worker in progress | **Q/AF show live health; N shows money and control.** Q/AF own "what is happening now"; N owns "what did it cost, was it worth it, and who may change what". N's plane links to the control room; it does not re-render it. |
| Step V integrations (Agent 26/75/78/84, slices 1–8 DONE) | Registry, Slack/Teams/Gmail/Drive/ERP/bank connectors, admin connectors page | V owns connector *behaviour*; N's plane lists connection health/status from V's registry (read-only). |
| Step W self-healing (Agent 29) | Detector slice done | W owns fault detection/remedy; N does not touch it. Cost of a remedy is a ledger input only if W records it. |

### Genuinely new for N

1. **ROI per agent** — (human cost − AI cost) / AI cost, with human cost *known*
   only when the company recorded human time **and** a rate. No human input means
   ROI is `unknown`, never zero, never a guess.
2. **Cost per successful task** — total known cost / completed tasks. Nothing in
   the code computes it today (analytics has totals and a success rate, never the
   ratio).
3. **Human-vs-AI comparison** — per agent/task class, with the "unknown" state
   carried through every roll-up (a department with one unknown agent is not
   reported as fully known).
4. **Org-wide roll-up by agent/department from ledgers** — analytics today is
   per-owner Studio views; no org-wide cost view exists.
5. **Unified admin control plane** — one surface over identities/memberships,
   policies, agents, workflow templates, knowledge ACLs and integration
   connections, where a change made there **is** the API-enforced change (no
   second, weaker path). Today the Module Control Center is a chat desk, and each
   admin surface enforces its own slice.
6. **Control-action audit with before/after** — every control change audited with
   actor, exact action hash where applicable, and before/after values. (Platform
   audit rows exist; the plane's uniform before/after record does not.)

## 3. Safety and security controls (non-negotiable)

1. **Advisory only where money is reported.** A cost figure never triggers an
   action, payment, or permission change by itself. Enforcement stays with J/AK
   (budgets, kill-switch); N only surfaces the numbers and the alert.
2. **Numbers come from ledgers, not from the model.** Cost figures are sums of
   recorded run/ledger rows. A run with no configured rate contributes to
   `runs_without_cost`, never to a zero cost. A missing human-time input is
   `unknown`, never zero cost.
3. **Money is `Decimal`, never float.** The existing `calculate_token_cost`
   returns float estimates; N treats those as *estimates* and never writes them
   into a ledger as fact. All N arithmetic is `Decimal`.
4. **Scope isolation.** Every figure is computed per scope; one department cannot
   see another's spend. Roll-ups group by scope id and never mix scopes.
   Non-admins see only the `visible_scopes` slice on the control plane.
5. **Every lookup/dispatch through `app/platform`.** `resolve_principal` /
   `authorize` / `require` — 404 for non-members, 403 for members without the
   permission. No direct table reads in API handlers.
6. **Approvals bound to the exact action.** Any control-plane change that is
   privileged (permission/policy change, agent lifecycle, integration secret
   rotation) goes through an exact-action approval (hash, expiry, single-use,
   separation of duties) and `execute_once` with read-back verification.
7. **Text is untrusted data.** Task descriptions, run notes, agent names and
   human-time notes are never read for decisions and never executed; the cost
   engine reads numeric/status fields only. Injection text in a note changes
   nothing (tested).
8. **Audit everything the plane changes.** Actor, exact action hash where
   applicable, before/after values, timestamp. No silent control change.

## 4. MVP scope

- Cost/ROI figures per agent and per department, org-wide roll-up, from the
  existing run/ledger records; human-vs-AI comparison with honest `unknown`.
- One admin surface (read first, writes second) listing identities/memberships,
  policies, agents, workflow templates, knowledge ACLs, integration connections —
  each row linking to the surface that already manages it, then (slice 5) acting
  through the same enforcement path.
- No new money movement, no new permissions model, no new LLM provider, no
  re-pricing of past runs. N reports and controls; it does not spend.

## 5. Slices (numbered, each independently testable)

1. **Pure cost/ROI engine** — `app/cost_intelligence/figures.py` (stdlib only):
   per-agent/department figures, cost per task, cost per successful task,
   success/failure rates, spend vs budget, human-vs-AI comparison with
   `unknown`, scope-grouped roll-up, deterministic fingerprint. **DONE (this
   agent).**
2. **Cost ledger service** — read-only aggregation over `AgentStudioRun` +
   AK `AgentSpendLedger` + T routing records; reconcile estimated vs recorded
   cost, count unpriced runs; org-scoped service through `app/platform`. A
   snapshot/cache table only if measurement proves the aggregation is too slow
   (migration then; otherwise no migration). **DONE (Agent 128).**
   `app/cost_intelligence/ledger.py`: `scope_ledger()` (figures + per-agent
   reconciliation + routing comparison + provenance in `sources` + SHA-256
   fingerprint), `reconcile()` (pure), `compare_routing_estimates()` (pure),
   `scope_member_user_ids()` / `department_for_owner()` /
   `attributed_owner_ids()` / `scope_agent_ids()` / `studio_run_records()`.
   AK + T are unmerged on this base, so their readers raise
   `LedgerSourceUnavailable` (never a silent zero) and `scope_ledger`
   degrades gracefully, recording them as unavailable. **Attribution rule
   (found by the DB tests, not assumed):** a run belongs to exactly one
   department — its owner's first (by name) active department membership,
   the platform translation of
   `app.agent_studio.unified.scope_for_owner`; org sees every run;
   workspaces under a department see their own members whose first
   department is that parent; `module_space` raises `ValueError`. No
   migration in this slice.
3. **Cost API** — `GET /api/cost-intelligence/scopes/{scope_id}/summary` (+
   per-agent, per-department), platform-gated (404/403), scope-scoped,
   zero-side-effect proof (a GET writes nothing). **DONE (Agent 134).**
   `app/api/routes/cost_intelligence.py`: summary (full `ScopeLedger`
   incl. `sources` provenance + fingerprint), `/agents` (per-agent
   figures + reconciliation), `/agents/{agent_id}` (single agent, 404
   when the agent has no runs/recorded spend in the scope),
   `/departments` (per-department figures via the new
   `ledger.department_scopes_for()` helper — org → every department,
   department → itself, workspace → nearest department ancestor).
   Gated by `platform.require(db, p, "cost.read", scope_id)`; the
   permission is registered as `PERMISSION_COST_READ` in
   `app/core/permission_catalog.py` (also in `DEPARTMENT_CAPABILITIES`,
   so department managers can hold it). Deliberately **not** added to
   the legacy-admin bridge: the bridge comment states business
   permissions are never implied by the legacy admin role — the owner
   holds `cost.read` via a `finance` role granted on the org scope,
   which the scope chain then covers on every descendant scope (§7.3).
   `module_space` is 404 (no cost view there); a scope too large to
   report (over `MAX_RECORDS` runs) is 422, never a silent truncation.
   No migration in this slice.
4. **Control-plane inventory (read-only)** — unified read model over
   identities/memberships, policies, agents, workflow templates, knowledge
   ACLs, integration connections; AK `unify.py` pattern (read-only views, a real
   control record always wins, never map a call-window budget onto money).
   **DONE (Agent 142).** `app/cost_intelligence/inventory.py`:
   `control_inventory(db, scope_id)` (async service) + pure view builders
   (`studio_agent_view`, `governance_agent_view`, `ak_record_view`,
   `effective_agent_view`, `inventory_fingerprint`); six sections
   (identities/memberships, channel identities, policies/roles, agents,
   workflow templates, knowledge ACLs, integration connections). AK
   `AgentControlRecord` unmerged on this base → `sources.ak_controls` =
   `"unavailable:..."` (never silent); call-window budget shown as
   `call_budget` only, `spend_limit_usd` is always `None`; no money
   fields anywhere. `GET /api/cost-intelligence/scopes/{scope_id}/inventory`
   gated by the new `PERMISSION_CONTROL_READ` (`control.read`, also in
   `DEPARTMENT_CAPABILITIES`, deliberately **not** in the legacy-admin
   bridge — same reasoning as `cost.read`). Org scope = whole company;
   department connectors follow the sync-mirrored department name;
   knowledge is aggregate counts only (no content/filename/storage key);
   no credentials/cursors ever listed; section cap `MAX_SECTION_ROWS =
   5000` refuses truncation, never silent. No migration in this slice.
5. **Control-plane writes** — permission/policy/agent-lifecycle changes through
   the same enforcement path the API uses (no second path); exact-action
   approval where privileged; audit with actor + action hash + before/after.
6. **Admin UI** — cost dashboard + control-plane surface; non-admin sees only
   the `visible_scopes` slice; money rendered at 4dp, `unknown` shown as
   "unknown", never zero.
7. **Budget surfacing** — warn-at-80% / over-budget states from J/AK budgets
   via the existing watch/alert pattern (dedupe + cooldown + quiet hours);
   N adds the cost side only, never its own enforcement.
8. **`CHANGES-step-N.md` + gates + merge readiness** — plain-language summary,
   file list, exact test counts, full backend suite + Alembic + frontend gates.

## 6. Acceptance tests (v2 §2, features 13 + 14)

- For seeded runs, the dashboard shows cost per task, success and failure rates,
  and spend vs budget, matching the sum of run records.
- A missing human-time input is shown as "unknown", not as zero cost.
- Figures are tenant/scope scoped; one department cannot see another's spend.
- Numbers come from ledgers, not from the model's own claim.
- From one admin surface, an operator can view and (when permitted) change:
  identities/memberships, policies, agents, workflow templates, knowledge
  sources/ACLs, and integration connections.
- A permission change there is the same change the API enforces (no second,
  weaker path).
- Every control action is audited with the actor, exact action hash where
  applicable, and before/after.
- A non-admin sees only the slice `visible_scopes` allows.

## 7. Owner decisions needed (safe defaults in place)

1. **Authoritative rate card.** `LLM_MODEL_PRICING_JSON` (admin-configured,
   per provider:model) vs `operational_health.PROVIDER_RATES` (hardcoded
   per-1k table). Proposed: the admin-configured card wins when present;
   hardcoded table is a fallback estimate only, flagged as estimated.
2. **Human-time recording.** Who records human minutes per task, and the default
   hourly rate (if any). Proposed: no default rate — without a recorded rate the
   human cost is `unknown`, never imputed.
3. **Who sees org-wide cost.** Proposed: owner + finance role (`finance.read`
   on the scope); department managers see their own department only.
4. **Budget breach behaviour for the dashboard.** Proposed: warn only — hard
   enforcement stays with J/AK; N never blocks a run.
5. **Cost snapshot retention.** Proposed: 90 days of daily roll-ups, then
   monthly aggregates only.

## 8. Merge notes

- **J slice 6 vs N slice 3 must agree.** J's workforce reporting and N's cost
  API compute the same per-agent figures from the same facts; add a differential
  test at merge proving identical totals.
- **AK spend ledger is the money source of truth** for enforced spend; run
  `estimated_cost_usd` is an estimate. Slice 2 must document which figure each
  API field comes from.
- **Migration numbers:** 0104 is claimed 4× and 0105 3× on other branches; mint
  the next free number at merge time. Slices 1–3 mint no migration.
- **ModuleWorkspaces / AdminConsole conflicts:** slices 5–6 touch admin
  surfaces also touched by the frontend redesign rounds — reconcile at merge,
  styling only, no behaviour change.
- **No live behaviour change in slice 1** — pure functions, no route, no
  migration, no production code changed.

## 9. Slice 1 test counts

- `tests/test_cost_intelligence_figures.py`: **25 passed, 0 failed** (run twice,
  `--noconftest`; first run 10.20s, second 1.71s). Exact command:
  `PYTHONPATH=<worktree> /home/hatch/workspace/infraspine/work/digital-twin-v2/.venv/bin/python -m pytest tests/test_cost_intelligence_figures.py -q -p no:cacheprovider --noconftest`
  (system python3 has no pytest; reused Agent 118's venv, as Agents 119/122 did).
  One test-fix round: 1 failure was a test bug (helper kwarg name); product code
  unchanged, full green on re-run.
- Coverage: per-agent figures, cost per task / per successful task, success and
  failure rates, spend vs budget (ok/warn/over, no-budget), human-vs-AI
  (known/partial/unknown — never zero), scope roll-up isolation, fingerprint
  determinism, validation errors, size caps, hostile text as data.

## 10. Slice 2 test counts

- `tests/unit/test_cost_intelligence_ledger_pure.py`: **14 passed, 0 failed**
  (`--noconftest`). Reconciliation statuses (matched / estimated_higher /
  recorded_higher / recorded_unavailable / no_recorded_spend / recorded-only
  agent), variance + variance-pct math (incl. zero recorded → no pct),
  validation errors (float/str/negative/bool money, bad counts), routing
  comparison (matched/mismatched, unpriced + missing runs skipped, hostile
  notes ignored), both unavailable-source errors, JSON-safe rendering.
- `tests/test_cost_intelligence_ledger.py`: **8 passed, 0 failed** in 345.03s
  (throwaway `*_test` DB on a private PostgreSQL 16 cluster,
  127.0.0.1:55498, DBs `infraspine_n128` / `infraspine_n128_test`).
  Exact command: `DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55498/infraspine_n128
  TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55498/infraspine_n128_test
  <backend-tests venv>/bin/python -m pytest tests/test_cost_intelligence_ledger.py -q
  -p no:cacheprovider` (with `TMPDIR`, `HF_HUB_OFFLINE=1`,
  `TRANSFORMERS_OFFLINE=1`, sanitized `NO_PROXY`).
  Coverage: active/revoked/expired memberships; run mapping + cross-scope
  exclusion (HR run and outsider run never leak into Finance); since-filter
  + limit-refuses-truncation; end-to-end ledger (5 runs, total 4.2500,
  4 known / 1 unknown, cost/task 1.0625, cost/success 1.4167, 75%/25%
  rates, human unknown, AK + T recorded as unavailable not zero,
  fingerprint stable across calls, JSON-safe); empty scope (zero runs,
  zero cost); hostile agent name/description changes nothing;
  first-department-by-name attribution (granting Finance to an HR member
  moves their runs to Finance and empties HR; org still sees all 7 runs,
  total 20.2500); bad scope id / unknown scope / `module_space` rejected.
- Slice-1 regression re-run with slice 2: `tests/test_cost_intelligence_figures.py`
  **25 passed, 0 failed** (`--noconftest`).
- **Alembic: `check` = "No new upgrade operations detected."** No migration
  in this slice (head stays `0103_unified_studio_governance`).
- One real bug found by the first DB run (5 failed): owner-membership
  attribution counted a shared member's HR run in Finance — fixed by
  deriving attribution from the owner's first department
  (`department_for_owner`, the platform translation of
  `app.agent_studio.unified.scope_for_owner`), proven by the
  first-by-name test above. No production behaviour besides this module
  was touched.
- Environment: private cluster `/home/hatch/tmp-pg-n128/cluster`, port
  55498, OS user `n128`, superuser `postgres` (trust auth, throwaway
  data only), `fsync/synchronous_commit/full_page_writes` off, socket dir
  `/home/hatch/tmp-pg-n128/sockets` (the shared `/var/run/postgresql`
  was not writable after a restart — same incident Agent 111 hit).

## 11. Slice 3 test counts

- `tests/test_cost_intelligence_api.py`: **18 passed, 0 failed** in
  1056.39s for the full file (one test fixed and re-run green in
  177.57s: the org roll-up assertion was a test bug — the org sees every
  run including the outsider's, per the ledger docstring; product code
  unchanged). Throwaway `*_test` DB on the same private cluster as
  slice 2 (127.0.0.1:55498, DBs `infraspine_n128` /
  `infraspine_n128_test`). Exact command:
  `DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55498/infraspine_n128
  TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55498/infraspine_n128_test
  <backend-tests venv>/bin/python -m pytest tests/test_cost_intelligence_api.py -q
  -p no:cacheprovider` (with `TMPDIR`, `HF_HUB_OFFLINE=1`,
  `TRANSFORMERS_OFFLINE=1`, sanitized `NO_PROXY`).
  Coverage: summary figures match the slice-2 ledger exactly (Finance 5
  runs / 4.2500 / 4 known + 1 unknown / 75% + 25% / cost-per-task 1.0625
  / cost-per-success 1.4167; org 7 runs / 20.2500 incl. the outsider's
  7.0000); `sources` map shows AK + T as `unavailable:` (never zero),
  `recorded_total`/`routing` are `None`; Finance member reads own
  department (200); HR member gets 404 on Finance; member without
  `cost.read` gets 403; baseline org membership alone gets 403 on the
  org summary; unknown scope 404; `module_space` 404; workspace summary
  uses the parent department's figures; `since` filter (tomorrow → 0
  runs, yesterday → 5); unauthenticated 401; per-agent list + detail
  (unknown agent 404); per-department list (org → Finance + HR with
  exact figures; department → itself); zero-side-effect proof (row
  counts identical across 12 tables after three GETs).
- Slice-1 regression re-run with slice 3:
  `tests/test_cost_intelligence_figures.py` **25 passed, 0 failed**
  (`--noconftest`).
- Slice-2 regression re-run with slice 3:
  `tests/unit/test_cost_intelligence_ledger_pure.py` **14 passed, 0
  failed** (`--noconftest`); `tests/test_cost_intelligence_ledger.py`
  **8 passed, 0 failed** in 181.46s. (One run passed `--noconftest`
  to all three files by mistake: the 8 DB tests errored on the missing
  `clean_tables` fixture — re-run without the flag, all green.)
- **Alembic: `check` = "No new upgrade operations detected."** (run
  with `DATABASE_URL` pointed at the migrated `*_test` DB; the
  un-migrated throwaway app DB reports "not up to date" because it was
  never migrated — pre-existing, unrelated to this slice.) No migration
  in this slice (head stays `0103_unified_studio_governance`).

## 12. Slice 4 test counts

- `tests/unit/test_cost_intelligence_inventory_pure.py`: **12 passed, 0
  failed** (`--noconftest`, 22.49s). View builders (studio/governance/AK
  records, effective view precedence, money never mapped from a call
  budget), fingerprint determinism, section-cap refusal, AK-unavailable
  source note, hostile text as data.
- `tests/test_cost_intelligence_inventory.py`: **7 passed, 0 failed** in
  179.43s (full file, throwaway `*_test` DB on the same private cluster
  as slices 2–3, 127.0.0.1:55498, DBs `infraspine_n128` /
  `infraspine_n128_test`). Coverage: org inventory lists everything
  (identities incl. the outsider's baseline org membership, channel
  identities, policies incl. `control.read` on the `controller` role,
  agents from all three islands, templates, knowledge ACL counts,
  integrations with no credential material); Finance inventory is
  scoped (own members/agents/connectors only); HR member gets 404 on
  Finance; member without `control.read` gets 403; unknown scope 404 +
  `module_space` 404; unauthenticated 401; fingerprint stable across
  two GETs + zero-side-effect proof (row counts identical across 12
  tables after three GETs). Three test bugs fixed on the way (governance
  identity needs its `security_members` row flushed first; quarantined
  document needs `acl_quarantine_reason`; the org list correctly
  includes the outsider's baseline membership; the secret-field
  assertion now checks exact field names, not substrings, because the
  sources prose itself mentions credentials/cursors as never listed).
  Product code unchanged by all four fixes.
- Slice-1 regression re-run with slice 4:
  `tests/test_cost_intelligence_figures.py` **25 passed, 0 failed**
  (in the combined run below).
- Slice-2 regression re-run with slice 4:
  `tests/unit/test_cost_intelligence_ledger_pure.py` **14 passed, 0
  failed** (`--noconftest`, re-run standalone after the combined run
  was killed); `tests/test_cost_intelligence_ledger.py` **8 passed, 0
  failed** (in the combined run below).
- Slice-3 regression re-run with slice 4:
  `tests/test_cost_intelligence_api.py` **18 passed, 0 failed** — NOT
  in one process: the combined run (figures + ledger + API) was
  OOM-killed at 44/65 dots (25 figures + 8 ledger DB + 11 API, 0
  failures), then a VM restart killed the one-at-a-time driver at 8/18
  API passed; a second driver finished the remaining 10/10, all
  exit=0. Per-test driver logs: `/home/hatch/tmp-agent142-api-regr.log`
  (8 passed) + `/home/hatch/tmp-agent142-api-regr2.log` (10 passed).
- **Alembic: `check` = "No new upgrade operations detected."** (run
  with `DATABASE_URL` pointed at the migrated `*_test` DB, head
  `0103_unified_studio_governance`.) No migration in this slice.
- Environment: private cluster `/home/hatch/tmp-pg-n128/cluster`, port
  55498, OS user `n128` uid 1014 — recreated after a VM restart wiped
  it (data dir survived; cluster restarted with system pg_ctl 16.15).
  Env file `/home/hatch/.test-env-n142.sh` (chmod 600).
