# Step AL — What-if Simulations: implementation plan

Status: **slices 1–6 DONE (Agent 93, 2026-11-28)** — drill comparison,
rehearsal runner + records, twin UI panel, `assessment/CHANGES-step-AL.md`
and gates (branch `agent/whatif-simulations`, base `step-e-gaps-wip`
`ca956c2`). Merge readiness: code complete; the full backend suite could
not be completed on the VM (OOM kills under load 40–95; single-test runs
green, zero code failures observed) — a quiet-VM re-run of the full file
and the full suite is still owed before merge. Migration `0104` collides
with the HR/AI/AK branches and must be renumbered at merge. Step AL is the owner-approved resilience
step from the 10-feature list (2026-11-28): run "what if" simulations —
server down, key employee leaves, supplier fails, cyber incident — see the
impact and rehearse the recovery, without touching live operations.

This step is the **simulation engine of master-plan v2 §8** (Simulation /
What-if Engine) and the what-if half of Step K (Digital twin, prediction,
simulation, 28–40 h). It is **not** a second digital twin and **not** a
second continuity graph. It builds on:

- Agent 24, Step S (digital twin) — `docs/step-s-digital-twin-plan.md` and
  the uncommitted slice `app/digital_twin/impact.py` (`simulate_absence`:
  person/team absence over the same snapshot shape). Step AL **reuses that
  snapshot shape** so the two engines can run on the same twin snapshot
  later, and **does not duplicate** the absence-only math: AL's engine is
  the general multi-kind scenario engine (any node kind can fail), with
  failed / degraded / exposed states and ranked recovery options.
- Agent 43, Step AH (Company Resilience AI) — dependency map, risk
  prediction, recovery plans, approval gates, audit. AL is the
  *rehearsal* half of AH: AH predicts and alerts on real risks; AL lets
  the owner practise hypothetical ones. AH owns the live map and alerts;
  AL owns the scenario library and the dry-run engine.
- Agent 16, Step G (Continuity Engine) — `docs/step-g-continuity-plan.md`:
  recovery drills on seeded data. A Step G drill can be compared to an AL
  simulation; differences are listed (v2 §8 acceptance test).

Sources: `assessment/master-plan-v2.md` §8 (Simulation / What-if Engine),
§4 (Step K), §5 (Predictive Continuity); `HANDOVER-PROMPT.md` §4–§5;
`docs/step-s-digital-twin-plan.md` (Agent 24); `docs/step-g-continuity-plan.md`
(Agent 16). All paths relative to the repo root.

## 1. What Step AL must deliver (acceptance)

Taken from v2 §8 acceptance tests, restated for AL:

1. **The three founder questions, on seeded data, return impact and a
   recovery plan.** "What if the Finance Manager is removed?", "What if
   the ERP is down?", "What if a supplier fails?" — each returns affected
   entities + processes (with owners and why) and a recovery plan
   (owners, grants, steps). A simulation is deterministic for the same
   twin version (same snapshot + same scenario → identical result).
2. **Side-effect free, by construction and by proof.** Running a
   simulation writes no `platform_action_executions` in the dispatched
   state and changes no production records. The engine is pure functions
   over a snapshot; the later API slice must additionally prove this with
   a test that counts dispatches before/after a run.
3. **Rehearsal mode.** A recovery plan can be practised step by step
   ("rehearsal") without touching real systems: each step is marked
   practice-only, and no step is executed. A rehearsal never grants
   access, never approves, never pays.
4. **Drill comparison (Step G).** A recovery drill's actual steps can be
   compared with the simulated plan; differences are listed (missing
   steps, extra steps, different owners, different order).
5. **Explainable, permission-safe.** Every affected entity carries the
   path that explains why it is affected (edges + confidence). A
   restricted caller's simulation omits unauthorized entities and never
   leaks their counts (same `visible_scopes` rule as the twin; enforced
   at the API slice, not in the pure engine).
6. **Money at risk is exposure only.** Invoice batches and payments in
   the affected set contribute amounts per currency. No simulation output
   is ever a payment instruction.

## 2. What already exists (do not rewrite)

| Area | Existing code | Notes |
|---|---|---|
| Company-graph simulation | `app/company_graph/schemas.py` (`Simulation`: kinds resignation / absence / system_outage / supplier_failure / restructuring) | Schema only; no engine runs it against the live twin or real permissions. |
| Continuity scenarios | `app/continuity/schemas.py` (12 kinds incl. resignation, server_outage, supplier_disruption, cyber_incident, compound), `create_scenario` in `app/continuity/service.py` | Scenarios are records + BIA scores, not simulations with impact + recovery plan. |
| Company-graph impact | `app/company_graph/intelligence.py` (`impact()` forward/reverse BFS depth 6) | Outage semantics ("failed node"), no degraded/exposed states, no money, no recovery plan. |
| Continuity BIA | `app/continuity/intelligence.py` (`bia_score` / `analyze` / `handover_draft`, recovery waves, skill-matched backup candidates) | Static graph; activation is its own code path, not the unified engine (Step G fixes this). |
| Digital-twin absence math | `app/digital_twin/impact.py` (Agent 24, uncommitted on `agent/digital-twin`) — `simulate_absence()` over the twin snapshot shape, backup coverage, money at risk, recovery waves | Person/team absence only. Step AL reuses the **snapshot shape** and the **wave/backup ideas**, not the function itself. |
| Forecast scenarios (RAG) | `app/services/forecasts.py`, `app/rag/forecast.py` | Numeric projections, not ops what-if. |
| Workflow runs (live-work feed) | `app/workflows/unified.py`, `app/db/workflow_engine_models.py` | Runs exist; nothing feeds them into either graph today (Step S slice 3). |
| Frontend | `frontend/src/company-graph`, `frontend/src/continuity` | Graph/plan UI exists; no what-if panel, no rehearsal view. |
| Tests (thin) | `tests/test_company_graph.py` (12), `tests/test_continuity.py` (4) | No simulation-engine tests exist. |

## 3. What is missing (the Step AL work list)

1. **General scenario engine (slice 1 — implemented now).** Pure
   functions: any node kind can be the seed failure (system, supplier,
   person, team, site, data); failed / degraded / exposed states;
   deterministic; ranked recovery options; rehearsal framing. See §5.
2. **Scenario library.** Named, versioned scenario templates (server
   down, key employee leaves, supplier fails, cyber incident, site
   unavailable, compound) with seed-kind rules and default horizons.
   Slice 1 ships the library as data + a builder that validates seeds.
3. **Twin snapshot adapter (later slice).** Build the simulation snapshot
   from an authorized company-graph projection + continuity nodes +
   finance rows, all through `app/platform` scopes. No new tables; no
   writes. Reuses Step S slice 2 when it lands; until then, snapshots
   are supplied by the caller (tests use synthetic snapshots).
4. **API + permission projection (later slice).** `POST
   /api/whatif/simulate` through `app/platform` (restricted callers see
   only their authorized projection; counts never leak hidden nodes);
   zero-side-effect proof test (dispatch count before/after).
5. **Drill comparison (later slice, with Step G).** Compare a Step G
   drill's actual steps with the simulated plan; list differences.
6. **Rehearsal runner (later slice).** Step-by-step practice UI flow:
   each recovery step shown, marked practice-only, checked off by the
   practising user; rehearsal record stored (who practised, when, which
   steps, what was found) — a record, never an execution.
7. **Twin UI panel (later slice, with Agent 18 frontend redesign).**
   "What if?" panel on the twin view: pick a scenario, see impact +
   recovery plan + money at risk, start a rehearsal.
8. **Docs.** `CHANGES-step-AL.md` (plain language, file list, test
   counts), operator runbook for scenarios + rehearsals.

## 4. Build order (each slice finished + tested before the next)

1. ✅ **Slice 1 — general scenario simulator + scenario library**
   (this branch): `app/whatif/simulator.py` +
   `tests/test_whatif_simulator.py`. Pure stdlib, no I/O, no DB, no live
   data, no secrets. Same snapshot shape as `app/digital_twin/impact.py`
   so both engines can share one snapshot later.
2. **Slice 2 — twin snapshot adapter.** Read-only adapter from
   authorized projections (reuses Step S slice 2 when available).
3. **Slice 3 — API + permission projection + zero-side-effect proof.**
4. **Slice 4 — drill comparison with Step G.**
5. **Slice 5 — rehearsal runner + records.**
6. **Slice 6 — UI panel + docs + full gates, then merge.**

## 5. Slice 1 — what was implemented (this branch)

**Module:** `app/whatif/simulator.py` (pure functions; no imports from
`app.db`, no I/O, no network, no secrets; stdlib only).

Snapshot shape — identical to `app/digital_twin/impact.py` so the two
engines share one snapshot:

```python
{
    "entities": [
        {"id": "s-1", "kind": "system", "name": "ERP",
         "attributes": {"restorable_from_backup": True}},
        {"id": "b-1", "kind": "invoice_batch", "name": "March batch",
         "attributes": {"amount": 1500.00, "currency": "PKR",
                        "required_skills": ["approvals"]}},
    ],
    "relationships": [
        {"id": "e-1", "source_id": "p-1", "target_id": "b-1",
         "kind": "approves", "confidence": 1.0, "evidence": []},
    ],
    "as_of": "2026-11-28T00:00:00+00:00",  # optional, passed through
}
```

**Scenario kinds** (`SCENARIO_KINDS`): `employee_absence`,
`team_absence`, `system_outage`, `supplier_failure`, `cyber_incident`,
`site_unavailable`, `compound`. Each scenario has a seed kind rule
(e.g. `system_outage` seeds must be `system`/`service`; `compound`
seeds may mix kinds).

**States.** An affected entity is `failed`, `degraded`, or `exposed`
(cyber only):

- Seed nodes are `failed` (`exposed` is never a seed state).
- `depends_on`: target failed → source `failed`; target degraded →
  source `degraded`; target exposed → source `degraded` (data it relies
  on may be tainted). `located_at` behaves the same (site failure flows
  to what is located there).
- `supplies` / `serves`: source failed → target `failed` **only when
  every provider of that kind for the target has failed**; while any one
  provider still works the target is `degraded` instead. Source
  degraded/exposed → target `degraded`.
- Authority edges (`owns` / `responsible_for` / `operates` /
  `approves`): holder failed/absent → target `failed`; holder degraded
  → target `degraded`. (This is how absence works in the general
  engine: an absent person is a failed seed, and what they hold fails
  with them — the same outcome as `simulate_absence`, without
  duplicating its function.)
- `cyber_incident`: the compromised seed is `failed`; data/knowledge
  entities directly connected to it (`documents`, `depends_on`,
  `supplies`, `serves`) become `exposed`; systems depending on the
  compromised node become `degraded` (quarantine). A failed state
  always wins over degraded/exposed when an entity is reached by
  several paths.
- `member_of`: a `team` seed expands to its person members (same rule
  as the twin's absence expansion).
- Propagation is BFS, cycle-safe, depth-limited (`depth`, default 6,
  same as `app.company_graph.intelligence.impact`); `truncated: true`
  when the depth limit cuts a path. A nearer (shorter-path) state wins;
  ties keep the worse state.

**Severity.** Per affected entity, explainable from state + money +
distance: `critical` (failed + money at risk, or failed process /
invoice_batch / payment), `high` (failed), `medium` (degraded),
`low` (exposed). The rule is recorded on the result, not hidden.

**Recovery options** (ranked per affected entity, best first):

1. `activate_backup` — a `backup_for` edge whose backup person is not
   failed and holds every skill the entity requires (same eligibility
   rule as the twin). Owner = the backup person.
2. `alternate_supplier` — another non-failed supplier `supplies` the
   entity. Owner = the alternate supplier.
3. `restore_from_backup` — entity attributes say
   `restorable_from_backup: true` (systems/data only). Owner = the
   entity's recorded owner, if any, else "operations".
4. `manual_workaround` — always available fallback: manual operation
   by the entity's owner (or "operations") while the failure lasts.

Entities with no option above the fallback are listed under
`recovery_plan.uncovered` with the reason (`no_backup_recorded` /
`backup_unavailable` / `backup_missing_skills` / `no_alternate_supplier`).

**Recovery waves.** Affected entities grouped into waves so that every
wave's dependencies are in earlier waves (topological order over
`depends_on`); entities in a dependency cycle are reported under
`blocked_by_dependency_cycle` instead of silently dropped (same
approach as the twin).

**Scenario library.** `SCENARIOS` — named templates (`server_down`,
`key_employee_leaves`, `supplier_fails`, `cyber_incident`,
`site_unavailable`) with name, description, kind, allowed seed kinds
and default horizon. `build_scenario(template_id, seed_ids, ...)`
validates the template and the seeds against the snapshot and returns
a scenario dict ready for `simulate()`.

**Rehearsal.** `simulate(..., rehearse=True)` adds a `rehearsal`
block: the same recovery plan rewritten as numbered practice steps,
each marked `practice_only: True`, plus `dry_run: True` and
`side_effects: []` (empty by construction — a pure function cannot
execute anything). Rehearsal never grants access, never approves,
never pays.

**Result shape:** `scenario` (kind, name, seed ids/names, horizon),
`affected` (entity, state, severity, distance, confidence = lowest
confidence on the shortest explanatory path, path edges,
`recovery_options`), `failed` / `degraded` / `exposed` (id lists),
`money_at_risk` (per-currency totals + batch/payment counts, exposure
only), `recovery_plan` (waves of steps + uncovered), `rehearsal` (or
`None`), `truncated`, `assumptions`, `as_of`, `dry_run: True`.

**Tests:** `tests/test_whatif_simulator.py` — **32 passed, 0 failed**
(2.08s, minimal pytest-only venv, `--noconftest`, log capture on).
Pure pytest, no database rows touched; run with `--noconftest` in a
minimal venv because the repo `conftest.py` needs the full app stack
and this slice is pure functions. Log capture stays on.

**What slice 1 deliberately does NOT do:** no live sync, no graph
writes, no new tables, no migration, no API route, no UI, no
permission grants, no `execute_once` dispatch, no drill comparison,
no rehearsal records. It is the math the later slices wire to live
data.

## 6. Data-integrity and safety guarantees (whole step)

- Every read goes through `app/platform` authorization
  (`resolve_principal`, `authorize`, `require` → 404 non-members, 403
  members without permission). A restricted caller's simulation omits
  unauthorized entities and never leaks their counts (enforced at the
  API slice; the pure engine only ever sees the snapshot it is given).
- The simulator is a **projection reader**, not a second source of
  truth: module tables stay authoritative; simulations never write back
  into finance, workflow, or membership records.
- Simulations are side-effect free by construction: pure functions over
  a snapshot; the API slice (slice 3) must additionally prove zero
  `platform_action_executions` dispatched per run.
- A `backup_for` link is a recorded assignment, never an access grant.
  Effective-permission gaps are listed, not silently bridged.
- Money amounts are exposure figures. No simulation output is ever a
  payment instruction; payments still require the exact-action approval
  flow in Step E/R.
- No secrets in the simulator, its snapshots, its logs, or its tests.
  Test data is synthetic (fake people/teams/amounts).
- Tests run only on throwaway `*_test` databases; pytest log capture
  stays on; no live folder, live DB, `.env`, uploads, or data touched.

## 7. What Step AL still needs from the owner

- Nothing new for slice 1 (pure math, synthetic test data).
- For the snapshot adapter (slice 2): which HR/membership source is
  authoritative for people/teams, and whether supplier/system records
  may be seeded from connectors (same open question as Step S).
- For rehearsal records (slice 5): who may run rehearsals and whether
  rehearsal findings go to the same audit trail as real incidents.
- No credentials are needed for any slice; connectors stay mock until
  the owner provides real ones (same rule as Step E Gap 4).
