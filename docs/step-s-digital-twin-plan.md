# Step S — Company Digital Twin: implementation plan

Status: **preparation + first safe slice** (branch `agent/digital-twin`, base
`step-e-gaps-wip` `ca956c2`). Step S is the new futuristic step O–W in the
master plan (`MASTER-PLAN.md`: "live model of teams, money flow, and what
breaks if someone is away"). It is the **preparation and foundation slice for
master-plan v2 Step K** (Digital twin, prediction, simulation, 28–40 h) — not
a second, competing twin. The full K build (continuous live sync, horizon
predictions, what-if engine, twin UI) remains K's scope; this document maps
what already exists, what is missing, and the order the rest is built in.
It does not change any live behaviour by itself.

Sources: `assessment/master-plan-v2.md` §4 (Live Company Digital Twin), §5
(Predictive Continuity), §8 (Simulation / What-if Engine), §1 (Step K table),
`HANDOVER-PROMPT.md` §4–§5, `docs/step-g-continuity-plan.md` (Agent 16, Step G
prep — the graph-merge foundation S/K build on), and the code survey below
(all paths relative to the repo root).

## 1. What Step S / K must deliver (acceptance)

Taken from v2 §4/§5/§8 acceptance tests, restated for the twin:

1. **One continuously updated model.** People, teams, processes, systems,
   suppliers, data, agents, workflows, invoice batches and payments, and the
   links between them — one entity id, one scope. Adding or revoking a
   person, agent, system or supplier updates the twin without a manual
   redraw (outbox event → graph update). The same entity is never duplicated
   across the old module tables and the twin.
2. **"What breaks if X is away?"** For a seeded employee, system and
   supplier, "unavailable 7 days" returns a stable, numbered process list
   (e.g. 14 processes) with owners, dependencies, and money at risk. A
   process the caller is not allowed to see is omitted, and the count is
   not leaked. Removing a `backup_for` link or a permission changes the
   prediction on the next refresh. Predictions are explainable (which links
   and which live work items produced each impact).
3. **What-if simulations, side-effect free.** "What if the Finance Manager
   is removed?", "What if the ERP is down?", "What if a supplier fails?"
   return impact (entities + processes) and a recovery plan (owners, grants,
   steps). Running a simulation writes no `platform_action_executions` in
   the dispatched state and does not change production records. The same
   simulation is deterministic for the same twin version. A recovery drill
   (Step G) can be compared to the simulation; differences are listed.
4. **Freshness is visible.** A stale source is marked stale; a query says
   when the twin was last confirmed. No cross-tenant or out-of-scope node
   is visible to a restricted user (same `visible_scopes` as the rest of
   the platform).
5. **Money flow is part of the model.** Invoice batches (`fin_invoice_batches`,
   statuses open → verifying → awaiting_approval → paying → completed) and
   payments (`fin_payments`, `amount > 0`, awaiting_approval → approved →
   dispatched → verified) appear as twin nodes with amounts, so "away"
   answers include money at risk — never a payment instruction. The twin
   **simulates only**: it never approves, dispatches, or pays anything.

## 2. What already exists (do not rewrite)

| Area | Existing code | Notes |
|---|---|---|
| Company graph | `app/company_graph/*` (`company_graph_records` entities + relationships, composite tenant FKs, governed proposals + approval, `service.projection` / `neighborhood` / `events` outbox journal, `discovery.synchronize` with idempotency keys, `schemas.Simulation` with kinds resignation / absence / system_outage / supplier_failure / restructuring) | Second graph, second tenant table. Mostly manual/API-ingested, not fed live by HR, workflow, agent activity or connectors. |
| Graph impact | `app/company_graph/intelligence.py` (`impact()` forward/reverse BFS depth 6, `backup_for` suggestions, path confidence, `quality()` gaps/stale/conflicts, `query_plan` / `answer_query`) | Outage semantics ("failed node"), not person-absence semantics. No money amounts, no time horizon, no workflow linkage. |
| Continuity graph + BIA | `app/continuity/*` (`bc_nodes`, `bc_links`, `bc_scenarios`, `intelligence.bia_score` / `analyze` / `handover_draft`, 12 scenario kinds, recovery waves, skill-matched backup candidates) | Static, separately maintained graph. `backup_for` is a recorded link only — no effective-permission check. Activation is its own code path, not the unified engine (Step G fixes this). |
| Process intelligence | `app/process_intelligence/*` | Process mining exists but is not wired to real workflow/agent runs (v2 §4 gap). |
| Workflow runs (live-work feed) | `app/db/workflow_engine_models.py` (`WorkflowRun`), `app/workflows/unified.py` (unified engine), finance `start()` returns `(FinInvoiceBatch, WorkflowRun)` | Runs exist; nothing feeds them into either graph today. |
| Finance money flow | `app/db/finance_models.py` (`FinInvoiceBatch`, `FinInvoice`, `FinPayment` with amounts; `app/finance/service.py` batch lifecycle) | Money data exists; no graph edges from person → approval → payment. |
| Frontend | `frontend/src/company-graph`, `frontend/src/continuity` | Graph/plan UI exists; no twin view, no freshness indicator, no money-at-risk view. |
| Tests (thin) | `tests/test_company_graph.py` (12), `tests/test_continuity.py` (4) | No twin, absence-simulation, or money-flow tests exist. |

## 3. What is missing (the Step S/K work list)

1. **Graph merge (Step G foundation, reused here).** One entity id + one
   scope across `company_graph_records` and `bc_nodes`; seed from live
   memberships; outbox event → graph update; stale sources marked stale.
   No cross-tenant/out-of-scope node visible to a restricted user. (Step G
   work list item 1 — S/K must not build a third graph.)
2. **Live sync feeds (the "continuously updated" part).** Outbox-driven
   updaters from: platform membership changes (person added/revoked),
   `WorkflowRun` lifecycle (run started/paused/completed/failed →
   workflow/process node freshness), finance events (batch status changes,
   payment approved/dispatched/verified → money-flow node amounts),
   Agent Studio runs, and connector syncs. Each feed is idempotent
   (idempotency key + digest, same pattern as
   `company_graph/discovery.synchronize`) and writes through the
   company-graph governed-change path, never directly into tables.
3. **Person-absence simulation (first slice — implemented now, §5).**
   Absence semantics on top of the existing impact math: absent person →
   owned/operated/approved entities at risk → downstream processes and
   money at risk, with backup coverage and a recovery order. Deterministic
   for the same twin version; horizon in days; simulation only.
4. **Money-flow edges.** Twin node kinds `invoice_batch` and `payment`
   (amount + currency + status), edges `approves` (person → batch/payment),
   `pays`/`settles` (batch → payment), `depends_on` (process → batch).
   Amounts are exposed as "at risk" totals, never as instructions.
5. **Horizon predictions + watch (K).** Standard "unavailable N days"
   horizon; an always-on watch that refreshes predictions as the twin
   changes; removing a backup link or a permission changes the next
   refresh. Process-level impact counts ("14 processes").
6. **What-if engine (K).** The three founder questions on the live twin,
   deterministic per twin version, recovery plan (owners, grants, steps),
   drill comparison (Step G drills vs simulation, differences listed).
   Must prove zero `platform_action_executions` dispatched and zero
   production records changed per run.
7. **Effective-permission check (G, reused).** A recorded `backup_for`
   link is not enough: the replacement must actually hold the required
   scopes/permissions (via `app/platform`); the gap is listed when it
   does not. Simulation never grants access.
8. **Twin UI (K, with Agent 18 frontend redesign).** One twin view:
   teams, money flow, freshness ("last confirmed …"), absence/what-if
   panel, explainable impact paths. Restricted users see only their
   authorized projection; counts never leak hidden nodes.
9. **Docs.** `CHANGES-step-S.md` / `CHANGES-step-K.md` (plain language,
   file list, test counts), operator runbook for twin freshness + drills.

## 4. Build order (each slice finished + tested before the next)

1. ✅ **Slice 1 — person-absence dependency-impact pure module** (this
   branch): `app/digital_twin/impact.py` +
   `tests/test_digital_twin_impact.py`. No I/O, no DB, no live data, no
   secrets. Extends the existing impact math with absence semantics and
   money at risk; does not touch `company_graph` or `continuity` code.
   DONE (Agent 24): 22 new tests + 16 regressions passed.
2. ✅ **Slice 2 — twin snapshot builder (read-only adapter).** Build a twin
   snapshot dict from an authorized `company_graph` projection +
   authorized continuity nodes + finance batch/payment rows, all through
   `app/platform` scopes. No new tables; no writes.
   DONE (Agent 55, commit `f5b2d2b`): snapshot builder + tests; canonical
   slices 2–3 run 84 passed (46 new + 38 regressions).
3. ✅ **Slice 3 — live sync feeds.** Outbox-driven updaters (§3 item 2),
   idempotent, governed-change path. Freshness timestamps + stale marking.
   DONE (Agent 55, commit `71e92fb`): 5 outbox events via governed-change
   proposals only; canonical slices 2–3 run 84 passed.
4. ✅ **Slice 4 — horizon predictions + watch.** "Unavailable N days" on the
   merged graph; watch refreshes on twin revision change.
   DONE (Agent 81, commit `0963118`; verified Agent 92):
   `app/digital_twin/horizon.py` — fingerprint-gated refresh, 0–100
   risk score + explainable factors, quiet-hours watch 22:00–07:00 PKT
   (Step AH rules-as-data pattern, SHA-256 dedupe + cooldown). 27 new
   tests passed (pure, `--noconftest`).
5. ✅ **Slice 5 — what-if engine + drill comparison.** Founder questions,
   determinism proof, zero-side-effect proof, G-drill diff.
   DONE (Agent 81 code, verified Agent 92): `app/digital_twin/whatif.py`
   (failure engine: system_outage / supplier_failure / cyber_incident /
   site_unavailable / compound; failed/degraded/exposed states;
   all-providers rule; ranked recovery; rehearsal dry-run;
   `compare_drill` vs Step G drills) + `app/digital_twin/service.py` +
   `app/api/routes/digital_twin.py`. 20 pure what-if tests passed
   (`--noconftest`); 8/8 service/API tests passed on a throwaway
   `*_test` DB, including the zero-side-effect proof (overview +
   what-if + predict-absence write no rows in 8 write tables).
6. ✅ **Slice 6 — twin UI + docs.** Twin view, freshness, money at risk,
   `CHANGES-step-K.md`.
   DONE (Agent 81 code, verified Agent 92): `frontend/src/digital-twin/`
   (TwinWorkspace, api, types) + AdminConsole/AppSidebar/
   ModuleWorkspaces wiring + `assessment/CHANGES-step-K.md`. 5 new
   frontend tests; full frontend suite 27 passed, 0 failed; typecheck
   clean; `vite build` clean. Alembic roundtrip (downgrade base →
   upgrade head, head `0103`) + `alembic check` clean on the throwaway
   DB. Full backend suite could NOT complete on this VM (OOM-killed
   runs, load 40–85, free RAM ~154 MB — same environment that blocks
   the Step E gate; Agent 104 owns that gate) — quiet-VM re-run advised
   before merge.

Slices 1–6 are all DONE on this branch (`agent/digital-twin`).

## 5. Slice 1 — what was implemented (this branch)

**Module:** `app/digital_twin/impact.py` (pure functions; no imports from
`app.db`, no I/O, no network, no secrets).

- `simulate_absence(snapshot, absent_ids, *, horizon_days=7, depth=6)` —
  given a plain-dict twin snapshot (`entities` with `id`/`kind`/`name`/
  `attributes`, `relationships` with `source_id`/`target_id`/`kind`/
  `confidence`), returns a deterministic simulation:
  - Absent ids must exist in the snapshot (unknown id → `ValueError`).
    A `team` id expands to its `member_of` members; a `person` id is used
    directly. Other kinds are rejected (`ValueError`).
  - Directly at-risk entities: anything the absent person(s) `owns`,
    `responsible_for`, `operates`, or `approves`.
  - Propagation: `depends_on` and `supplies` edges carry impact forward
    (a dependent of an at-risk entity is at risk); `owns` /
    `responsible_for` / `operates` edges also carry impact from an
    at-risk owner to what they own. BFS, cycle-safe, depth-limited
    (`depth`, default 6); `truncated: true` when the depth limit cuts a
    path.
  - Backup coverage: a `backup_for` edge counts only when the backup
    person is not absent and (when the at-risk entity lists
    `required_skills`) holds all of them. Covered entities list their
    backups; uncovered entities are listed separately with the reason
    (`no_backup_recorded` / `backup_absent` / `backup_missing_skills`).
  - Money at risk: `invoice_batch` / `payment` entities in the affected
    set contribute `attributes.amount` grouped by currency (amounts must
    be numeric and ≥ 0; anything else → `ValueError`). Totals are
    exposure only — the twin never pays.
  - Recovery order: affected entities grouped into waves so that every
    wave's dependencies are in earlier waves (topological order over
    `depends_on`); entities in a dependency cycle are reported under
    `blocked_by_dependency_cycle` instead of silently dropped.
  - Result shape: `absent_ids`, `absent_names`, `horizon_days`,
    `affected` (entity, distance, confidence = lowest confidence on the
    shortest explanatory path, path edges, `covered_by` backups),
    `uncovered`, `money_at_risk` (per-currency totals + batch/payment
    counts), `recovery_waves`, `blocked_by_dependency_cycle`,
    `truncated`, `assumptions` (simulation only; dependencies propagate
    potential impact, not certain failure; backups are recorded links,
    not access grants), `as_of` (snapshot timestamp passthrough).
- `money_at_risk(affected_entities)` — helper that sums amounts per
  currency from an affected-entity list.

**Tests:** `tests/test_digital_twin_impact.py` — **22 passed, 0 failed**
(official pytest run, 139.29 s, throwaway DB
`infraspine_digital_twin_test` on a private Postgres 16 cluster at
127.0.0.1:55442; the repo `conftest.py` session fixture migrated the
test DB as required — no live database involved). Relevant regressions
`tests/test_company_graph.py` + `tests/test_continuity.py`: **16 passed,
0 failed** (582.20 s; one pre-existing `SADeprecationWarning` in
`app/continuity/service.py:156`, untouched by this slice). Pure pytest,
no database rows touched by the slice tests themselves.

**What slice 1 deliberately does NOT do:** no live sync, no graph writes,
no new tables, no migration, no API route, no UI, no permission grants,
no `execute_once` dispatch. It is the math the later slices wire to live
data.

## 6. Data-integrity and safety guarantees (whole step)

- Every read goes through `app/platform` authorization
  (`resolve_principal`, `authorize`, `require` → 404 non-members, 403
  members without permission). A restricted caller's simulation omits
  unauthorized entities and never leaks their counts.
- The twin is a **projection**, not a second source of truth: module
  tables stay authoritative; the twin never writes back into finance,
  workflow, or membership records.
- Simulations are side-effect free by construction: pure functions over a
  snapshot; the what-if engine (slice 5) must additionally prove zero
  `platform_action_executions` dispatched per run.
- A `backup_for` link is a recorded assignment, never an access grant.
  Effective-permission gaps are listed, not silently bridged.
- Money amounts in the twin are exposure figures. No twin output is ever
  a payment instruction; payments still require the exact-action approval
  flow in Step E/R.
- No secrets in the twin, its snapshots, its logs, or its tests. Test
  data is synthetic (fake people/teams/amounts).
- Tests run only on throwaway `*_test` databases; pytest log capture stays
  on; no live folder, live DB, `.env`, uploads, or data touched.

## 7. What Step S/K still needs from the owner

- Nothing new for slice 1 (pure math, synthetic test data).
- For live sync (slice 3): which HR/membership source is authoritative
  for people/teams (live memberships vs an external directory), and
  whether supplier/system records may be seeded from connectors.
- For money flow (slice 2/3): confirmation that invoice-batch and
  payment amounts may appear in the twin view for authorized finance
  viewers (they already exist in Finance; the twin only re-projects
  them).
- No credentials are needed for any slice; connectors stay mock until
  the owner provides real ones (same rule as Step E Gap 4).
