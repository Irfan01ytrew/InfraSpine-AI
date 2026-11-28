# CHANGES — Step AH: Company Resilience AI

Plain-language summary of what Step AH adds to InfraSpine AI. Slices 1–7,
all on branch `agent/resilience-ai` (from `step-e-gaps-wip` `2fb387d`).

## What it is

Company Resilience AI keeps a continuously updated map of what the company
depends on — people, teams, processes, systems, data, suppliers and money —
predicts what breaks when one of them fails, and turns the prediction into
a recovery plan that only runs with a human approval. It also compares
real continuity drills against its predictions and feeds the outcome back
into the risk scores, so a drill that fails makes the risk score go up.

## What it does, slice by slice

1. **Disruption impact + risk scoring** (`app/resilience/impact.py`).
   Scores five disruption kinds (person absence, team absence, server
   outage, cyber incident, supplier failure) over a dependency snapshot:
   what is affected, how bad it is, money at risk by currency, which
   entities have no backup, and ranked recovery options. Every entity
   also gets a proactive 0–100 risk score with named factors (blast
   radius, backup coverage, sole approver, sole supplier, staleness,
   criticality).
2. **Risk watch** (`app/resilience/watch.py`). Rules as data (severity
   threshold on the 0–100 score, optionally narrowed to a named factor
   or entity kinds). Repeats are deduped by a stable SHA-256 fingerprint
   per (rule, node) with a cooldown; quiet hours are 22:00–07:00
   Asia/Karachi — only rules marked `breaks_quiet_hours` (critical,
   ≥80) fire then, the rest are deferred. Alert messages carry names
   and scores only, never account numbers or secrets.
3. **Snapshot builder** (`app/resilience/snapshot.py`). Read-only adapter
   from the company graph, the continuity graph and finance rows into one
   validated snapshot. The company-graph version wins when the same
   entity appears in both graphs with a different kind or name; retired,
   unknown and dangling rows are skipped and listed; `payee_account` is
   never copied.
4. **Recovery plans as unified-engine runs** (`app/resilience/plans.py`,
   `app/resilience/runtime.py`). A plan is built from the analysis's
   ranked recovery options (action, owner and ETA kept verbatim) and
   runs through `app/workflows/unified.py`: approval nodes with
   exact-action approvals, person execution steps via `execute_once`
   with read-back verification, pause/resume/stop from the last verified
   step. A supplier switch is sensitive — only the owner
   (`platform.manage`) may approve it; other approval-bound steps need
   `workflow.manage`. Starting the same plan twice returns the original
   run instead of duplicating approvals. Performing a step is a human
   attestation recorded in the audit trail — it moves no money, restores
   no data and switches no supplier externally.
5. **Drill comparison** (`app/resilience/drills.py`). A closed Step G
   drill is compared against an AH simulation: unexpected affected
   entities, predicted entities the drill missed, uncovered entities the
   drill hit, and steps that missed their RTO. An uncovered hit counts
   only when the drill shows the missing cover mattered (the entity's
   step missed its RTO or never verified) — mere contact is not a
   failure. Failed drills raise the node's risk score (+10 unexpected,
   +15 uncovered hit, +10 RTO missed, capped at +30 per node); a passing
   drill never lowers a score.
6. **Audit trail + export** (`app/resilience/audit.py`). Every resilience
   event (plan started / controlled / step performed, drill comparison)
   is recorded via platform audit events and published to the outbox,
   with redaction. The shared redactor in `app/recovery/policy.py` was
   widened: it missed keys literally named `master_key` /
   `encryption_key`. Financial identifiers (`payee_account`,
   `bank_account`, `account_number`, IBAN) are blanked on top, so the
   trail never holds them. Export is CSV or JSON, owner-only.
7. **Resilience UI + API** (`app/resilience/service.py`,
   `app/api/routes/resilience.py`, `frontend/src/resilience/`,
   `frontend/src/components/ModuleWorkspaces.tsx`). One view ("08 ·
   Company resilience" in the sidebar, admin-only like the other module
   workspaces): dependency-map counts, ranked risks, a disruption
   simulation panel (advisory only), recovery-plan progress, recent
   audit activity, map freshness, and an owner-only CSV export. The
   overview is the caller's authorized projection — continuity nodes
   for the department (the department-kind node carrying the scope's
   `access_group_id`, plus the resources in the same tenant whose
   `department_id` is that node) plus company-graph records the caller
   may read under the graph's own visibility rules (tenant / department
   / restricted + readers). Finance rows are not linked to department
   scopes yet, so they are omitted and the omission is stated in the
   response. Counts cover visible records only; hidden records are never
   counted, named, or leaked through totals.

## Files changed

- New: `app/resilience/impact.py`, `watch.py`, `snapshot.py`,
  `plans.py`, `runtime.py`, `drills.py`, `audit.py`, `service.py`,
  `app/api/routes/resilience.py`, `frontend/src/resilience/api.ts`,
  `frontend/src/resilience/types.ts`,
  `tests/test_resilience_impact.py`, `tests/test_resilience_watch.py`,
  `tests/test_resilience_snapshot.py`, `tests/test_resilience_plans.py`,
  `tests/test_resilience_drills.py`, `tests/test_resilience_audit.py`,
  `tests/test_resilience_runtime.py`, `tests/test_resilience_overview.py`,
  `docs/step-ah-resilience-plan.md`, `assessment/CHANGES-step-AH.md`.
- Modified: `app/resilience/__init__.py` (exports),
  `app/workflows/actions.py` (registers the two resilience handlers),
  `app/recovery/policy.py` (redactor: `master_key` / `encryption_key`),
  `app/main.py` (registers the resilience router),
  `frontend/src/components/ModuleWorkspaces.tsx` (ResilienceWorkspace),
  `frontend/src/components/AdminConsole.tsx` + `AppSidebar.tsx`
  (view + sidebar entry).

No new database migrations: Step AH adds no tables. No production data
is changed by any of it; plans execute only through approvals.

## Tests

Pure-function suites (run with `--noconftest`, backend-tests venv):

- `tests/test_resilience_impact.py`: 43 passed
- `tests/test_resilience_watch.py`: 22 passed
- `tests/test_resilience_snapshot.py`: 19 passed
- `tests/test_resilience_plans.py`: 10 passed
- `tests/test_resilience_drills.py`: 8 passed
- `tests/test_resilience_audit.py`: 6 passed
- **Pure total: 108 passed, 0 failed** (single run of all six files)

DB-backed suites (canonical conftest, throwaway `*_test` database,
run in chunks because the shared VM kept OOM-killing full runs;
each chunk passed on its own):

- `tests/test_resilience_runtime.py`: **5 passed** — plan start creates
  the run + approvals + audit row and is idempotent; end-to-end
  approve → perform → completed with exactly one verified
  `execute_once` row per step; pause blocks execution until resumed;
  supplier switch needs owner approval (a plain member cannot see it
  in the inbox and gets 403 on decide); repeated pumps never execute
  a step twice.
- `tests/test_resilience_overview.py`: **6 passed** — empty scope;
  continuity map + ranking from the department node; restricted graph
  record invisible to a plain member (not in the ranking, not in the
  counts, not in the response) and visible to the admin; simulate
  endpoint scores a server outage over the map; audit export
  owner-only (403 for a member, CSV for the owner); plan-progress
  endpoint.
- **DB total: 11 passed, 0 failed.**
- Regression for the shared redactor change
  (`app/recovery/policy.py`): `tests/test_recovery.py::test_evidence_redaction_encryption_postmortem_and_db_guard`
  — **1 passed**. The regex widening is additive (more keys redacted)
  and breaks nothing.

Frontend: `npm test` 22 passed, 0 failed; `npm run typecheck` clean;
`npm run build` clean (built in ~20s).

## Gate status

- Alembic: no new migrations. Single head
  `0103_unified_studio_governance`; `alembic check` against the
  migrated test database: "No new upgrade operations detected."
- DB-backed resilience tests: **11 passed, 0 failed** (chunked runs;
  the shared VM OOM-killed two full-suite attempts at ~22 min each
  before any test in them executed — environmental, no code failure;
  each chunk passed on its own).
- Frontend: `npm test` 22 passed, 0 failed; `npm run typecheck` clean;
  `npm run build` clean (built in ~20s).
- Merge: Step AH is code-complete and its own gates are green. It
  still must NOT merge to main until the shared full-backend-suite
  gate is green on the merged HEAD (Agent 104's Step E verification
  owns that count) and the recovery redaction regression above is
  confirmed. The branch also needs a merge-order decision: it touches
  `frontend/src/components/ModuleWorkspaces.tsx`, which the
  frontend-redesign rounds are also editing (round 8 covers
  ModuleWorkspaces), and it shares the 0104/0105 migration-number
  space with the HR, AI-freshness and AK branches (Step AH itself
  mints no migration).

## Owner decisions still open (plan doc §7)

- Authoritative HR source for people/teams, and whether supplier/system
  records may be seeded from connectors.
- Who may approve a supplier switch and a data restore — proposed:
  owner only, with separation of duties from the proposer.
- Alert channel + quiet-hours rule — implemented default: critical
  (≥80) breaks quiet hours, everything else respects 22:00–07:00 PKT.
- Vendor-id → graph-supplier mapping (needed before finance vendor
  edges and finance rows can join the scope map).

## Known limits

- The scope map has no finance rows yet (no department link for finance
  data) — stated in the UI and the API response, not silent.
- Department-visible graph records whose department is not in the
  caller's departments are excluded from the projection; the Step G
  graph merge is the proper fix for the two-graph split.
- The watch has no live delivery worker yet (alerts are evaluated and
  formatted; delivery goes through Step AA scheduled reports / Step F
  WhatsApp when those land).
- Recovery plans attest human action; they do not perform restores,
  failovers or supplier switches themselves — those stay human-mediated
  by design.
