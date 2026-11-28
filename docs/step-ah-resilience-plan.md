# Step AH — Company Resilience AI: implementation plan

Status: **preparation + first safe slice** (branch `agent/resilience-ai`, base
`step-e-gaps-wip` `ca956c2`). Step AH is the new owner-requested step in the
master plan (`MASTER-PLAN.md`): an AI system that understands dependencies
across employees, processes, software, data and suppliers, keeps a
continuously updated map, predicts risks and disruptions before they hit,
explains the business impact, and suggests practical recovery plans.

Step AH builds on — and does not duplicate — three earlier pieces:

- **Step G (continuity)** — `docs/step-g-continuity-plan.md` (Agent 16 prep,
  Agent 34 building slices 2–4): the continuity graph + BIA, handover,
  drills, backup/restore, unified-engine activation, and the **graph merge**
  (one entity id + one scope across `company_graph_records` and `bc_nodes`).
  AH consumes the merged graph; it does not build a third one.
- **Step S / K (digital twin)** — `docs/step-s-digital-twin-plan.md`
  (Agent 24 prep, branch `agent/digital-twin`): the twin snapshot shape and
  the person-absence simulation (`app/digital_twin/impact.py`,
  `simulate_absence`). AH **reuses that snapshot shape** and extends the
  math to all disruption kinds (server outage, cyber incident, supplier
  failure) plus risk scoring and ranked recovery options.
- **Step W (self-healing)** — `docs/step-w-self-healing-plan.md` (Agent 29):
  fault detection + safe-remedy allow-list. AH's risk watch feeds W's
  detector; W's remedies are a subset of AH's recovery options. AH never
  auto-executes a remedy — every sensitive action needs an approval.

Sources: the owner's Step AH request (2026-11-28), `HANDOVER-PROMPT.md`
§4–§5, `assessment/master-plan-v2.md` §4 (Digital Twin), §5 (Predictive
Continuity), §8 (What-if Engine), §1 (Steps K/L/M tables), and the code
survey below (all paths relative to the repo root).

## 1. What Step AH must deliver (acceptance)

1. **One continuously updated dependency map.** Employees, processes,
   software/systems, data, suppliers — one entity id, one scope. Adding or
   revoking a person, system or supplier updates the map without a manual
   redraw (outbox event → graph update, the Step G/S live-sync feeds).
   The same entity is never duplicated across module tables and the map.
2. **Risk prediction before the disruption.** For every node, a risk score
   with named factors (single point of failure, no backup, approval
   bottleneck, supplier concentration, stale data). The highest-risk nodes
   are listed first; a score change is explainable (which link or work
   item changed it).
3. **Disruption answers for the four owner-named kinds.** Key employee
   absence, server outage, cyber incident, supplier failure — each returns
   the affected processes/owners, money at risk, a plain-language business
   impact explanation, and ranked practical recovery options with owners
   and ETAs. A process the caller is not allowed to see is omitted, and
   its count is not leaked.
4. **Recovery plans that are practical, not prose.** Each option names the
   action, who does it, what it covers, and whether it needs approval.
   Recovery progress is tracked (steps done / blocked / next) and the team
   gets updates as it moves.
5. **Role-based permissions everywhere.** Every read goes through
   `app/platform` authorization (`resolve_principal`, `authorize`,
   `require` → 404 non-members, 403 members without permission). A
   simulation never grants access; a recorded `backup_for` link is never
   an access grant.
6. **Approval before any sensitive or irreversible action.** Recovery
   steps that change access, switch a supplier, restore data, or move
   money run as unified-engine workflow runs with exact-action approvals
   (hash-bound, expiring, single use, separation of duties) and
   `execute_once` with read-back verification. Advisory output never
   executes anything by itself.
7. **Secure audit record of everything.** Every alert, recommendation,
   approval and action is recorded through the platform audit events
   (`app/platform/events.py` → `app/services/audit.py`), with secret
   redaction; the audit trail is exportable for review.
8. **Simulations are side-effect free.** Running a disruption analysis or
   a what-if writes no `platform_action_executions` in the dispatched
   state and changes no production records. The same analysis is
   deterministic for the same map version.

## 2. What already exists (do not rewrite)

| Area | Existing code | Notes |
|---|---|---|
| Company graph | `app/company_graph/*` (`company_graph_records` entities + relationships, governed proposals + approval, `service.projection` / `neighborhood`, `discovery.synchronize` with idempotency keys, `schemas.Simulation` with kinds resignation / absence / system_outage / supplier_failure / restructuring) | Second graph, second tenant table. Mostly manual/API-ingested, not fed live by HR, workflow, agent activity or connectors. |
| Graph impact (outage) | `app/company_graph/intelligence.py` (`impact()` forward/reverse BFS depth 6, `backup_for` suggestions, path confidence, `quality()` gaps/stale/conflicts) | Outage semantics ("failed node"), not person-absence or cyber semantics. No money amounts, no time horizon, no recovery ranking. |
| Continuity graph + BIA | `app/continuity/*` (`bc_nodes`, `bc_links`, `bc_scenarios`, `intelligence.bia_score` / `analyze` / `handover_draft`, 12 scenario kinds, recovery waves, skill-matched backup candidates) | Static, separately maintained graph. `backup_for` is a recorded link only — no effective-permission check. Activation is its own code path, not the unified engine (Step G fixes this). |
| Digital twin slice 1 | `app/digital_twin/impact.py` on branch `agent/digital-twin` (`simulate_absence`, `money_at_risk`) | Person/team absence only, over the twin snapshot shape. **Not in this base** — AH reuses its snapshot shape and extends it; it does not copy the file. |
| Continuity health + backup manifest | `app/continuity/health.py` (Agent 16 slice 1: `aggregate_health`, `build_backup_manifest`, `verify_backup_manifest`, `backup_freshness`) | Pure functions; the health roll-up feeds AH's server-outage risk. |
| Self-healing detector | `app/self_healing/detector.py` on branch `agent/self-healing` (`classify_incident`, `decide_action`, `escalation_report`) | Fault classification + remedy-vs-escalate rules. AH's risk watch feeds it; AH does not re-implement detection. |
| Recovery | `app/recovery/*` (incidents, playbooks, approved commands, worker with its own `_execute_once`, `policy.redact` / `action_digest`) | Not hooked to live workflow/agent/integration failures. Playbooks do not auto-start. |
| Platform core | `app/platform/*` (`resolve_principal`, `authorize`, `require`, approvals with SHA-256 hash, `execute_once` with read-back, `events.audit`, outbox) | The safety spine AH wires into; continuity/recovery do not consistently use it yet. |
| Risk scoring | `app/continuity/intelligence.py` (`bia_score`, `impact_score`/`risk_score` in `analyze`) | BIA on the static continuity graph only; no node-level single-point-of-failure score, no supplier concentration, no watch. |
| Frontend | `frontend/src/company-graph`, `frontend/src/continuity` | Graph/plan UI exists; no resilience map, risk list, or recovery-progress view. |
| Tests (thin) | `tests/test_company_graph.py` (12), `tests/test_continuity.py` (4), `tests/test_recovery.py` (15) | No disruption, risk-ranking, or recovery-plan tests exist. |

## 3. What is missing (the Step AH work list)

1. **Disruption math for all four kinds (slice 1 — implemented now, §5).**
   One pure module over the twin snapshot shape: person/team absence,
   server outage, cyber incident, supplier failure → affected set, impact
   severity, money at risk, ranked recovery options. Advisory only.
2. **Node risk scoring + ranking (slice 1 — implemented now, §5).**
   Single-point-of-failure, backup coverage, approval bottleneck,
   supplier concentration, staleness → a 0–100 score with named factors.
3. **Risk watch (slice 2).** Always-on ranking refresh on map-revision
   change; alert rules with severity thresholds, dedupe and cooldown
   (same pattern as Step W's detector); alerts are notifications, never
   actions.
4. **Snapshot builder (slice 3).** Read-only adapter building the AH
   snapshot from the merged graph (Step G) + authorized continuity nodes
   + finance batch/payment rows + live workflow runs, all through
   `app/platform` scopes. No new tables; no writes.
5. **Recovery plans as workflow runs (slice 4).** A chosen recovery
   option becomes a real unified-engine workflow run
   (`app/workflows/unified.py`) with exact-action approval and
   `execute_once`; progress tracked step by step; team updates published
   through the outbox as the plan moves.
6. **Drill comparison (slice 5).** The five Step G disruption drills
   compared against AH simulations; differences listed; drill results
   feed the risk scores back (a drill that fails raises the node's risk).
7. **Audit trail + export (slice 6).** Every AH alert, recommendation,
   approval and action recorded via platform audit events with redaction;
   exportable audit report for the owner.
8. **Resilience UI + docs (slice 7).** One view: dependency map, ranked
   risks, disruption panel, recovery progress; freshness ("map updated
   …"); restricted users see only their authorized projection; counts
   never leak hidden nodes. `CHANGES-step-AH.md` (plain language, file
   list, test counts).

## 4. Build order (each slice finished + tested before the next)

1. ✅ **Slice 1 — disruption-impact + risk-scoring pure module** (this
   branch): `app/resilience/impact.py` +
   `tests/test_resilience_impact.py`. No I/O, no DB, no live data, no
   secrets. Extends the Step S snapshot math to all four disruption
   kinds and adds risk scoring + ranked recovery options.
2. **Slice 2 — risk watch.** Ranking refresh on map revision; alert
   rules (thresholds, dedupe, cooldown); alert delivery via Step AA
   scheduled reports / Step F WhatsApp (mocked in tests).
3. **Slice 3 — snapshot builder (read-only adapter).** Merged graph +
   finance + workflow runs through `app/platform` scopes.
4. **Slice 4 — recovery plans as unified-engine runs.** Approval-bound
   execution, progress tracking, team updates.
5. **Slice 5 — drill comparison.** Step G drills vs AH simulations;
   drill outcomes feed risk scores.
6. **Slice 6 — audit trail + export.** Platform audit events for every
   AH alert/recommendation/approval/action; export.
7. **Slice 7 — resilience UI + docs.** Map/risk/recovery views,
   `CHANGES-step-AH.md`, full suite + Alembic round-trip + frontend
   clean, merge to main.

Slices 2–7 are Step AH build work; this branch delivers the plan (§1–§7)
and slice 1 only.

## 5. Slice 1 — what was implemented (this branch)

**Module:** `app/resilience/impact.py` (pure functions; standard library
only, no imports from `app.db`, no I/O, no network, no secrets). It uses
the **same snapshot shape as Step S** (`app/digital_twin/impact.py`):
`entities` with `id`/`kind`/`name`/`attributes`, `relationships` with
`source_id`/`target_id`/`kind`/`confidence`, optional `as_of`.

Entity kinds: `person`, `team`, `process`, `system` (server / software),
`data`, `supplier`, `invoice_batch`, `payment`. Relationship kinds:
`owns` / `responsible_for` / `operates` / `approves` (authority),
`depends_on`, `supplies`, `backup_for`, `member_of`, `stores`
(data → system), `hosts` (system → data/process).

- `score_disruption(snapshot, event)` — the main entry. `event` is
  `{"kind": "person_absence" | "team_absence" | "server_outage" |
  "cyber_incident" | "supplier_failure", "target_ids": [...],
  "duration_days": N}`. Returns a deterministic analysis:
  - Targets must exist and match the event kind (a `supplier_failure`
    target must be a supplier, etc.) — otherwise `ValueError`.
  - Directly affected: what the target owns / operates / approves /
    supplies / stores / hosts, plus the target itself.
  - Propagation: `depends_on` and `supplies` carry impact forward;
    authority edges carry impact from an at-risk holder to what it
    holds; `stores`/`hosts` carry a cyber incident from a system to its
    data and from data to the processes that depend on it. BFS,
    cycle-safe, depth-limited (default 6); `truncated: true` when the
    depth limit cuts a path.
  - Severity: `critical` / `high` / `medium` / `low` from money at risk,
    critical-process count, and whether an approval bottleneck is hit
    (a pending `invoice_batch`/`payment` whose only approver is
    affected).
  - Money at risk: `invoice_batch` / `payment` entities in the affected
    set contribute `attributes.amount` grouped by currency (amounts must
    be numeric and ≥ 0; anything else → `ValueError`). Exposure only.
  - Business impact: plain-language sentences naming the affected
    processes, owners, money, and the bottleneck — no invented facts,
    only snapshot content.
  - Ranked recovery options (advisory only, §5 details): backup person,
    team reassignment, alternative supplier, manual workaround,
    escalate to owner. Each option names its action, owner, what it
    covers, ETA, and whether it needs approval.
  - Uncovered entities listed separately with the reason
    (`no_backup_recorded` / `backup_absent` /
    `backup_missing_skills` / `no_alternative_supplier` /
    `no_workaround`).
- `assess_risk(snapshot, node_id)` — proactive 0–100 risk score for one
  node with named factors: dependents count (blast radius), backup
  coverage, approval bottleneck (sole approver of pending money),
  supplier concentration (only supplier of a process), staleness
  (`attributes.last_confirmed_days_ago` beyond 30 days), criticality
  (`attributes.critical: true`).
- `rank_risks(snapshot)` — every node scored, highest first; ties broken
  by node id for determinism.
- `recovery_progress(plan)` — given a recovery plan dict (`steps` with
  `id`/`status` in `pending` / `in_progress` / `done` / `blocked` /
  `skipped`), returns percent complete, counts, blocked steps, and the
  next actionable step. Pure bookkeeping — it never executes a step.

**Tests:** `tests/test_resilience_impact.py` — **43 passed, 0 failed,
0 skipped** (4.30s, minimal pytest-only venv, `--noconftest`, log capture
on). Pure pytest, no database rows touched; the file imports only the
standard library and `app.resilience.impact`, so it also runs under the
full suite. No `*_test` database is needed — the slice is pure functions.
The minimal venv was removed after the run, so nothing extra is committed.

**What slice 1 deliberately does NOT do:** no live sync, no graph
writes, no new tables, no migration, no API route, no UI, no permission
grants, no `execute_once` dispatch, no alerts sent. It is the math the
later slices wire to live data.

## 6. Data-integrity and safety guarantees (whole step)

- Every read goes through `app/platform` authorization
  (`resolve_principal`, `authorize`, `require` → 404 non-members, 403
  members without permission). A restricted caller's analysis omits
  unauthorized entities and never leaks their counts.
- The resilience map is a **projection**, not a second source of truth:
  module tables stay authoritative; the map never writes back into
  finance, workflow, or membership records.
- Analyses are side-effect free by construction: pure functions over a
  snapshot; slices 4–5 must additionally prove zero
  `platform_action_executions` dispatched per analysis run.
- A `backup_for` link is a recorded assignment, never an access grant.
  Effective-permission gaps are listed, not silently bridged. Recovery
  options that need access are marked `requires_approval: true` and
  execute only as approved unified-engine runs.
- Money amounts are exposure figures. No AH output is ever a payment
  instruction; payments still require the exact-action approval flow in
  Step E/R.
- Recovery plans never auto-start. A plan starts only when an authorized
  person approves it; drills are side-effect free.
- No secrets in the map, its snapshots, its logs, or its tests. Test
  data is synthetic (fake people/teams/amounts).
- Tests run only on throwaway `*_test` databases; pytest log capture
  stays on; no live folder, live DB, `.env`, uploads, or data touched.

## 7. What Step AH still needs from the owner

- Nothing new for slice 1 (pure math, synthetic test data).
- For live sync (slice 3): which HR/membership source is authoritative
  for people/teams (live memberships vs an external directory), and
  whether supplier/system records may be seeded from connectors.
- For recovery execution (slice 4): who may approve a supplier switch
  and a data restore (proposed: owner only, separation of duties from
  the person who proposed it).
- For alerts (slice 2): the WhatsApp number / email for urgent
  resilience alerts, and quiet-hours rules (proposed: critical alerts
  break quiet hours, all others respect them).
- No credentials are needed for any slice; connectors stay mock until
  the owner provides real ones (same rule as Step E Gap 4).
