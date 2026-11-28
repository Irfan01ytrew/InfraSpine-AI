# Step AL — What-if simulations (CHANGES)

Plain-language summary of what changed, for the company owner.

## What this step adds

**"What if this fails?" — ask it safely, then practise the recovery.**

Before this step, the company could only guess what would break if a
system went down, a key person left, or a supplier stopped delivering.
Now the twin (the company map) can be asked a "what if" question and
shows, in plain language:

- what stops working (failed, degraded, exposed),
- how much money is at risk, in which currency,
- the recovery plan, in waves, with an owner for each step.

**Nothing is ever executed.** A simulation only reads a snapshot. A
rehearsal is a numbered list of practice steps: dry run, no side
effects, no system touched, no access granted, no payment made. A
drill comparison only reads a real drill's report — it never changes
the drill.

## The three new pieces

1. **Drill comparison** — run a scenario and compare the predicted
   recovery steps with what a real business-continuity drill actually
   did. It reports which predicted steps were drilled, which were
   missed, which came in a different order, and which had a different
   owner, plus a coverage percentage and a plain-language finding.
   (Slice 4)
2. **Rehearsal runner + records** — start a practice run from a
   simulated plan, mark steps done (with a finding) or skipped (with
   a reason), and complete the run with a summary. Records are stored
   in the database as data only. A rehearsal can only be completed
   when no step is still pending, and a tampered record (dry run
   switched off, a side effect added, a step no longer practice-only)
   is refused. (Slice 5)
3. **Twin UI panel** — on the company-graph workspace, under the live
   map: pick a scenario from the library, tick the visible entities
   it starts from, run the simulation, see the impact and the recovery
   waves, start a practice run, mark steps done or skipped, look back
   at past practice runs, and compare with a real drill by giving its
   continuity tenant and run ids. (Slice 6)

## API (for the technical reader)

- `GET /api/whatif/scenarios` — the scenario library (no company data).
- `POST /api/whatif/{tenant_id}/simulate` — from slice 3 (unchanged).
- `POST /api/whatif/{tenant_id}/rehearsals` — start a practice run (201).
- `GET /api/whatif/{tenant_id}/rehearsals` — newest-first list.
- `GET /api/whatif/{tenant_id}/rehearsals/{id}` — one record.
- `POST /api/whatif/{tenant_id}/rehearsals/{id}/check-off` — mark a step done, optional finding.
- `POST /api/whatif/{tenant_id}/rehearsals/{id}/skip` — skip a step, reason required.
- `POST /api/whatif/{tenant_id}/rehearsals/{id}/complete` — finish the run, optional summary.
- `POST /api/whatif/{tenant_id}/drills/{run_id}/compare` — compare a Step G drill run with a fresh simulation.

Every endpoint enforces the company-graph scope first (404 for
non-members) and writes one audit row. Drill comparison also enforces
the business-continuity scope for the drill's tenant (404 otherwise).

## Files changed

- `app/whatif/drills.py` — pure drill comparison.
- `app/whatif/rehearsal.py` — pure rehearsal runner.
- `app/whatif/models.py` — `WhatIfRehearsal` record (`whatif_rehearsals`).
- `app/whatif/service.py` — record persistence + drill-compare wiring.
- `app/whatif/__init__.py` — export the new modules.
- `app/api/routes/whatif.py` — scenarios, rehearsal and drill-compare endpoints.
- `migrations/env.py` — register the what-if models for Alembic.
- `migrations/versions/0110_whatif_rehearsals.py` — create `whatif_rehearsals`.
- `frontend/src/whatif/api.ts`, `types.ts`, `whatIfPanel.mjs`, `whatIfPanel.d.mts`, `WhatIfPanel.tsx` — panel + API client + pure helpers.
- `frontend/src/components/ModuleWorkspaces.tsx` — mount the panel under the live company map.
- `tests/test_whatif_drills.py`, `tests/test_whatif_rehearsal.py` — pure-function tests.
- `tests/test_whatif_rehearsals_api.py` — API + database tests.
- `frontend/test/whatIfPanel.test.mjs` — panel helper tests.
- `assessment/CHANGES-step-AL.md` — this file.

## Tests

- Slice 1 (Agent 48): 32 new tests passed.
- Slices 2–3 (Agent 67): 24 new tests passed; 32 slice-1 regressions passed.
- Slice 4 (Agent 93): 13 new drill-comparison tests passed; 51 slice-1/2 regressions passed.
- Slice 5 pure (Agent 93): 9 new rehearsal-runner tests passed (22 pure tests total with slice 4).
- Slice 5 API (Agent 93): 7 new tests — all PASSED, each in its own run (the VM kills multi-test runs with OOM): scenarios library 1 passed (26:21); rehearsal lifecycle 1 passed (11:08); complete-with-pending-steps 422 PASSED; check-off-unknown-step 422 PASSED; rehearsal non-member 404 PASSED (10:42); drill comparison read-only PASSED (6:19, coverage 1.0, drill untouched, +1 audit row); drill non-member-continuity 404 PASSED (7:04). Two full-file runs were OOM-killed mid-run before these single-test runs; no code failure was observed in any run. Three real bugs were found and fixed along the way (see below).
- Slice 6 frontend (Agent 93): 5 new panel-helper tests passed; full frontend suite 27/27 passed; typecheck clean; vite build clean (2m 51s).
- Full-suite gate: NOT completed — the VM is thrashing (load 40–95, OOM kills, 3 runtime restarts during this task). Single-test runs green, zero code failures observed. A quiet-VM full-backend-suite run is still owed before merge (Agent 104 owns the Step E gate; the Step AL branch needs its own quiet run).
- Alembic roundtrip: 0104 → 0103 → 0104 verified on a disposable test cluster; `alembic check` clean ("No new upgrade operations detected.").

## Bugs found and fixed while testing (Agent 93)

- The `GET /api/whatif/scenarios` endpoint called `simulator.list_templates()`, which did not exist — the endpoint would have 500'd. Added `list_templates()` + `template_name()` to `app/whatif/simulator.py` (2 new pure tests).
- The drill comparison endpoint and UI assumed flat keys (`coverage`, `verified_rate`, `simulated_steps`...) that `app/whatif/drills.py` does not return. Aligned the endpoint, the API test, the panel and its helpers to the real shape (`summary.coverage`, `summary.drill_verified_rate`, `summary.simulated_count`, `run.mode`, ...).
- The drill-seed helper in the API test omitted `bc_tenants.revision`/`event_head` (no server defaults) and the plan data lacked `crisis_team`/`contact_tree`, which continuity's `plan_for` requires — and its step owner/resource ids must exist as `bc_nodes`. Fixed the seed; the continuity service itself was not changed.

## Things to decide at merge time (not blockers)

- **Migration number 0104 collided.** This branch minted
  `0104_whatif_rehearsals` (down_revision `0103`). At integration it was
  renumbered to `0110_whatif_rehearsals` (down_revision
  `0109_knowledge_expiry_alerts`) in the linear chain recorded in
  INTEGRATION-SNAPSHOT.md (map v4). The table itself is independent of
  the others.
- **Step S overlap.** Step S (digital twin) slices 4–6 also plan a
  what-if engine + drill comparison with a zero-side-effect proof.
  Reconcile at merge so the proof is not built twice; the Step AL
  drill comparison is the one to keep, and the simulation endpoint
  should switch from the graph-only `snapshot_from_projection` to the
  full-twin `build_simulation_snapshot` once the Step S builder
  merges (the adapter already raises a clear error at that
  integration point until then).
- **Owner decisions (from the plan):** who may run rehearsals, and
  whether rehearsal findings go to the same audit trail as real
  incidents. Safe default shipped: the same scope and audit trail as
  simulations.
- **Known unrun tests:** the company-graph suite (2/12 passed, 10
  unrun — OOM-killed under VM load) should be re-run in a quieter
  window or as part of the merge gate.
