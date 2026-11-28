# Step K — Digital Twin v2: implementation plan

Status: **slices 1–5 done** (branch `agent/digital-twin-v2`, base
`step-e-gaps-wip` `ca956c2`). Step K v2 builds on **Step S** (company digital
twin, branch `agent/digital-twin`, HEAD `f5d4b1e`, slices 1–6 DONE) — it is
the same twin, grown up. It is **not** a second twin, and it does not redo
any Step S work. The v2 scope is master-plan v2 Step K ("Digital twin,
prediction, simulation, 28–40 h") plus the owner-approved Step S
description ("live model of teams, money flow, and what breaks if someone
is away").

Sources: `MASTER-PLAN.md` (J–N list: "Digital Twin"; O–W list: Step S),
`docs/step-s-digital-twin-plan.md` (Agent 24/55/81/92 — the foundation
this plan extends), `assessment/CHANGES-step-K.md` (Agent 92 — the Step S
changelog, read it first), `assessment/master-plan-v2.md` §4 (Live Company
Digital Twin), §5 (Predictive Continuity), §8 (Simulation / What-if
Engine), `docs/step-g-continuity-plan.md` (Step G — graph merge + drills),
`docs/step-al-whatif-plan.md` (Step AL — general what-if engine),
`docs/step-ah-resilience-plan.md` (Step AH — risk scoring + watch), and
the code survey in §2 (all paths relative to the repo root, read — not
guessed).

## 1. What Step K v2 must deliver (acceptance)

Taken from v2 §4/§5/§8, restated for the twin. Items 1–5 are Step S
acceptance, kept; the **bold** parts are what v2 adds.

1. **One continuously updated model.** People, teams, processes, systems,
   suppliers, data, agents, workflows, invoice batches and payments, and
   the links between them — one entity id, one scope. Adding or revoking a
   person, agent, system or supplier updates the twin without a manual
   redraw (outbox event → graph update). The same entity is never
   duplicated across the old module tables and the twin. **v2: any
   consumer can ask "what changed since the last confirmed twin, and is
   the twin still trustworthy?" — drift is a first-class, deterministic
   answer, not a feeling.**
2. **"What breaks if X is away?"** For a seeded employee, system and
   supplier, "unavailable 7 days" returns a stable, numbered process list
   with owners, dependencies, and money at risk. A process the caller is
   not allowed to see is omitted, and the count is not leaked. Removing a
   `backup_for` link or a permission changes the prediction on the next
   refresh. Predictions are explainable (which links and which live work
   items produced each impact). **v2: a prediction records the twin
   fingerprint it was computed from; drift beyond a threshold marks the
   prediction stale automatically, so nobody acts on a prediction the
   twin has outgrown.**
3. **What-if simulations, side-effect free.** "What if the Finance Manager
   is removed?", "What if the ERP is down?", "What if a supplier fails?"
   return impact (entities + processes) and a recovery plan (owners,
   grants, steps). Running a simulation writes no
   `platform_action_executions` in the dispatched state and does not
   change production records. The same simulation is deterministic for
   the same twin version. A recovery drill (Step G) can be compared to
   the simulation; differences are listed. **v2: there is ONE failure
   engine — Step AL's `app.whatif.simulator` — used by both AL and the
   twin. The twin keeps only its scenario library, service wiring, drill
   comparison, and API. The two engines must never diverge.**
4. **Freshness is visible.** A stale source is marked stale; a query says
   when the twin was last confirmed. No cross-tenant or out-of-scope node
   is visible to a restricted user (same `visible_scopes` as the rest of
   the platform). **v2: freshness rolls up into ONE twin health score
   (0–100) with a version history, so "how healthy is the twin?" has a
   number and a timeline, not just per-source badges.**
5. **Money flow is part of the model.** Invoice batches
   (`fin_invoice_batches`) and payments (`fin_payments`, `amount > 0`)
   appear as twin nodes with amounts, so "away" answers include money at
   risk — never a payment instruction. The twin **simulates only**: it
   never approves, dispatches, or pays anything. **v2: money drift is
   tracked per currency between twin versions (exposure deltas only).**
6. **NEW for v2 — twin-driven proposals.** A twin finding (e.g. an
   uncovered critical process, a supplier single point of failure, a
   backup whose holder lacks the required permission) can become an
   approval-gated action proposal. Nothing executes without an approval
   bound to the exact action, single-use, expiring, separation of
   duties, dispatched through `execute_once` with read-back
   verification. A proposal is a suggestion until a human approves it.
7. **NEW for v2 — drill reconciliation.** Twin simulations are compared
   against real Step G recovery drills on a schedule; differences go to
   a review queue, never auto-applied. The twin learns from drills; it
   never rewrites drill records.

## 2. Overlap check — what already exists (do not rewrite)

Read from the code, not from memory. Every row below was verified
against the named branch/worktree before this plan was written.

| Area | Existing code | What it does | K v2 reuse / gap |
|---|---|---|---|
| Twin snapshot builder | Step S `app/digital_twin/snapshot.py` (`build_snapshot`, `build_twin_snapshot`, `source_freshness`) | Merges an authorized company-graph projection + continuity nodes/links + finance batches/payments + workflow runs into one plain-dict snapshot; company graph wins on conflicts; fingerprint + `last confirmed`; payee account never copied | Reuse as-is. K adds drift + persisted versions **on top** of the snapshot, never inside the builder |
| Live sync feeds | Step S `app/digital_twin/feeds.py` (`translate_event`, `apply_feed`, `register_twin_feeds`) | 5 real outbox events → twin through the governed-change path only (pending graph-change proposals, never a direct write) | Reuse as-is. K adds the remaining feeds from the S plan §3 item 2 (membership changes, workflow-run lifecycle, Agent Studio runs, connector syncs) in slice 7 |
| Absence simulation | Step S `app/digital_twin/impact.py` (`simulate_absence`, `money_at_risk`) | Person/team absence → at-risk entities, backup coverage, money at risk per currency, recovery waves, cycle-safe BFS depth 6 | Reuse as-is. K adds prediction-staleness-on-drift (slice 3) |
| Horizon predictions + watch | Step S `app/digital_twin/horizon.py` (`predict_absence`, `refresh_predictions`, `evaluate_horizon_watch`, `snapshot_fingerprint`, `fingerprint_changed`) | "Unavailable N days" with 0–100 advisory risk score + explainable factors; fingerprint-gated refresh; rules-as-data watch, SHA-256 dedupe + cooldown, quiet hours 22:00–07:00 PKT | Reuse as-is. K adds the aggregate health score (slice 3). Note: horizon only answers *whether* the twin changed (boolean) — it never says *what* changed or *how much*. That is slice 1 |
| What-if engine (twin) | Step S `app/digital_twin/whatif.py` (`simulate_failure`, `run_whatif`, `compare_drill`, `build_scenario`, rehearsal) | 6 scenario kinds; failed/degraded/exposed states; all-providers rule; ranked recovery; rehearsal dry-run; drill comparison | Reuse until slice 4, then **delegate** to AL's engine (see §1 item 3). The twin keeps its scenario library + `compare_drill` + API |
| What-if engine (general) | Step AL `app/whatif/simulator.py` (`simulate`, `build_scenario`, `list_templates`, `money_at_risk`) + `adapter.py` + `drills.py` + `rehearsal.py` | Same states, same all-providers rule, same recovery ranking as the twin engine, plus a general scenario-template library and a `POST /api/whatif/{tenant_id}/simulate` endpoint | The **one** engine after slice 4. Differential test must prove identical results before the twin engine is retired |
| Twin service + API | Step S `app/digital_twin/service.py` + `app/api/routes/digital_twin.py` | Read-only overview / what-if / predict-absence; tenant scope via `scope_for` → 404/401; zero-side-effect proof | Reuse; K adds read-only version/drift/health endpoints (slices 2–3) |
| Twin UI | Step S `frontend/src/digital-twin/` (TwinWorkspace, api, types) | Entity/relationship/money-at-risk stats, freshness badges, live map, what-if + absence panels | Reuse; K adds drift timeline, health gauge, version history, proposal inbox (slice 8) |
| Risk scoring + watch | Step AH `app/resilience/impact.py` (`score_disruption`, `assess_risk`, `rank_risks`, `recovery_progress`) + `app/resilience/watch.py` (rules as data, SHA-256 dedupe + cooldown, quiet hours) + `app/resilience/snapshot.py` (read-only builder, company-graph wins) | Advisory 0–100 risk scores; watch rules; read-only snapshots | Reuse the **watch pattern** for twin health alerts (slice 3). Do NOT rebuild a second risk engine. K never duplicates AH scoring |
| Continuity drills / backup / restore | Step G `app/continuity/*` (backup.py, restore.py, unified.py; slices 2–4 DONE by Agent 34; slices 5–8 by Agent 114) | Real pg_dump backups + manifest, restore verification into `*_test` DBs, activations on the unified engine, drills + graph merge in progress | Drills are the **ground truth** for slice 5 reconciliation. Graph merge (G slice 6) is the dependency K must not duplicate — K consumes the merged graph, it does not merge it |
| Control room | Step Q `app/control_room/service.py` + Step AF `app/control_room/alerts.py`, `charts.py` | Live status API, alert rules, charts, dispatch worker | Twin health surfaces **there**; K builds no second dashboard |
| Company graph intelligence | `app/company_graph/intelligence.py` (`impact()`, `quality()`, `query_plan`) | Outage semantics ("failed node"), path confidence, gaps/stale/conflicts; no money amounts, no time horizon, no workflow linkage | K uses twin simulation, not a third impact engine |

## 3. What is genuinely new for v2 (not in S / AL / G / AH)

1. **Drift detection (slice 1, this agent).** A deterministic diff
   between two twin snapshots: added / removed / changed entities and
   relationships (attribute-level), per-currency money deltas, a 0–100
   drift score with documented weights, and a trust verdict
   (`trustworthy` / `review` / `stale`). Step S can only say *whether*
   the twin changed; nothing anywhere says *what* changed or *how
   much*.
2. **Persisted twin versions + drift history (slice 2).** Snapshots are
   built on demand today and thrown away. v2 stores versions
   (fingerprint + drift report + `as_of`) so drift has a timeline and
   predictions can be checked against the twin they were computed from.
3. **Twin health score + watch (slice 3).** One aggregate 0–100 health
   number from drift + source freshness, with rules-as-data alerts
   (AH pattern), quiet hours, cooldown, SHA-256 dedupe.
4. **Engine unification (slice 4).** Twin what-if delegates to
   `app.whatif.simulator`; differential test proves identical results.
5. **Drill reconciliation loop (slice 5).** Scheduled twin-vs-drill
   comparison; differences → review queue, never auto-applied.
6. **Twin-driven proposals (slice 6).** Finding → approval-gated
   proposal → `execute_once` dispatch with read-back. Never
   auto-executed; separation of duties; full audit trail.
7. **Multi-source trust scoring + conflict reporting (slice 7).**
   Per-source trust weights; conflicts listed with both sides, never
   silently resolved by "graph wins" alone. Remaining live-sync feeds
   (membership, workflow-run lifecycle, Agent Studio runs, connectors).
8. **Twin UI v2 + docs + gates (slice 8).** Drift timeline, health
   gauge, version history, proposal inbox; `CHANGES-step-K-v2.md`;
   full-suite / Alembic / frontend gates; merge readiness.

## 4. Safety / security controls (non-negotiable, carried from S + repo rules)

- **The twin simulates only.** Read-only views by default. It never
  approves, dispatches, grants access, restores data, switches a
  supplier, or pays. Money figures are exposure ("money at risk"),
  never payment instructions.
- **Any twin-driven action** (slice 6) goes through the platform:
  approval bound to the exact action hash, expiring, single-use,
  separation of duties (nobody approves their own proposal),
  dispatched through `execute_once` with read-back verification, full
  audit trail. A twin finding is a suggestion until a human approves it.
- **Finance amounts stay owner-only** in the twin until the owner
  decides otherwise (Step S default, kept). Drift money deltas are
  exposure totals, never account numbers.
- **Snapshot text is untrusted data.** Names, attributes, and evidence
  strings are compared as data only. Drift never `eval`s, `exec`s, or
  interprets text as an instruction; text can never grant a
  permission, change a payee, or change an amount. Amounts come only
  from numeric `amount` attributes (non-numeric / negative → error).
- **Restricted users see only their authorized projection.**
  Entity/relationship counts never leak hidden nodes (Step S rule, kept
  for drift reports and health scores alike).
- **A `backup_for` link is a recorded assignment, never an access
  grant.** Effective-permission gaps are listed, not bridged.
- **No secrets** in the twin, its snapshots, its drift reports, its
  logs, or its tests. Test data is synthetic (fake people, systems,
  amounts). Bounded inputs: drift refuses snapshots over
  `MAX_ENTITIES` / `MAX_RELATIONSHIPS` instead of grinding the VM.
- Every lookup/dispatch through `app/platform`
  (`resolve_principal`, `authorize`, `require` → 404 non-members, 403
  members without permission). Tests only on throwaway `*_test`
  databases. Small logical commits. No live folder / live DB / `.env` /
  uploads / data touched. No push — Master Agent pushes.

## 5. Build order (each slice finished + tested before the next)

1. ✅ **Slice 1 — twin drift detector (this agent).**
   `app/digital_twin/drift.py` + `tests/test_digital_twin_drift.py`.
   Pure functions, stdlib only, no migration, no route, no production
   code changed. Deterministic diff + 0–100 score + trust verdict +
   one-line summary. Imports nothing from Step S modules (they do not
   exist on this branch yet) — the fingerprint algorithm is
   reimplemented identically so fingerprints match
   `horizon.snapshot_fingerprint` once S merges.
2. ✅ **Slice 2 — persisted snapshot versions + drift history.**
   Migration `0106_twin_snapshot_versions`: `twin_snapshot_versions`
   (fingerprint, `as_of`, snapshot JSON, drift report JSON, source
   freshness) + read-only API
   (`GET /api/digital-twin/{tenant_id}/versions`,
   `GET .../versions/{fingerprint}/drift`). Versions are append-only;
   retention default 30 versions / 90 days (owner decision §6). No
   record endpoint — versions are recorded by calling
   `app.digital_twin.versions.record_version` (wired up by a later
   slice). See §9 for the implementation notes.
3. **Slice 3 — twin health score + watch.** ✅ Aggregate 0–100
   from the latest drift score + per-source freshness; rules as data
   (AH pattern); quiet hours 22:00–07:00 PKT; cooldown + SHA-256
   dedupe; predictions older than the drift threshold marked stale.
   See §10 for the implementation notes.
4. **Slice 4 — engine unification.** ✅ Twin what-if delegates to
   `app.whatif.simulator.simulate`; twin keeps its scenario library,
   `compare_drill`, service wiring, API. Differential test: same
   snapshot + scenario → identical results from both engines, then the
   twin-native engine is retired. Needs AL merged first (or a shim +
   differential test against the AL branch). See §11 for the
   implementation notes.
5. **Slice 5 — drill reconciliation loop.** ✅ Scheduled comparison
   of twin simulations vs real Step G drills; differences → review
   queue (read-only list + acknowledge); never auto-applies, never
   rewrites drill records. See §12 for the implementation notes.
6. **Slice 6 — twin-driven proposals.** ✅ Finding → proposal via
   platform approvals (exact-action hash, single-use, expiring,
   separation of duties) → `execute_once` dispatch with read-back.
   Audit trail for every proposal, approval, and dispatch. Also
   closes the slice-5 owed scheduler/CLI wiring for
   `evaluate_reconciliation`. See §13 for the implementation notes.
7. **Slice 7 — multi-source trust scoring + conflict reporting +
   remaining feeds.** Per-source trust weights; conflicts listed with
   both sides; membership / workflow-run / Agent Studio / connector
   feeds through the governed-change path.
8. **Slice 8 — Twin UI v2 + docs + gates.** Drift timeline, health
   gauge, version history, proposal inbox (read-only until approval);
   `assessment/CHANGES-step-K-v2.md`; full-suite / Alembic / frontend
   gates; merge readiness.

## 6. Owner decisions needed (safe defaults in place)

- **Finance amounts in the twin:** owner-only today (Step S default).
  May invoice-batch/payment amounts appear for authorized finance
  viewers, or stay owner-only?
- **Authoritative HR/membership source** for people/teams in live
  sync; may supplier/system records be seeded from connectors?
  (Default today: live memberships only; nothing seeded.)
- **Drift trust thresholds:** proposed defaults implemented in slice 1
  — `trustworthy` ≤ 25, `review` ≤ 60, `stale` > 60 (or any stale
  critical source). Confirm or change the bands.
- **Which twin findings may become proposals, and who approves them?**
  Proposed: owner only for money/privilege/data-restore actions;
  department managers for coverage/backup actions. Confirm.
  **Implemented safe default (slice 6, §13):** owner
  (`company_graph.role.owner`) approves money/privilege/data-restore
  actions (`twin.apply`); the company-graph approver
  (`company_graph.role.approver`) approves coverage/backup actions
  (`twin.apply.coverage`) — the approver stands in for department
  managers until Step J lands the manager mapping. Owners are a
  superset of approvers, so an owner may approve either class.
- **Snapshot retention:** proposed 30 versions / 90 days. Confirm or
  change.

## 7. Merge notes (read before merging this branch)

- **Step S (`agent/digital-twin`) must merge to main before K v2, or
  K v2 rebases onto it.** This branch is based on `ca956c2` WITHOUT
  the Step S files; slice 1 imports nothing from S modules (stdlib
  only) so it stands alone on this base.
- **`app/digital_twin/__init__.py`:** this branch creates a minimal
  one (drift exports only). At merge with S, **KEEP the S
  `__init__.py` content and ADD the drift names** to its imports and
  `__all__`. Do not take either file wholesale — the S file imports
  modules this branch does not have, and this branch's file lacks the
  S exports.
- **No migration in slice 1** — no 0104/0105 collision from this
  slice. Slice 2 minted `0106_twin_snapshot_versions`
  (down_revision `0103`): at slice-2 time no branch claimed 0106,
  0107, or 0108 (verified by scanning every branch's
  `migrations/versions/`). The known 0104 (HR, WhatsApp AE, AI
  freshness, continuity, AB, AA, language, foundry, finance
  autopilot, scheduled reports) and 0105 (AK, AI, AB) collisions are
  renumbered at merge; if another branch lands 0106 first, renumber
  this revision at merge — the table is self-contained (no foreign
  keys into other new tables).
- **Slice 4 needs Step AL merged first** (or a compatibility shim +
  differential test against the AL branch). Do not delegate to an
  engine that is not on main yet. Slice 4 shipped exactly that shim +
  differential test (see §11): the delegation module lazy-imports
  AL's simulator and raises `EngineUnavailable` naming the missing
  module until AL merges; the differential test runs both real engines
  from their branches and proves byte-identical results for every
  failure kind. At rebase onto Step S, this branch's
  `app/digital_twin/whatif.py` REPLACES the Step S file: retire
  `simulate_failure` and its helpers (`_recovery_options`,
  `_edge_view`, `_expand_seeds`, `_validate_seeds`, `_validate_args`,
  `_as_decimal`); keep `build_scenario` / `SCENARIOS`,
  `_normalize_absence` + helpers, `_rehearsal`, `compare_drill`, and
  the `run_whatif` result shape. The Step S tests that import
  `simulate_failure` move to the differential test and to AL's
  `test_whatif_simulator.py` at rebase; the `run_whatif` /
  `compare_drill` cases keep passing unchanged. The twin-native
  engine is NOT retired on this branch (it lives on the S branch) —
  retirement is the merge-time action, gated on this differential
  test passing.
- **Slice 5 needs Step G drills** (Agent 114, slices 5–8) and the
  drill record shape reconciled with `compare_drill`.
- **Slice 6 minted `0110_twin_proposals`** (down_revision
  `0109_twin_reconciliation_findings`; verified free across all
  worktrees at slice-6 time; single Alembic head
  `0110_twin_proposals`). Renumber 0109/0110 at merge together with
  the other collisions. At merge with Step S, the `__init__.py`
  merge keeps the S content and ADDS the K names — drift + health +
  whatif + reconciliation + proposals. The platform
  `APPROVAL_REQUIRED` additions (`twin.apply`,
  `twin.apply.coverage`) and the company-graph role mirror additions
  in `app/platform/sync.py` merge with the platform files as-is
  (no other branch touches those two spots on this base).
- Full backend suite + Alembic + frontend gates are owed before any
  K v2 slice merges (same quiet-VM gate as Step E).

## 8. Slice 1 — what was implemented (this branch)

**Module:** `app/digital_twin/drift.py` (pure functions; stdlib only —
`hashlib`, `json`, `decimal`. No imports from `app.db`, no I/O, no
network, no secrets, no imports from Step S modules.)

- `snapshot_fingerprint(snapshot)` — SHA-256 over the canonical JSON
  of `entities` + `relationships` (`sort_keys=True`, `default=str`),
  byte-identical to `horizon.snapshot_fingerprint` for the same
  snapshot, so fingerprints agree once Step S merges. A stored
  `fingerprint` field is trusted when present and non-empty.
- `diff_snapshots(old, new)` — deterministic diff:
  - Validation first (same rules as the snapshot builder): snapshots
    must be dicts; entities/relationships lists; entity ids and
    relationship ids non-empty strings; no duplicate entity or
    relationship ids; every relationship endpoint exists (dangling →
    `ValueError`); money entities (`invoice_batch` / `payment`) carry
    a numeric, non-negative `amount` (missing / string / negative →
    `ValueError`); attributes must be JSON-serializable; snapshots
    over `MAX_ENTITIES` (50,000) / `MAX_RELATIONSHIPS` (100,000) →
    `ValueError`.
  - Entity diff: `added` / `removed` ids (sorted), `changed` entries
    with kind, name, and the top-level attribute keys that differ
    (scalar old/new values included when short; nested values recorded
    as `"<changed>"` so reports stay bounded).
  - Relationship diff: `added` / `removed` ids (sorted), `changed`
    entries with old/new `kind` and `confidence`.
  - Money: per-currency totals old vs new (Decimal sums, float
    output — same convention as `impact.money_at_risk`) plus
    `batch_count` / `payment_count` and their deltas.
  - `drift_score` 0–100, documented weights: removed entities 10
    each (cap 40); added entities 2 each (cap 10); changed entities 3
    each (cap 15); removed relationships 3 each (cap 15); added
    relationships 1 each (cap 5); changed relationships 2 each
    (cap 10); money movement up to 20 (largest per-currency
    |delta| share of the larger total). Deterministic for the same
    pair of snapshots.
  - `trust`: `trustworthy` (score ≤ 25), `review` (≤ 60), `stale`
    (> 60, or the new snapshot lists a stale source). `changed` is
    true exactly when the fingerprints differ.
  - Result shape: `fingerprint_old` / `fingerprint_new`, `as_of_old` /
    `as_of_new`, `entities`, `relationships`, `money`, `counts`,
    `drift_score`, `trust`, `stale_sources`, `changed`.
- `drift_summary(report)` — one deterministic line: score, trust,
  entity/relationship counts, per-currency money deltas, stale-source
  count. Counts and totals only — no attribute values, no secrets.
- Hostile text is data: names/attributes that look like instructions
  ("ignore previous instructions", "approve all payments") are
  compared as plain strings, appear in the diff as data, and can never
  grant a permission, change a payee, or change an amount — the module
  has no approval, dispatch, or payment path at all.

**Tests:** `tests/test_digital_twin_drift.py` — **38 passed, 0 failed**
(pure run, `--noconftest`, 3.22 s; command:
`TMPDIR=/home/hatch/tmp-pip .venv/bin/python -m pytest
tests/test_digital_twin_drift.py --noconftest -q -p no:cacheprovider`).
First run was 37 passed + 1 failed — the failure was a REAL bug in
the module, not the test: `diff_snapshots` validated `stale_sources`
only on the new snapshot, so a malformed old snapshot slipped
through. Fixed by validating `stale_sources` inside
`_validate_snapshot` (both sides); re-run green. Fingerprint
cross-check: `drift.snapshot_fingerprint` is byte-identical to Step S
`horizon.snapshot_fingerprint` for the same snapshot (verified
directly against the Step S code: content snapshot, stored
fingerprint, and empty snapshot all MATCH).
Covers: empty/identical snapshots (score 0, trustworthy); added /
removed / changed entities (name, kind, scalar + nested attributes);
added / removed / changed relationships (kind, confidence); money
deltas (added batch, amount change, new currency, batch/payment
counts); fingerprint stability + equality; determinism (same inputs →
  byte-identical report); trust bands; summary text; all validation
errors; hostile-instruction text treated as data; size caps.

**What slice 1 deliberately does NOT do:** no live sync, no graph
writes, no new tables, no migration, no API route, no UI, no permission
grants, no `execute_once` dispatch, no prediction or simulation. It is
the math the later slices (versions, health, proposals) build on.

## 9. Slice 2 — what was implemented (this branch)

**Model:** `app/db/twin_snapshot_models.py` — `TwinSnapshotVersion`
(`twin_snapshot_versions`): `id` UUID PK, `seq` BIGINT IDENTITY
(monotonic insertion order — `created_at` uses `now()`, the
transaction timestamp, so versions recorded in one transaction tie
on it; `seq` is the deterministic "latest" / retention order),
`tenant_id` UUID, `fingerprint` `String(64)`, `as_of` timestamptz, `snapshot` JSONB,
`drift_report` JSONB nullable, `source_freshness` JSONB (default
`[]`), `created_at` timestamptz server default `now()`. Unique
`(tenant_id, fingerprint)`; check `fingerprint ~ '^[0-9a-f]{64}$'`;
index on `(tenant_id, created_at)`. Registered in
`app/db/models.py` (bottom imports, same pattern as the other
per-feature model modules).

**Migration:** `migrations/versions/0106_twin_snapshot_versions.py`
(down_revision `0103`). Downgrade drops the table — that loses
version history, not the twin: versions are derived data, rebuildable
by recording snapshots again and recomputing drift.

**Service:** `app/digital_twin/versions.py` (async, takes an
`AsyncSession`; pure helpers unit-testable without a DB):

- `record_version(db, tenant_id, snapshot, *, as_of=None,
  source_freshness=None, now=None)` — validates the snapshot with
  slice 1's rules (`drift.validate_snapshot`, added as a public
  wrapper in this slice), resolves `as_of` (explicit arg, else the
  snapshot's `as_of`, else now; ISO strings incl. `Z` accepted),
  normalizes `source_freshness` (list of dicts with a string `name`),
  then stores. **Idempotent:** the same snapshot for the same tenant
  returns the existing version, no new row. The drift report is
  computed against the latest previous version (`None` for the
  first). Retention pruning runs in the same transaction.
- **Fingerprint is recomputed from content.** Any stored
  `fingerprint` field on the incoming snapshot is ignored, and the
  stored copy drops it — the row key is always the content hash, so a
  tampered label cannot misidentify a version.
- `prune_versions(db, tenant_id, *, keep=30, max_age_days=90)` —
  deletes versions older than 90 days or outside the newest 30;
  returns the count deleted.
- `list_versions(db, tenant_id, *, limit=30)` — newest-first
  summaries (limit clamped 1–100). Summaries never include the full
  snapshot JSON or money amounts.
- `get_version` / `get_drift` — one version / its stored drift
  report (`drift: null`, `is_first_version: true` for the first
  version). Malformed fingerprints raise `ValueError`.
- `redact_money_for_viewer(drift_report)` — copy with `money: null`
  and `money_redacted: true` (`None` in → `None` out).
- `version_summary(row)` — bounded summary dict.

**API:** `app/api/routes/digital_twin.py` (new file on this branch;
at merge it combines with the Step S routes file, which has
overview / what-if / predict-absence):

- `GET /api/digital-twin/{tenant_id}/versions` — `scope_for` first
  (404 unknown tenant / non-member, 401 unauthenticated); `limit`
  query 1–100, default 30. Returns `{tenant_id, versions, count}`.
- `GET /api/digital-twin/{tenant_id}/versions/{fingerprint}/drift`
  — same scope gate; malformed fingerprint → 422; unknown version →
  404. Money shown only when the caller's graph role is `owner`;
  other members get the redacted report. Registered in `app/main.py`
  next to the company-graph router.

**Tests:** `tests/test_digital_twin_versions_pure.py` — pure tests
(`--noconftest`): `validate_snapshot`, `redact_money_for_viewer`,
`version_summary`, fingerprint recompute, `as_of` parsing — **8
passed, 0 failed**. `tests/test_digital_twin_versions.py` — DB tests
on a throwaway `*_test` database: record first/second version,
idempotency, list order + limit, get drift, retention pruning (30-cap +
90-day), validation errors write nothing, and the API tests
(owner sees money, viewer gets redacted money, unknown tenant /
non-member 404, unauthenticated 401, bad fingerprint 422, unknown
fingerprint 404, first version drift `null`) — **all 23 tests passed**
(24 collected items: the 23 tests plus the `test_engine_options`
import pytest collects from the conftest import — it passes too).
Slice-1 regression `tests/test_digital_twin_drift.py` re-verified:
**38 passed, 0 failed** (`--noconftest`). Alembic roundtrip green
(upgrade head → downgrade base → upgrade head, `alembic check`
clean, single head `0106_twin_snapshot_versions`). Note: the
full-file DB run was twice interrupted by the VM (once by a daemon
restart, once OOM-killed at 23/24 with 0 failures); the last test
was re-verified green standalone (1 passed, 327s under load) — every
test passed at least once against the final code, zero code failures.

**What slice 2 deliberately does NOT do:** no record endpoint (no
write route — recording is a service call for the twin service /
worker to wire up later); no snapshot building (the caller passes an
already-authorized snapshot — S's `build_twin_snapshot` when it
merges); no health score (slice 3); no UI; no permission grants; no
`execute_once` dispatch; no prediction or simulation.

## 10. Slice 3 — what was implemented (this branch)

**Score:** `app/digital_twin/health.py` (pure functions; stdlib
only, except the drift module itself — no imports from `app.db`, no
I/O, no network, no secrets):

- `compute_health(*, drift_report, source_freshness, fingerprint,
  as_of)` — `100 − (drift_score + freshness penalty)`. Freshness:
  10 points per stale source, cap 30 (stale = freshness entry with
  `stale: true` or `status: "stale"`, plus the drift report's
  `stale_sources`, deduped). The drift score already includes money
  movement (up to 20 points), so the reported components split it
  back out: `money` is recomputed from the report's per-currency
  totals with the same formula slice 1 uses, `drift` is the
  remainder — the two sum to the drift score (below its 100 cap).
  The first version has no drift report, so its health is freshness
  only (100 when fresh).
- Bands aligned with the drift trust bands: `healthy` ≥ 75 (drift
  ≤ 25, trustworthy), `degraded` ≥ 40 (drift ≤ 60, review),
  `critical` < 40 (drift > 60, stale). The assessment carries score
  points and counts only — no money amounts — so it is safe to show
  to any tenant member.
- `health_summary(assessment)` — one deterministic line.
- `prediction_is_stale(prediction, latest_assessment, *,
  threshold=60)` — a prediction is stale when its fingerprint
  (`fingerprint` or `twin_fingerprint`) differs from the latest
  version's AND the latest drift score exceeds the threshold
  (default 60, the stale trust band). Fail-closed: a prediction with
  no fingerprint, or one that does not match the only recorded
  version (drift unknown), is stale; a matching fingerprint is never
  stale; no recorded versions → not stale (nothing to compare
  against). Pure — the Step S horizon prediction store is not on
  this branch yet, so this is the rule slice 4+ wiring calls.

**Watch:** `app/digital_twin/health_watch.py` (pure; follows the
Step AH / Step L pattern, `app/predictive_continuity/watch.py`):

- `HealthRule` / `HealthAlert` dataclasses; rules are data.
  `DEFAULT_RULES` (4): `health_critical` (critical, health < 40,
  breaks quiet hours, 1 h cooldown), `health_degraded` (warning,
  health < 75, 2 h), `twin_stale` (critical, trust == `stale`,
  breaks quiet hours, 1 h), `stale_source` (warning, ≥ 1 stale
  source, 4 h).
- `evaluate_health_watch` — SHA-256 dedupe fingerprint per (rule,
  tenant, twin version), cooldown via a caller-supplied `sent_at`
  map, quiet hours 22:00–07:00 Asia/Karachi where only
  `breaks_quiet_hours` rules fire and the rest are reported as
  `deferred` (never silently dropped); cooldown hits are reported as
  `suppressed`. Returns `{"alerts", "suppressed", "deferred",
  "evaluated_at", "fingerprint"}`. Alert detail is score points and
  counts only — no amounts, no entity names.
- `is_quiet_hour`, `validate_health_rules`, `watch_fingerprint`,
  `format_watch_message` — same conventions as the L watch.

**Persistence:** `app/db/twin_health_models.py` —
`TwinHealthAlert` (`twin_health_alerts`): tenant, rule key,
severity, dedupe fingerprint (unique per tenant), health/drift
scores, trust, title, detail, `evaluated_at`, `created_at`.
Migration `0107_twin_health_alerts` (down_revision
`0106_twin_snapshot_versions`; 0107 verified free across all
worktrees at slice time — 0106 is claimed by four branches, all
renumbered at merge). `app/digital_twin/health_records.py` —
`latest_health` / `health_history` (per-version assessments from
stored rows), `record_health_alerts` (idempotent per fingerprint;
a refire after cooldown refreshes `evaluated_at` instead of
duplicating the row, so cooldown memory advances),
`list_health_alerts`, `prune_health_alerts` (newest 100 / 90 days),
`evaluate_and_record_health_watch` (loads latest health, builds
`sent_at` from stored rows' `evaluated_at`, evaluates, records,
prunes; returns the evaluation + `health` + `recorded`; with no
versions recorded it fires nothing). No write route — the twin
service / worker calls the evaluator on a schedule.

**API** (read-only, same `scope_for` gates as the slice-2
endpoints; no amounts anywhere): `GET
/api/digital-twin/{tenant_id}/health` (404 when no version is
recorded yet), `GET .../health/history` (newest first, limit 1–100,
default 30), `GET .../health/alerts` (newest first, limit 1–100,
default 30). Registered in `app/digital_twin/__init__.py`
(health + watch names; at merge, ADD these to the S `__init__.py`
alongside the drift names — see §7).

**Tests:** `tests/test_digital_twin_health_pure.py` — **23 passed,
0 failed** (`--noconftest`, worktree venv). Covers band boundaries
(drift 25 → 75 healthy; 26 → 74 degraded; 60 → 40 degraded; 63 →
37 critical), money decomposition (drift 11 → structural 8 +
money 3), freshness penalty + 30 cap, stale-source merge/dedupe,
summary text, all validation errors, prediction staleness
(match / within-threshold / over-threshold / no fingerprint /
first-version / no versions / custom threshold), watch fire /
defer / suppress / cooldown expiry / new-version refire, fingerprint
stability + scoping, quiet-hour windows incl. naive datetimes,
rule validation, disabled rules, message format.
`tests/test_digital_twin_health.py` — DB + API tests on a throwaway
`*_test` database: **10 service tests + 9 API tests, all passed**
(run in two split runs under VM load — the full-file run was
OOM-killed at 18/19 with 0 failures; service run 11 passed incl.
the conftest pseudo-test in 255.61 s, API run 9 passed in 621.22 s).
Slice-1 regression 38 passed + slice-2 pure 8 passed
(`--noconftest`); slice-2 DB file re-run after a daemon restart
killed the first attempt (SIGTERM, no output): **24 passed**
(23 tests + the conftest pseudo-test) in 469.07 s.
Alembic roundtrip GREEN (upgrade head → downgrade base → upgrade
head, `alembic check` clean, single head `0107_twin_health_alerts`).

**What slice 3 deliberately does NOT do:** no record endpoint for
versions (unchanged); no evaluation endpoint (the watch is evaluated
by a service call, not HTTP); no alert acknowledgement (the model
has no acknowledged state — a later slice adds it if the UI needs
it); no UI; no permission grants; no `execute_once` dispatch; no
prediction or simulation. The health score is advisory: it never
approves, dispatches, or pays anything.

## 11. Slice 4 — what was implemented (this branch)

**Module:** `app/digital_twin/whatif.py` (pure functions; stdlib only,
except the drift module itself — no imports from `app.db`, no I/O, no
network, no secrets). The twin's what-if after unification: scenario
library + delegating dispatcher + drill comparison. It contains NO
failure engine of its own — that is the point of the slice.

- **Routing.** Failure kinds (`system_outage`, `supplier_failure`,
  `cyber_incident`, `site_unavailable`, `compound`) delegate to Step
  AL's `simulate` (injected as `engine`, else lazily imported from
  `app.whatif.simulator`). Absence kinds (`employee_absence`,
  `team_absence`) run Step S's `simulate_absence` (injected as
  `absence_engine`, else lazily imported from
  `app.digital_twin.impact`) and are normalized by the ported
  `_normalize_absence` (with `_severity`, `_entity_owner`,
  `_recovery_waves`, `_rehearsal` — the twin-kept wiring, verbatim
  from Step S). Without an injected engine and before the owning
  branch merges, `EngineUnavailable` (a `RuntimeError`) names the
  missing module instead of failing obscurely.
- **Enrichment.** AL's result lacks two twin-public fields, so the
  dispatcher adds them: `fingerprint` (the drift/horizon fingerprint
  of the snapshot, via `drift.snapshot_fingerprint`) and `covered_by`
  per affected entry (derived from the `activate_backup` recovery
  options). The enrichment copies the result; the engine's dict is
  never mutated. The dispatcher builds the rehearsal block itself
  from the recovery waves (one place, both paths), so the twin's
  rehearsal shape stays authoritative.
- **Scenario library.** `SCENARIOS` / `build_scenario` /
  `list_templates` / `template_name` — ids, names, kinds, and default
  horizons byte-identical to both engines' libraries, so drills, the
  twin UI, and the what-if engine share one vocabulary.
- **`compare_drill`** — ported verbatim from Step S (simulation vs a
  Step G drill record; never re-runs or re-scores anything).
- **Validation.** The dispatcher validates the scenario shape itself
  (same messages as both engines); snapshot/seed validation is the
  engine's own (Step S `impact._validate_snapshot` and Step AL
  `simulator._validate_snapshot` are the same rules with the same
  messages), so a snapshot valid for one engine is valid for the
  other and error behaviour matches.

**Why absence stays on the twin engine (proven, not assumed).** The
probe that preceded this slice ran both real engines on one rich
snapshot for all 8 scenario kinds. All 6 failure kinds were
byte-identical after enrichment; both absence kinds diverged by
design: a covered process is `degraded` for the twin (Bilal covers
it) and `failed` for AL (no backup coverage in propagation); the
team node itself appears in AL's affected list and never in the
twin's; seed recovery notes and uncovered reasons differ. Delegating
absence to AL would silently change twin answers — so the dispatcher
never routes an absence scenario to AL, and the differential test
pins both facts.

**Tests:** `tests/test_digital_twin_whatif.py` — **21 passed, 0
failed** (`--noconftest`, worktree venv, 2.10 s). Covers the library,
dispatcher routing with recording fakes (failure → engine, absence
→ absence engine, never crossed), rehearsal numbering, validation
errors, `EngineUnavailable` naming the missing module (both engines
unimportable on this branch), malformed engine results, the real AL
engine injected by file path (all failure kinds incl. the
all-providers single-vs-both supplier cases, cyber `exposed`,
compound, multi-currency money, dependency cycle blocking all
waves, depth-2 truncation, invalid-snapshot errors), the real Step S
absence engine injected by file path (covered → degraded, uncovered
→ failed, team members fail, team node never affected), and
`compare_drill` (match / affected / money / wave differences +
validation errors). `tests/test_digital_twin_whatif_differential.py`
— **18 passed, 0 failed** (`--noconftest`, worktree venv, 57.35 s).
Runs both real engines in isolated subprocesses (the S engine is
loaded by file path into a synthetic package namespace — the S
package `__init__` drags in heavy app imports that hang a bare
import; the AL simulator is stdlib-only). Covers: failure kinds
byte-identical on the rich snapshot (6 cases), the cycle snapshot,
and the full-depth chain snapshot; the absence divergence
documented with its specific expected differences; the K dispatcher
reproducing Step S's `run_whatif` output byte-identically for all 8
scenario kinds (failure via the injected AL engine, absence via the
injected Step S absence engine); error parity on 4 invalid
snapshots (both engines raise `ValueError`). Both differential
files skip when a sibling worktree is absent. Regressions
re-verified: slice-1 drift 38 + slice-3 health pure 23 = 61 passed
(worktree venv); slice-2 versions pure 8 passed (baseline venv —
the worktree venv still lacks sqlalchemy, pre-existing, noted by
Agent 123). No migration, no DB test, no API change in this slice;
Alembic head unchanged (`0107_twin_health_alerts`).

**What slice 4 deliberately does NOT do:** it does not retire the
twin-native engine (that lives on the Step S branch; retirement is
the merge-time action, gated on this differential test); it does not
merge Step AL or Step S; it adds no API route, no UI, no permission
grants, no `execute_once` dispatch, no prediction. The delegation
module imports cleanly on a branch where neither engine exists
(`EngineUnavailable` until they merge).

## 12. Slice 5 — what was implemented (this branch)

**The shape problem (verified from code, not assumed).** The twin
and Step G compare a simulation against a drill in two different
vocabularies, and neither side sees the other's differences:

- The twin's `compare_drill` (`app/digital_twin/whatif.py`)
  compares a `run_whatif` result with a drill observation in twin
  shape (`observed_affected` / `observed_money_at_risk` /
  `observed_recovery_waves`) and reports affected-set, money-total,
  and recovery-wave differences as plain strings.
- Step G's `compare_drill_to_simulation`
  (`app/continuity/drills.py` on the `agent/stepg-continuity`
  branch, Agent 114/144 in flight) compares a drill run with a
  simulation forecast on timing (RTO / MTPD / RPO per resource) and
  reports differences as `{code, resource_id, detail}` dicts.

Slice 5 reconciles the two shapes into ONE review-queue finding
format. It takes Step G's `drill_comparison` output as a plain
dict — it does **not** import `app.continuity`, so the slice stands
alone on this branch until G merges; at merge, the caller passes
G's dict straight in and nothing here changes.

**Pure module:** `app/digital_twin/reconciliation.py` (stdlib only,
except the twin's own `compare_drill`):

- `reconcile(simulation, drill_observation, *, tenant_id,
  drill_run_id, simulation_fingerprint=None, g_comparison=None,
  scenario_kind=None)` — runs the twin comparison, adapts the G
  comparison when supplied, merges both sides into one
  deterministic finding list, deduped by fingerprint, capped at
  `MAX_FINDINGS` (500, with `truncated: True` when hit). `matches`
  is True only when neither side reports any difference. The
  simulation fingerprint is required (explicit arg or the
  simulation's own) — findings must be tied to the twin version
  they were computed from. Returns the report plus `twin_matches` /
  `g_matches`, per-severity `counts`, and the findings.
- `findings_from_twin_comparison` — one finding per affected-set
  difference, one per currency with a money difference, one when
  the recovery waves disagree (all `warning`, source `twin`). The
  `detail` text carries NO amount — the amount lives in the
  finding's `money` field, which the API redacts for non-owners.
- `findings_from_g_comparison` — keeps G's `{code, resource_id,
  detail}` verbatim and grades by `G_CODE_SEVERITY`: the five
  timing failures (`affected_not_recovered`, `rto_missed_in_drill`,
  `drill_missed_rto_simulation_met`, `mtpd_exceeded_in_drill`,
  `data_loss_exceeds_rpo`) are `critical`;
  `recovered_not_in_simulation` is `warning`; a drill that beats
  its forecast (`drill_met_rto_simulation_missed`) is reported as
  `info` — a forecast that keeps being wrong must get noticed, not
  quietly stay wrong. Fail-closed: an unknown G code is a
  `ValueError` (a difference we cannot grade must not enter the
  queue with a guessed severity), and a G comparison whose `run_id`
  names a different drill run is a `ValueError` (the queue must
  never mix findings from two runs).
- `finding_fingerprint` — SHA-256 over the canonical JSON of
  `(tenant, drill run, simulation fingerprint, code, resource,
  money)`. `money` is part of the identity: two money findings for
  different currencies are different findings, not one row.
- `reconciliation_summary(report)` — one deterministic line.

**Persistence:** `app/db/twin_reconciliation_models.py` —
`TwinReconciliationFinding` (`twin_reconciliation_findings`):
tenant, drill run id, scenario kind, simulation fingerprint,
code, resource id, severity (`info`/`warning`/`critical`),
source (`twin`/`continuity`), detail text (never an amount),
`money_difference` JSONB (per-currency exposure diffs, owner-only
at read), dedupe fingerprint (unique per tenant), acknowledge
bookkeeping (`acknowledged` / `acknowledged_at` /
`acknowledged_by`), `created_at`. Migration
`0109_twin_reconciliation_findings` (down_revision
`0107_twin_health_alerts`; 0109 verified free across all
worktrees at slice time — 0108 is claimed by the mobile-app
branch, `0108_offline_idempotency_records`; renumber at merge if
another branch lands 0109 first).

**Service:** `app/digital_twin/reconciliation_records.py`
(async; follows the `health_records.py` pattern):

- `record_reconciliation` — idempotent per (tenant,
  fingerprint): re-reconciling the same drill run against the same
  simulation stores each finding once; a new simulation of the
  same run produces new rows. Findings are immutable facts; a
  re-run never edits a stored row.
- `list_findings` (newest first; `acknowledged` and `severity`
  filters), `reconciliation_counts` (queue badge: total /
  unacknowledged / per-severity / unacknowledged-critical),
  `acknowledge_finding` (flips only the queue row's acknowledged
  state + who/when; returns `None` when the finding is not the
  tenant's), `prune_findings` (newest 100 / 90 days, same rule as
  health alerts).
- `evaluate_reconciliation` — the scheduled entry point: the twin
  service / worker calls it on a schedule (or after a drill run)
  with the twin simulation and the Step G drill comparison for
  that run; it reconciles, records, prunes, and returns the report
  + recorded rows + one-line summary. No scheduler/CLI entry point
  exists yet — the next slice (or the twin service wiring) adds
  the call; the function is the contract.
- `finding_summary` / `redact_money_for_viewer` — money redaction
  for non-owners, same owner-only rule as the drift endpoint
  (plan §4).

**API** (same `scope_for` gates as the slice-2/3 endpoints; the
acknowledge route commits, per the codebase pattern — see the bug
note below):

- `GET /api/digital-twin/{tenant_id}/reconciliation/findings`
  (`limit` 1–100, `acknowledged` and `severity` filters; money
  redacted for non-owners), `GET .../reconciliation/counts`,
  `POST .../reconciliation/findings/{finding_id}/acknowledge`
  (404 unknown finding/tenant, 401 unauthenticated). Acknowledge
  is gated on tenant membership (`scope_for`), like every other
  twin endpoint, and records `acknowledged_by` — acknowledging is
  queue bookkeeping on an advisory row, not a privileged platform
  action (it approves, dispatches, and applies nothing).

**Tests:** `tests/test_digital_twin_reconciliation_pure.py` —
**21 passed, 0 failed** (`--noconftest`, worktree venv, 1.57 s).
Covers match / mismatch on both sides, all 7 G codes and their
severities, the beat-the-forecast `info` case, money amounts only
in the `money` field (never in `detail`), multi-currency
findings, merge + counts + summary, the 500-finding cap,
fingerprint stability + scoping, run-id mismatch, unknown G code,
malformed comparisons, validation errors, hostile-instruction
text treated as data.
`tests/test_digital_twin_reconciliation.py` — DB + API tests on a
throwaway `*_test` database: **12 passed, 0 failed** (6 service +
6 API; run in two split runs under VM load — service 232 s, API
262 s — plus the conftest pseudo-test). Covers evaluate + store,
idempotency, match stores nothing, new simulation → new rows,
list filters, acknowledge (service + API), prune + validation,
owner sees money / viewer gets redacted money, counts, 404/401.
Slice regressions re-verified after the slice: drift 38 + health
pure 23 + whatif 21 + differential 18 + reconciliation pure 21 =
**121 passed**, versions pure 8 passed (baseline venv).
Alembic roundtrip GREEN on a fresh throwaway DB
(`infraspine_test_roundtrip_k5`, created by the `postgres`
superuser on the twink2 cluster port 55510 — the test role
`twin_k2` has no CREATEDB privilege; DB dropped afterwards):
upgrade head 113 migrations → single head
`0109_twin_reconciliation_findings`; downgrade base 113;
upgrade head 113; `alembic check` clean ("No new upgrade
operations detected"). Note: on the shared test DB the downgrade
is blocked by the pre-existing guard in migration 0089
("Company graph contains data") — that guard fires whenever
company-graph test data exists and is unrelated to this slice;
the fresh-DB roundtrip is the valid proof.

**Real bugs fixed by this slice's tests (not test bugs):**
1. Dedupe fingerprint collision: the first `finding_fingerprint`
   omitted `money`, so two money-mismatch findings for different
   currencies (USD −200, EUR −50) had the same fingerprint and
   merged into one row. Fixed by including `money` in the
   fingerprint identity.
2. Acknowledge route never persisted: the POST returned 200 with
   `acknowledged: true`, but the route never called
   `await db.commit()`, so the next GET still showed both findings
   unacknowledged. Fixed by committing in the route, per the
   codebase pattern (`app/api/routes/agent_studio.py`,
   `app/api/routes/whatif.py`).

**What slice 5 deliberately does NOT do:** no UI (the review queue
surfaces in the control room / twin UI v2, slice 8); no
auto-apply, no drill-record or
twin-version writes, no permission grants, no `execute_once`
dispatch. The loop only ever reads simulations and drill
comparisons and writes review-queue rows. The scheduler/CLI entry
point owed by this slice was added by slice 6 — see §13.

## 13. Slice 6 — what was implemented (this branch)

**Owed from slice 5 (done first): scheduler/CLI wiring for
`evaluate_reconciliation`.** `app/digital_twin/reconciliation_schedule.py`
(`run_reconciliation_cycle`, with an injected async `input_provider`
so the cycle is testable without a live G/AL stack) — fingerprint
gate: skip when findings are already stored for (tenant,
drill_run_id, simulation fingerprint); a matching pair re-evaluates
since it stored nothing. Quiet hours (22:00–07:00 PKT) are reported
but do NOT gate recording, since the cycle sends no alert. CLI
`scripts/twin_reconcile.py`: `--tenant --drill-run-id --simulation
--drill-observation [--g-comparison] [--scenario-kind] [--dry-run]`;
`--dry-run` is pure (no DB); otherwise `DATABASE_URL` is required;
exit 0 ran/skipped, 2 bad input. Tests:
`tests/test_digital_twin_reconciliation_schedule.py` — **8 passed**.

**Pure rules:** `app/digital_twin/proposals.py` (stdlib only).
`ELIGIBILITY` maps (finding source, code) → action kind → approver
class. **Owner class** (money/privilege/data-restore):
`twin.request_supplier_review`, `twin.request_permission_review`,
`twin.request_money_review`, `twin.request_data_review` — required
permission `company_graph.role.owner`, duty `twin.apply`.
**Coverage class** (backup/drill-review): `twin.request_backup`,
`twin.request_drill_review` — required permission
`company_graph.role.approver`, duty `twin.apply.coverage`.
Eligibility: recon codes affected_not_recovered /
rto_missed_in_drill / drill_missed_rto_simulation_met → backup;
observed_not_simulated / simulated_not_observed /
recovery_waves_differ / recovered_not_in_simulation /
drill_met_rto_simulation_missed / mtpd_exceeded_in_drill →
drill_review; money_mismatch → money_review; data_loss_exceeds_rpo
→ data_review; sim kinds uncovered_process → backup,
supplier_spof → supplier_review, backup_permission_gap →
permission_review. Unknown codes fail closed (ValueError). Money
never enters action params. `findings_from_simulation` extracts
uncovered_process (affected entry state failed + `covered_by`
empty; severity critical→critical, high/medium→warning, low→info)
and supplier_spof (supplier_failure scenario seed with no
`activate_backup`/`switch_supplier` recovery option;
manual_workaround is NOT coverage). `backup_permission_gap` is
caller-built only (the simulation result carries no permission
data) via `validate_simulation_finding` (needs
extra.holder_person_id + extra.required_permission). The ActionSpec
canonicalization is a byte-identical port of
`app.platform.approvals._canonical`, pinned by a DB cross-check
test. Tests: `tests/test_digital_twin_proposals_pure.py` —
**30 passed**.

**Model:** `app/db/twin_proposal_models.py` + migration
**`0110_twin_proposals`** (down_revision
`0109_twin_reconciliation_findings`; 0110 verified free across all
worktrees; single Alembic head `0110_twin_proposals`). Statuses:
draft → pending_approval → approved/rejected/expired/cancelled →
dispatched → verified/failed. Unique (tenant,
proposal_fingerprint). Registered in `app/db/models.py`.

**Service:** `app/digital_twin/proposals_records.py`.
`create_proposal` (idempotent per finding+action; re-proposal from
rejected/expired/failed requests a fresh approval; cancelled stays
cancelled with ConflictError; requests platform approval with 24h
TTL via `request_approval`), `decide_proposal` (mirrors approval
outcome; expired approval marks proposal expired),
`execute_proposal` (three-way separation of duties enforced
explicitly — executor ≠ proposer and ≠ approver; dispatch =
publish durable `twin.proposal_action` outbox event carrying the
exact canonical action; verify = read back the outbox row (integer
id — `PlatformOutbox.id` is BigInteger autoincrement) and check
payload action_hash + proposal_id; idempotency key
`twin-proposal:{approval_id}` so each approval attempt dispatches
at most once and re-execute replays), `cancel_proposal` (proposer
only), `prune_proposals` (terminal statuses only — pending /
approved / dispatched never pruned; keep 100 / 90 days),
`proposal_summary` (includes `approver_class`, derived from the
stored `required_permission`), `redact_money_for_viewer`.
Tests: `tests/test_digital_twin_proposals.py` service + API —
**23 passed** (15 service + 8 API).

**Platform changes:** `app/platform/access.py`
`APPROVAL_REQUIRED` += `{"twin.apply": "company_graph.role.owner",
"twin.apply.coverage": "company_graph.role.approver"}`;
`app/platform/sync.py` company_graph role mirror: owner extra +=
`twin.apply`, `twin.apply.coverage`, AND
`company_graph.role.approver` (owner is a superset of approver,
matching the existing connector.read pattern — so an owner may
approve either class); approver extra += `twin.apply.coverage`.
`app/platform/handlers.py`: registered `twin.proposal_action`
handler (logs receipt only; the physical follow-up is a
human/operator step — the twin never writes the graph, grants
access, restores data, switches a supplier, or pays; this is the
dispatch boundary).

**API:** `app/api/routes/digital_twin.py`: POST
`/{tenant_id}/proposals` (exactly one of finding_id /
simulation_finding), GET list/counts/get, POST decide (body
decision + presented_hash + note), POST execute, POST cancel. All
`scope_for` first (404 non-members, 401 unauthenticated); money
redacted for non-owners; ConflictError→409,
PermissionDeniedError→403, InvalidInputError→422,
NotFoundError→404 (per `app/core/errors.py`).

`app/digital_twin/__init__.py` exports the proposal names (pure
module only; `proposals_records` NOT imported there — needs
sqlalchemy, same as the `health_records` / `versions` pattern).

**Pure regressions (slices 1–5): 159 passed** — drift 38 +
versions pure 8 + health pure 23 + whatif 21 + differential 18 +
reconciliation pure 21 + proposals pure 30.

**DB regressions (slices 2–3, 5): 57 passed, 0 failed** —
reconciliation 13 + versions 24 + health 20 (each count includes
the conftest collection item: 12 + 23 + 19 real tests).

**Alembic roundtrip: GREEN** — fresh throwaway DB
`infraspine_test_roundtrip_k6` on the twink2 cluster (port 55510):
upgrade head 114 migrations → single head
`0110_twin_proposals`; downgrade base 114; upgrade head 114
again; `alembic check` clean ("No new upgrade operations
detected"); DB dropped afterwards. Log:
`/home/hatch/k165-alembic.log`. Note: on the shared test DB the
downgrade is blocked by the pre-existing guard in migration 0089
("Company graph contains data") — that guard fires whenever
company-graph test data exists and is unrelated to this slice;
the fresh-DB roundtrip is the valid proof.

**Real bug fixed by this slice's tests (not a test bug):**
`proposal_summary` omitted `approver_class` (only the draft
carried it). Fixed by deriving it from the stored
`required_permission` in the summary. The two failing service
tests then passed.

**What slice 6 deliberately does NOT do:** no auto-apply — a
proposal never changes the graph, grants a permission, restores
data, switches a supplier, or moves money; dispatch publishes a
durable outbox event and the follow-up is a human/operator step.
No UI (proposal inbox is slice 8). No new feed kinds. No change
to the twin-native engine retirement (still a merge-time action,
gated on the differential test).

## 14. Slice 7 — what was implemented (this branch)

**Multi-source trust scoring.** `app/digital_twin/trust.py` (pure,
stdlib only). `TRUST_SOURCES` weights (points of 100): company_graph
30, finance 20, continuity 15, membership 10, workflow 10,
agent_studio 10, connectors 5. A source's trust is freshness-based
(fresh 100, stale 0); a source with no freshness entry is "unknown"
— it drops coverage instead of silently counting as fresh or stale.
The aggregate is the weight-normalised mean over known sources;
with no known source the score is `None` and the band is
`"unknown"` — never a fabricated number. The reported band is the
worse of the freshness band (≥75 trustworthy / ≥40 review / else
stale, aligned with health) and the drift trust verdict from the
latest drift report. Unknown source names are listed with weight 0,
never dropped. Trust is computed on read from the latest version
summary (which carries `source_freshness` + the drift verdict) —
NO migration. Per-source drift attribution is NOT computable from
the current snapshot shape (no per-entity `source` field) — owed
merge-time item: the Step S snapshot builder must stamp `source`
on entities.

**Conflict reporting.** `app/digital_twin/conflicts.py` (pure).
`detect_conflicts(source_views, max_conflicts=500)` — same entity id
in 2+ sources with differing kind / name / one attribute key →
`{entity_id, field, kinds, values}`, deterministic sort, values
bounded to 200 chars (drift rule). An entity present in one source
only is NOT a conflict. Money amounts are compared BY VALUE
(`"100.00"` == `100` == `100.0`, canonicalised to 2 decimals) and
validated like drift (≤2 decimals, finite). `redact_conflicts_for_viewer`
(non-owner: money-kind entity values → `"<redacted>"` +
`money_redacted: true`; the disagreement itself stays visible).

**Remaining feeds.** `app/digital_twin/feeds.py` (pure translators)
+ `app/digital_twin/feed_records.py` (async, governed-change
application) + handlers in `app/platform/handlers.py` (one handler
registered for every kind in `FEED_EVENTS`).

Two tiers, stated honestly. LIVE on this branch:
`identity.session_revoked` is published by `app.platform.access`
when a user's platform access is revoked (payload `{user_id,
reason, ...counts}`, scope_id = org id) — the twin proposes the
graph member be deactivated. READY, no publisher yet on this
branch: `access.member_added` / `access.member_removed`,
`automation.*` (workflow + run lifecycle), `agent_studio.*` /
`agent.profile_*` (Agent Studio agents + runs) and
`cloud_connector.*` exist today only as audit event names — no code
on this branch publishes them to the outbox (verified by scanning
every `publish(` call site). The translators and handlers are the
consumption side, tested end to end; they activate the moment
publishers start emitting these kinds (Step S feed publishers /
future slices). Until then a handler with no matching event simply
never fires.

Subject keys are the contract: `user_id` for members, `agent_id`
for agents, `workflow_id` for workflows, `run_id` for runs,
`connector_id` for connectors, falling back to `subject_id` then
`id`. A known kind with no subject id raises `ValueError` (retried,
then dead-lettered — never silently dropped); an unknown kind
returns `None`. No secrets ever ride a feed change (credential-ish
payload keys dropped; values bounded to 200 chars).

`apply_feed` stores ONE pending `GraphChange` through the
governed-change path — the twin NEVER writes the graph directly.
Records: external key `twin-feed:{feed}:{subject_ref}`, kind =
subject kind, visibility `restricted`, one evidence entry naming
the source event, `inferred=True`, `retired=True` for removals;
`base_version` resolves from the live record (0 = new),
`subject_id` is the live record id or a deterministic uuid5.
Members: new member proposed as `viewer` (never a privilege
grant — the event payload's role is deliberately ignored),
existing member keeps its role/departments, `active` from the
action. A removal with no live subject is skipped (`not_a_member`
/ `not_present`). Dedupe (discovery.py pattern): an identical
pending change — same subject, same payload IGNORING timestamps
(each proposal stamps `now`), same base_version — is skipped.
Expiry: members +1h, records +24h (same as `propose_*`).
Requester: the event actor when given and an active user, else the
tenant's first active owner member (by user id — deterministic, not
a preference); a tenant with no requester is a `ValueError`
(outbox retries, then dead-letters for an operator). Tenant
resolution: payload `tenant_id` first, else the outbox `scope_id`
when it IS a graph tenant id — a graph tenant id is NOT an org id,
so an event carrying neither is skipped with a warning, never
guessed at. Every proposal is audited as `twin.feed_proposed`.

**API.** `app/api/routes/digital_twin.py`: `GET /{tenant_id}/trust`
(latest version; 404 when no versions), `GET
/{tenant_id}/trust/history` (per-version trust), `POST
/{tenant_id}/conflicts/detect` (body source views; scope_for first;
money redacted for non-owners). Trust and conflicts carry scores,
counts and names only — no money amounts — so any tenant member may
see them. `__init__.py` exports the pure trust / conflicts / feeds
names only (`feed_records` needs sqlalchemy — NOT imported, same as
`proposals_records`).

**Tests:** `tests/test_digital_twin_trust_pure.py` — **10 passed**;
`tests/test_digital_twin_conflicts_pure.py` — **12 passed**;
`tests/test_digital_twin_feeds_pure.py` — **11 passed**;
`tests/test_digital_twin_feeds.py` (DB) — **18 passed in one
process** (274.49s; 1 pre-existing `run_db` unawaited-coroutine
warning, not a failure).
Pure regressions slices 1–6: **167 passed**
(drift 38 + health_pure + versions_pure + whatif + differential +
reconciliation_pure + reconciliation_schedule + proposals_pure).

**Real bugs found by this slice's tests (not test bugs):**
(1) `feed_records.apply_feed` built the `GraphChange` without
`tenant_id` — every insert failed the NOT NULL constraint; fixed by
passing `tenant_id=tenant_id`. (2) `conflicts._conflict_key` was
called with `field="amount"` while it checked for
`"attributes.amount"`, so money never compared by value; fixed by
passing the real field name. (3) Same helper compared
`str(Decimal("100.00"))` (`"100.00"`) against `str(Decimal(100))`
(`"100"`) — equal amounts looked different; fixed by quantizing to
2 decimals. (4) The dedupe compared full payloads, but each call
stamps a fresh `now` into `valid_from`/`observed_at`/evidence, so
dedupe could never fire; fixed with `_payload_identity` (timestamps
excluded). Test bugs fixed along the way: the conflicts redaction
test passed two views with the same source name (one view per
source); the trust 404/401 test took both `admin_client` and
`client` — they are the SAME TestClient object after login, so the
"unauthenticated" call was authenticated (split into two tests);
the handler test called `user_id()` (its own `asyncio.run`) inside
a running event loop (hoisted out).

**What slice 7 deliberately does NOT do:** no graph writes, no
privilege grants, no auto-apply; no per-source drift attribution
(owed, needs S snapshot `source` stamping); no new publishers for
the ready-tier feed kinds (consumption side only — publishers are
Step S / future work); no migration.

## 15. Slice 8 — what was implemented (this branch)

**Twin UI v2.** `frontend/src/digital-twin/api.ts` (`twinApi`
client: versions, health, healthHistory, healthAlerts, trust,
trustHistory, findings, findingCounts, acknowledgeFinding,
proposals, proposalCounts, decideProposal, executeProposal,
cancelProposal — via `request` from `../api`), `types.ts`,
`twinDashboard.mjs` (pure helpers: `healthBand`, `trustBand`,
`gaugeSegments`, `timelineRows`, `inboxRows`, `proposalStatusTone`,
`findingTone`, `queueSummary`, `maskMoney`, `shortFingerprint`) +
`twinDashboard.d.mts`, `frontend/test/twinDashboard.test.mjs`.

`TwinWorkspace` in `frontend/src/components/ModuleWorkspaces.tsx`:
`"digital-twin"` added to `WorkspaceView` + a case in
`ModuleWorkspacesPage` + a launch card in `FrontendUxWorkspace`'s
"Open an application workspace" list ("Digital twin — Drift,
health, versions, proposals"). The 12-module sidebar is NOT
renumbered — the twin is not one of the 12 numbered modules on
this base (owner decision still open: where the twin sits in the
numbered list). The workspace shows: tenant picker (graph tenants),
health gauge (score + band + three-segment bar + drift/money/
freshness deductions + stale sources), multi-source trust card
(per-source status/weight/score), drift timeline (newest first,
first version labelled "no drift recorded yet"), version history
table (summaries only, never the full snapshot), proposal inbox
(actionable first: pending approval, then approved; Approve /
Reject act on the exact `action_hash` the approver reviewed;
Execute only for approved proposals — the inbox is read-only until
approval), review queue (findings + Acknowledge), health alerts.
Health/trust 404s (tenant with no versions yet) render as an empty
state, not an error. Money amounts are shown only as the API
returns them (owner-only; non-owners see "<redacted>" via the
inbox note). `node_modules` was missing in this worktree — copied
from the frontend-redesign worktree (package-lock.json
byte-identical); `npm ci` was NOT run (OOM-killed 3× for Agent 130).

**Tests:** frontend `node --test test/*.test.mjs` — **30 passed, 0
failed** (22 prior + 8 new twinDashboard); `tsc --noEmit` clean;
`vite build` clean (12.79s, `NODE_OPTIONS=--max-old-space-size=1024`);
the twin strings ("Digital twin", "Proposal inbox", "Health gauge",
"Review queue", "Drift timeline") are verified present in the built
`ModuleWorkspaces-*.js` chunk.

**What slice 8 deliberately does NOT do:** no conflicts-detect UI
(the API exists; the UI has no per-source entity views to feed it —
owed/future); no proposal creation from the UI (create stays an API
operation from a finding); no sidebar renumbering; no deploy.
