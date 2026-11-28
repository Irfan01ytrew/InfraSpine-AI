# CHANGES — Step G: Continuity Engine (slices 2–8)

**Status:** slices 2–8 implemented on branch `agent/stepg-continuity`. Slice 1 was
the preparation slice (Agent 16). Slices 2–4 by Agent 34 (HEAD `2e0faa0`);
slices 5–8 by Agent 114 (this branch).

**Plain language:** the Continuity Engine answers "what breaks if this person,
server, or supplier disappears, and who can take over?" It now backs up the
database and proves the backup restores, runs recovery plans through the same
unified engine as every other workflow, checks whether a replacement person
actually has the permissions and skills to take over (and can grant them for a
limited time), starts merging the two separate company graphs into one identity,
runs five disruption drills and compares each drill against the simulation, and
shows all of this in the Continuity workspace in the frontend.

## What changed

### Slice 2 — backup + freshness (`app/continuity/backup.py`)

- `create_backup`: real `pg_dump` (custom format) + `manifest.json` with
  SHA-256, row counts, and the Alembic head at backup time.
- `check_backup`: freshness rules (never / stale / fresh) and
  `current_alembic_head`.
- Tests: `tests/test_continuity_backup.py`.

### Slice 3 — restore verification (`app/continuity/restore.py`)

- `verify_restore`: manifest first, target database must end in `_test` and
  differ from the source, then `createdb` → `psql` restore
  (`ON_ERROR_STOP=1`) → read-back checks (head, table count, row counts) →
  `dropdb` in a `finally`.
- Tests: `tests/test_continuity_restore.py`.
- A real restore drill passed: backup `bk-20261128-013323` restored into a
  throwaway database, head matched, 198 tables, zero problems, target dropped.

### Slice 4 — activations on the unified engine (`app/continuity/unified.py`)

- `mirror_continuity_run`: idempotent mirror of a continuity run into the
  workflow engine (`origin='continuity'`), `mirror_pending_continuity_runs`
  sweep, `on_workflow_cancelled` suspends the activation, and a
  `ContinuityExecuteHandler` (`continuity.execute`) that runs through
  `execute_once`.
- Wiring: `app/workflows/actions.py`, `engine.py` (tick sweep), `service.py`
  (cancel branch), `continuity/service.py` (mirror at decision time),
  `continuity/routes.py` (cipher pass-through).
- Migration `0104_continuity_unified_origin`: widens
  `ck_wf_definitions_origin_valid` to include `'continuity'`; downgrade
  refuses while continuity runs exist.
- Tests: `tests/test_continuity_unified.py`, `tests/test_continuity.py`.

### Slice 5 — effective-permission handover check + time-limited grants

- `app/continuity/handover.py` (pure + service):
  - `required_permissions`: per-kind defaults (process/service/system/project/
    approval → `workflow.access` + `documents.read`; department →
    `group.access`; knowledge → `documents.read`); a node can override with
    `required_permissions` in `node.data`, validated as dotted lowercase
    permission names (≤ 80 chars).
  - `check_candidate` / `handover_check`: gap codes
    `missing_permission` / `missing_skill` / `missing_backup` /
    `candidate_unavailable` / `no_user_binding`. Draft steps are grouped by
    resource. An unbound scope is reported as
    `permission_check: "not_applicable"` — eligibility then rests on backup
    coverage and skills.
  - `propose_handover_grant` (planner/owner): candidate must be eligible or
    the request fails with 409; the approval link expires after 1 hour;
    `valid_days` 1–90, default 30.
  - `decide_handover_grant` (owner/approver): separation of duties — the
    requester and the beneficiary cannot approve; the approval is bound to
    an exact digest of the grant; on approval the role
    `continuity.handover` is ensured and membership granted with `valid_to`,
    superseding prior active memberships.
  - `revoke_handover_grant`, `handover_access_active`.
- `app/continuity/models.py`: `HandoverGrant` + migration
  `0105_continuity_handover_grants` (`bc_handover_grants`; downgrade refuses
  while active grants exist).
- Routes: `GET .../scenarios/{id}/handover-check`,
  `GET/POST .../handover-grants`, `POST .../handover-grants/{id}/decision`,
  `POST .../handover-grants/{id}/revoke`.
- Tests: `tests/test_continuity_handover.py` (DB),
  `tests/test_continuity_handover_pure.py` (pure).

### Slice 6 — graph merge start (`app/continuity/graph_merge.py`)

- `unified_key`: person → `person:<user_id>`, department →
  `department:<access_group_id>`, otherwise `continuity:<bc_node_id>`.
  `seed_items` is deterministic (sorted); `seed_idempotency_key` is a
  content hash, so re-seeding the same memberships is a no-op.
- `seed_graph_merge` requires the caller to own the continuity tenant AND the
  company-graph tenant (the discovery sync underneath requires owner). It
  seeds departments (continuity department nodes with an access group) and
  people (active continuity members and members of bound access groups,
  creating a missing continuity employee node as evidence), then proposes
  them through `discovery.synchronize` as **pending GraphChange rows only** —
  it never writes directly to the company graph. Link rows are recorded in
  `bc_graph_links` and an outbox + journal event
  `continuity.graph_merge_seeded` is published.
- `merge_status` returns linked counts per side, unlinked samples (limit 10),
  and stale sources.
- `app/continuity/models.py`: `GraphLink` + migration
  `0106_continuity_graph_links` (`bc_graph_links`).
- Routes: `POST .../graph-merge/seed`, `GET .../graph-merge/status`.
- Tests: `tests/test_continuity_graph_merge.py` (DB),
  `tests/test_continuity_graph_merge_pure.py` (pure).

### Slice 7 — five disruption drills + drill/simulation comparison

- `app/continuity/drills.py`:
  - `DRILL_KINDS`: resignation, absence, server_outage, database_failure,
    supplier_disruption.
  - `pick_drill_targets`: first employee / a critical (or any) system / a
    database-named system / first supplier; `None` when that kind of node
    does not exist.
  - `seed_drills` (owner/planner): idempotent via `source_key="drill:<kind>"`
    (a second seed returns the existing scenarios); each drill runs 120
    minutes; the supplier drill is skipped when no supplier node exists.
  - `compare_drill_to_simulation`: comparison codes
    `affected_not_recovered`, `recovered_not_in_simulation`,
    `rto_missed_in_drill`, `drill_missed_rto_simulation_met`,
    `drill_met_rto_simulation_missed`, `mtpd_exceeded_in_drill`,
    `data_loss_exceeds_rpo`.
  - `drill_comparison`: the run must be in drill mode (409 otherwise); the
    drill is built from the run report + scenario analysis and compared
    against the simulation forecast.
- Routes: `POST .../drills/seed`, `GET .../runs/{run_id}/drill-comparison`.
- Tests: `tests/test_continuity_drills.py` (DB),
  `tests/test_continuity_drills_pure.py` (pure).

### Slice 8 — frontend views

- `frontend/src/continuity/types.ts`: `HandoverCheck`, `HandoverGrant`,
  `GraphMergeStatus`, `DrillSeedResult`, `DrillComparison`.
- `frontend/src/continuity/api.ts`: `handoverCheck`, `handoverGrants`,
  `seedDrills`, `drillComparison`, `seedGraphMerge`, `graphMergeStatus`.
- `frontend/src/continuity/drillSummary.mjs`: `summarizeDrillComparison`
  (plain-language one-line summary + counts per comparison code) with
  `frontend/test/drillSummary.test.mjs`.
- `frontend/src/components/ModuleWorkspaces.tsx`: the Continuity workspace
  gained Handover check, Disruption drills, and Graph merge cards.

## Tests

Pure (no database) — all passing:

| Suite | Command | Result |
| --- | --- | --- |
| Handover pure | `pytest tests/test_continuity_handover_pure.py -q` | 5 passed |
| Graph-merge pure | `pytest tests/test_continuity_graph_merge_pure.py -q` | 3 passed |
| Drills pure | `pytest tests/test_continuity_drills_pure.py -q` | 5 passed |
| Drill summary (frontend) | `node --test frontend/test/drillSummary.test.mjs` | 3 passed |
| Slice 2–4 pure | `pytest tests/test_continuity_backup.py tests/test_continuity_restore.py tests/test_continuity_unified.py` (pure subset) | 23 passed (Agent 34) |

Database (throwaway `*_test` DBs via `scripts/irfanix_postgres_tests.py`,
ephemeral clusters from `/home/hatch/tmp/pg-dist` binaries):

| Suite | Result |
| --- | --- |
| `tests/test_continuity_handover.py` | 7 passed (batch 4, attempt 2, 1231s) |
| `tests/test_continuity_graph_merge.py` | 3 passed (re-run, attempt 2, 468s) |
| `tests/test_continuity_drills.py` | 3 passed (re-run, attempt 3, 261s) |
| `tests/test_continuity_unified.py` + `tests/test_continuity.py` (slice 2–4 canonical re-run) | 10 passed (unified 6 + continuity 4) |
| `tests/test_company_graph.py` (regression for the `synchronize` digest change) | 12 passed |

## Gates

- `alembic upgrade head` to `0106_continuity_graph_links`: clean; roundtrip
  verified 2026-11-28 on a throwaway cluster — upgrade to `0106`, downgrade
  to `0105`, upgrade to `0106` again, single head, `alembic check` clean
  ("No new upgrade operations detected", RC=0).
- Frontend `npm test` 25 passed, `tsc` clean, `vite build` clean (8.08s,
  1 GB heap via `NODE_OPTIONS=--max-old-space-size=1024`).

### Fixes found while verifying (all in the committed code)

- Handover tests asserted the wrong error shape (`["detail"]`); the app
  returns `{"error": {"message"}}` — test fix, no product change.
- Graph-merge seed crashed with `create_node() missing 'ctx'` — `ctx` is
  now threaded through `_ensure_employee_node`, and the helper returns the
  `Node` row instead of the public dict `create_node` returns.
- Drill seed tried to `UPDATE bc_scenarios.source_key` after insert, which
  the immutability trigger rejects — `create_scenario` now takes an
  optional server-side `source_key` (set at insert; the API schema still
  has no `source_key` field, so clients cannot forge one).
- Graph-merge re-seed 409'd in `discovery.synchronize`: the idempotency
  digest covered the whole `SyncInput`, including the fresh `observed_at`
  timestamp, so a replay could never match — the digest now excludes
  `observed_at` (a different item/reference/visibility still 409s).
- Drills non-drill test left the session logged in as the approver (403)
  and used a simulation scenario for an incident run (422) — it now logs
  back in as the owner and uses an incident-escalation scenario.

## Merge notes

- **Migration collisions to reconcile at merge:** `0104` is claimed 4×
  (continuity vs HR vs WhatsApp AE vs AI freshness); `0105` is shared by AK,
  AI, AB, and Step G handover; `0106` is Step G graph links. Renumber at
  merge time — the Step G chain is `0104 → 0105 → 0106` internally.
- **Owner decisions still open:** which five disruptions to drill first
  (implemented default: resignation, absence, server outage, database
  failure, supplier disruption) and confirmation of the PC backup folder.
- **Known environment caveat:** this VM is heavily loaded; full-suite pytest
  runs are repeatedly OOM-killed (exit 137) and the VM restarts wipe the
  system PostgreSQL. The private test cluster
  (`/home/hatch/.tmp-pgtest/cluster`, port 55449, binaries at
  `/home/hatch/tmp/pg-dist/usr/lib/postgresql/16/bin/`) survives restarts
  and must be restarted from there after a VM restart. Two restart
  recoveries were needed while verifying slices 5–8: the `pg-dist`
  binaries need `LD_LIBRARY_PATH=/home/hatch/tmp/pg-dist/usr/lib/x86_64-linux-gnu`
  (the `ld.so` entry is wiped by restarts), and the `pgtest` user must be
  re-added to the `postgres` group or `pg_ctl start` fails on the Unix
  socket directory (`/var/run/postgresql`).

## Files changed (slices 5–8)

- `app/continuity/handover.py` (new)
- `app/continuity/graph_merge.py` (new)
- `app/continuity/drills.py` (new)
- `app/continuity/models.py` (`HandoverGrant`, `GraphLink`)
- `app/continuity/service.py` (optional server-side `source_key` on
  `create_scenario`, for drill seeds)
- `app/company_graph/discovery.py` (`synchronize` digest excludes
  `observed_at`, so identical re-seeds replay)
- `app/continuity/schemas.py` (`HandoverGrantInput`, `GraphMergeSeedInput`,
  `NodeInput.required_permissions`)
- `app/continuity/routes.py` (handover-check/grants, graph-merge, drills
  endpoints; renamed the existing `handover` route function to
  `handover_draft_endpoint` — it shadowed the `handover` module import)
- `migrations/versions/0105_continuity_handover_grants.py` (new)
- `migrations/versions/0106_continuity_graph_links.py` (new)
- `tests/test_continuity_handover.py`, `tests/test_continuity_handover_pure.py`
  (new)
- `tests/test_continuity_graph_merge.py`,
  `tests/test_continuity_graph_merge_pure.py` (new)
- `tests/test_continuity_drills.py`, `tests/test_continuity_drills_pure.py`
  (new)
- `frontend/src/continuity/types.ts`, `frontend/src/continuity/api.ts`,
  `frontend/src/continuity/drillSummary.mjs`,
  `frontend/test/drillSummary.test.mjs`,
  `frontend/src/components/ModuleWorkspaces.tsx`
- `docs/step-g-continuity-plan.md` (§4 slices 5–8 marked done)
- `assessment/CHANGES-step-G.md` (this file)
