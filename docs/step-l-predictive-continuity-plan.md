# Step L — Predictive Continuity: implementation plan

Status: **preparation + first slice** (branch `agent/predictive-continuity`,
base `step-e-gaps-wip` `ca956c2`). Step L is the v2 Predictive Continuity
feature from master-plan v2 §5. It is the **prediction layer on top of
the twin** — it does not build a second twin, a second risk engine, or a
second what-if engine.

Sources: `assessment/master-plan-v2.md` §5 (Predictive Continuity — the
authority for this step's scope), `MASTER-PLAN.md` (J–N list:
"Predictive Continuity"), `docs/step-s-digital-twin-plan.md` (Agent 24 —
the twin foundation), `docs/step-k-twin-v2-plan.md` (Agent 118 — the twin
v2 work this step builds on and must not duplicate),
`assessment/CHANGES-step-K.md` (Agent 92 — the Step S changelog),
`docs/step-g-continuity-plan.md` (Step G — graph merge + drills),
`docs/step-ah-resilience-plan.md` (Step AH — risk scoring + watch),
`docs/step-al-whatif-plan.md` (Step AL — general what-if engine), and
the code survey in §2 (all paths relative to the repo root, read — not
guessed).

**Note on the v2 document's letters.** Master-plan v2 §5 says
Predictive Continuity is "Completed in K", and §6 (AI Decision Memory)
says it is "Delivered in step L". Those letters do not match the team's
J–N mapping, where K is the Digital Twin and L is Predictive
Continuity (Master Agent brief, Agent 119). This plan defines Step L
by the **content of v2 §5** (Predictive Continuity), not by the stray
§6 letter. Decision Memory is a separate step in the team's plan
(Step M).

## 1. What Step L must deliver (acceptance)

Taken from v2 §5, restated. Plain language: do not wait for a
disruption. The system should already be able to say: "If this
employee, system or supplier is unavailable for 7 days, these 14
processes are affected."

1. **"Unavailable for N days" for people, systems and suppliers.**
   For a seeded employee, system and supplier, "unavailable 7 days"
   returns a stable, numbered process list (e.g. 14) with owners and
   dependencies.
2. **The horizon changes the answer.** A 1-day, 7-day and 30-day
   forecast for the same absence are different answers: deadlines and
   SLAs inside the window are what make a process "due", not just
   graph distance.
3. **Always-on, not on-demand.** Predictions refresh as the twin
   changes (fingerprint-gated), and a standing forecast exists for
   every critical entity — nobody has to remember to ask.
4. **Removing a `backup_for` link or a permission changes the
   prediction on the next refresh.** A forecast records the twin
   fingerprint it was computed from; a stale fingerprint means a
   stale forecast.
5. **A process the caller is not allowed to see is omitted, and the
   count is not leaked.** Visibility is enforced in the forecast
   itself, not only at the API edge.
6. **Predictions are explainable.** Which links *and which live work
   items* produced each impact — workflow runs, approvals and invoice
   batches in flight, not just graph edges.
7. **Advisory only.** A forecast never triggers an action, a payment,
   a permission change, or a supplier switch. A forecast may become a
   proposal; a human approves it (slice 6).

## 2. Overlap check — what already exists (do not rewrite)

Read from the code, not from memory. Every row below was verified
against the named branch/worktree before this plan was written.

| Area | Existing code | What it does | L reuse / gap |
|---|---|---|---|
| Twin snapshot builder | Step S `app/digital_twin/snapshot.py` (`build_snapshot`, `build_twin_snapshot`, `source_freshness`) | Merges an authorized company-graph projection + continuity nodes/links + finance batches/payments + workflow runs into one plain-dict snapshot; company graph wins on conflicts; fingerprint + `last confirmed`; payee account never copied | Reuse as-is. L forecasts **over** the snapshot; it never builds one |
| Live sync feeds | Step S `app/digital_twin/feeds.py` | 5 real outbox events → twin through the governed-change path only | Reuse as-is. L's standing forecast refreshes on the same twin changes |
| Absence simulation | Step S `app/digital_twin/impact.py` (`simulate_absence`, `money_at_risk`) | Person/team absence → at-risk entities, backup coverage, money at risk per currency, recovery waves, cycle-safe BFS depth 6 | Reuse the propagation semantics; L extends them. **Gaps L closes:** only person/team roots (no system/supplier); `horizon_days` is recorded but does not change the math; no process concept; no live-work linkage |
| Horizon predictions + watch | Step S `app/digital_twin/horizon.py` (`predict_absence`, `refresh_predictions`, `evaluate_horizon_watch`, `snapshot_fingerprint`, `fingerprint_changed`) | "Unavailable N days" with 0–100 advisory risk score + explainable factors; fingerprint-gated refresh; rules-as-data watch, SHA-256 dedupe + cooldown, quiet hours 22:00–07:00 PKT | Reuse the **watch pattern** and the fingerprint algorithm. **Gap L closes:** the watch only evaluates *configured* absent sets — nothing scans the whole twin for emerging risks, and nothing says which *processes* break |
| Twin drift | Step K v2 `app/digital_twin/drift.py` (`diff_snapshots`, `drift_score`, trust verdict) | Deterministic diff between two twin snapshots: what changed, how much, per-currency money deltas | Reuse for staleness: a forecast whose fingerprint drifted past the threshold is marked stale (slice 3). K owns drift; L consumes it |
| Twin versions + health | Step K v2 slices 2–3 (planned) | Persisted snapshot versions + drift history; one aggregate 0–100 twin health score | L's forecast records reference twin versions; L does not build its own version store |
| What-if engines | Step S `app/digital_twin/whatif.py` + Step AL `app/whatif/simulator.py` (+ `adapter.py`, `drills.py`, `rehearsal.py`) | Scenario simulation (7 kinds), failed/degraded/exposed states, all-providers rule, ranked recovery, rehearsal, drill comparison, `POST /api/whatif/{tenant_id}/simulate` | L never builds a second engine. A forecast is a *prediction over live data*, not a *scenario over a hypothetical*; when a forecast needs a scenario it calls the AL engine (slice 6) |
| Continuity impact / BIA | `app/continuity/intelligence.py` (`analyze`, `handover_draft`) + `app/company_graph/intelligence.py` (`impact`, `FORWARD_IMPACT`, `REVERSE_IMPACT`, depth 6) | BIA scoring on the continuity graph; forward/reverse impact on the company graph; 12 scenario kinds | Reuse the edge semantics (same forward/reverse rules). L does not re-score BIA |
| Risk scoring + watch | Step AH `app/resilience/impact.py` (`score_disruption`, `assess_risk`, `rank_risks`, `recovery_progress`) + `app/resilience/watch.py` + `app/resilience/snapshot.py` | Advisory 0–100 risk scores for all disruption kinds; watch rules as data; read-only snapshots | Reuse the **watch pattern** for the standing forecast (slice 2). L never duplicates AH scoring — a forecast carries no risk score of its own in slice 1; the score comes from S horizon / AH when the forecast is served |
| Continuity drills / backup / restore | Step G `app/continuity/*` (backup, restore, unified activations; slices 2–4 DONE, slices 5–8 in progress: effective-permission handover check, graph merge, 5 drills + comparison, UI) | Real backups, restore verification, activations on the unified engine | Drills are the **ground truth** for slice 7 reconciliation. Graph merge (G slice 6) is the dependency L must not duplicate — L consumes the merged graph |
| Control room | Step Q `app/control_room/service.py` + Step AF `app/control_room/alerts.py`, `charts.py` | Live status API, alert rules, charts, dispatch worker | Forecast alerts surface **there**; L builds no second dashboard |
| HR leave / attendance | Step AG `app/hr/leave.py`, `app/hr/reminders.py` (slices 2–3 in progress: models + migration + leave-request API) | Leave balances, salary reminders; leave requests on the unified engine | Slice 4 consumes planned-leave data. L does not model leave itself |

## 3. What is genuinely new for Step L (not in S / K / AL / G / AH)

1. **System + supplier unavailability (slice 1, this agent).**
   Step S simulates only person/team absence. v2 §5 acceptance
   explicitly requires employee, system *and* supplier forecasts.
   A failed system breaks its dependents; a failed supplier breaks
   what it supplies.
2. **Process-first forecast (slice 1).** A stable, numbered list of
   *processes* with owners, dependencies and money at risk. Step S
   returns affected entities of every kind; nothing anywhere answers
   "which 14 processes?".
3. **A horizon that changes the answer (slice 1).** Step S records
   `horizon_days` but the math ignores it. L is time-aware:
   `due_at` / remaining `sla_days` decide whether a process is due
   inside the window. A 1-day and a 30-day forecast differ.
4. **Live work in the explanation (slice 1, wired live in slice 5).**
   Each affected process names the workflow runs, approvals and
   invoice batches in flight linked to it — "which live work items
   produced each impact" (v2 §5 acceptance 6).
5. **Visibility in the pure layer (slice 1).** Hidden processes are
   omitted from the list *and* from every count and money total, so
   a restricted caller cannot infer hidden work. Step S enforces
   this at the API edge only.
6. **Standing forecast / always-on watch (slice 2).** Scan every
   critical entity × standard horizons (1/7/14/30 days) on every
   twin change; fingerprint-gated refresh; rules as data with
   cooldown + quiet hours. Step S's watch only evaluates absent
   sets somebody configured.
7. **Forecast records + staleness (slice 3).** Persisted forecasts
   (fingerprint, horizon, unavailable set, process list, `as_of`);
   a forecast whose twin drifted past the threshold is marked stale
   automatically, so nobody acts on a prediction the twin has
   outgrown. Read-only, scope-gated API.
8. **Calendar-aware forecasting (slice 4).** Planned absences from
   HR leave ("Ayesha is on leave 5–12 Dec") become forecasts for
   that window, with lead-time warnings when a backup holder's
   leave overlaps a critical process window.
9. **Forecast-driven proposals (slice 6).** An uncovered critical
   process or a supplier single point of failure becomes an
   approval-gated proposal — never auto-executed.
10. **Drill reconciliation (slice 7 — DONE, see §11).** Forecasts
    compared against real Step G drills: did the drill hit the
    processes the forecast named? Differences go to a review
    queue, never auto-applied.

## 4. Safety / security controls (non-negotiable, carried from S + repo rules)

- **A forecast is advisory only.** It never approves, dispatches,
  grants access, restores data, switches a supplier, or pays. Money
  figures are exposure ("money at risk"), never payment
  instructions.
- **Any forecast-driven action** (slice 6) goes through the
  platform: approval bound to the exact action hash, expiring,
  single-use, separation of duties (nobody approves their own
  proposal), dispatched through `execute_once` with read-back
  verification, full audit trail. A forecast is a suggestion until
  a human approves it.
- **Finance amounts stay owner-only** in forecasts until the owner
  decides otherwise (Step S default, kept). Forecast money totals
  are exposure totals, never account numbers.
- **Snapshot text is untrusted data.** Names, attributes, due
  dates and evidence strings are compared as data only. The
  forecast never `eval`s, `exec`s, or interprets text as an
  instruction; text can never grant a permission, change a payee,
  or change an amount. Amounts come only from numeric `amount`
  attributes (non-numeric / negative → error). `due_at` / `sla_days`
  are validated, never trusted blindly.
- **Restricted users see only their authorized projection.**
  Entity/relationship counts never leak hidden nodes — the
  visibility filter applies to the process list, the affected list,
  every count, and every money total alike. Propagation itself runs
  on the full twin (a hidden dependency still breaks a visible
  process), but the hidden node is never named.
- **A `backup_for` link is a recorded assignment, never an access
  grant.** Effective-permission gaps are listed, not bridged.
- **No secrets** in forecasts, their records, their logs, or their
  tests. Test data is synthetic (fake people, systems, amounts).
  Bounded inputs: forecasts refuse snapshots over `MAX_ENTITIES` /
  `MAX_RELATIONSHIPS` instead of grinding the VM.
- Every lookup/dispatch through `app/platform`
  (`resolve_principal`, `authorize`, `require` → 404 non-members,
  403 members without permission). Tests only on throwaway `*_test`
  databases. Small logical commits. No live folder / live DB / `.env`
  / uploads / data touched. No push — Master Agent pushes.

## 5. Build order (each slice finished + tested before the next)

1. ✅ **Slice 1 — time-aware process forecast (this agent).**
   `app/predictive_continuity/forecast.py` +
   `tests/test_predictive_continuity_forecast.py`. Pure functions,
   stdlib only, no migration, no route, no production code changed.
   Person/team/system/supplier roots; numbered process list with
   owners, dependencies, live work, money at risk; horizon that
   changes the answer (`due_at` / `sla_days`); visibility filter
   with no count leak; fingerprint byte-identical to S horizon / K
   drift. Imports nothing from Step S / K / AH / AL modules (they
   do not exist on this branch yet) — the propagation semantics are
   reimplemented identically so person/team results agree with
   `simulate_absence` once S merges (differential test owed at
   merge, §7).
2. ✅ **Slice 2 — standing forecast / always-on watch (this agent).**
   Pure `scan_forecasts(snapshot, critical_ids, horizons)` over the
   standard horizons (1/7/14/30); fingerprint-gated refresh
   (unchanged twin never re-fires); rules as data (AH pattern)
   with SHA-256 dedupe + cooldown + quiet hours 22:00–07:00 PKT.
   "Critical" = processes with required skills + money entities +
   systems/suppliers with dependents (owner decision §6) — computed
   by `find_critical_ids` (processes/money make their *holders*
   critical, since they are not forecastable roots).
3. ✅ **Slice 3 — forecast records + read-only API (this agent).**
   Migration `0106_forecast_records`: `forecast_records`
   (tenant, root, horizon, fingerprint unique; forecast + snapshot
   JSONB; `as_of`; superseded flag; drift score) + read-only API
   (`GET /api/predictive-continuity/{tenant_id}/forecasts`,
   `GET .../forecasts/{id}`) through `app/platform` (404
   non-member, 403 no permission), visibility-filtered, with a
   zero-side-effect proof (full row dump identical before/after a
   GET burst). Staleness: a superseded record carries the K drift
   score (≤25 trustworthy / ≤60 review / >60 stale) between its
   snapshot and the newer one. Retention default 30 records /
   90 days (owner decision §6).
4. **Slice 4 — calendar-aware forecasting (DONE, this agent).**
   Design + pure functions on leave-record inputs (§11): leave-window
   forecasts (leave length = horizon, leave start = anchor) and
   backup-overlap lead-time warnings (critical when every primary
   holder is also absent for the overlap). Wiring is done in
   `app/predictive_continuity/ag_leave.py`: AG leave-request rows
   map onto the calendar dicts (only `approved` counts as planned;
   `pending` / `rejected` / `cancelled` skipped), the loader reads
   the real `hr_leave_requests` table by reflection (no `app.hr`
   import needed), and `forecast-calendar` runs the path
   end-to-end. The AG table must exist on the database — it is
   created by AG's migration at merge time.
5. **Slice 5 — live-work wiring (DONE, this agent).**
   Real workflow runs, approvals and invoice batches in flight
   feed the forecast explanation (§11): the scan refreshes the
   snapshot's live-work statuses from the live tables before it
   scans, so a forecast refreshes when live work changes, not
   only when the graph changes. Loaders read the real tables by
   reflection (no source-module imports) and the snapshot builder
   must use DB row ids as snapshot entity ids for the match.
   `forecast-scan --live-work` runs the path end-to-end.
6. **Slice 6 — forecast-driven proposals (DONE, this agent).**
   Finding → proposal via platform approvals (exact-action hash,
   single-use, expiring, separation of duties) → `execute_once`
   dispatch with read-back. Audit trail for every proposal,
   approval, and dispatch. The AL engine is not on this branch
   yet (`app/whatif` does not exist here), so no proposal carries
   a scenario today — attaching one when AL merges is owed, and
   slice 6 never builds a second engine.
7. **Slice 7 — drill reconciliation (DONE, this agent).**
   Scheduled comparison of forecasts vs real Step G drills;
   differences → review queue (read-only list + acknowledge);
   never auto-applies, never rewrites drill records. Needed G
   slices 5–7.
8. **Slice 8 — Forecast UI + docs + gates (DONE, this agent).**
   Forecast dashboard (numbered process list, horizon switcher
   1/7/14/30, due-within-horizon highlighting, standing-forecast
   timeline, staleness badges, live-work status, proposals with
   the approval flow, drill-reconciliation review queue);
   `assessment/CHANGES-step-L.md`; full-suite / Alembic /
   frontend gates; merge readiness. **Step L is CODE-COMPLETE.**

## 6. Owner decisions needed (safe defaults in place)

- **Standard horizons:** proposed 1 / 7 / 14 / 30 days, 7 the
  default. Confirm or change.
- **What counts as "critical"** for the standing scan: proposed
  processes with required skills + money entities +
  systems/suppliers with dependents. Confirm.
- **Finance amounts in forecasts:** owner-only today (Step S
  default). May invoice-batch/payment amounts appear for
  authorized finance viewers, or stay owner-only?
- **Who receives forecast alerts, and where:** proposed owner +
  department managers for their own scope; quiet hours
  22:00–07:00 PKT, critical breaks through; in-app first,
  WhatsApp/email later (via Step AA / AF channels). Confirm.
- **`due_at` / `sla_days` source of truth:** proposed workflow due
  dates + invoice due dates + leave calendar. Confirm which
  system owns each deadline.
- **Forecast retention:** proposed 30 records / 90 days. Confirm
  or change.
- **Proposal authority:** who approves forecast-driven actions?
  Proposed: owner only for money/privilege/supplier actions;
  department managers for coverage/backup actions. Confirm.

## 7. Merge notes (read before merging this branch)

- **Step S (`agent/digital-twin`) must merge to main before L, or
  L rebases onto it.** This branch is based on `ca956c2` WITHOUT
  the Step S / K / AH / AL files; slice 1 imports nothing from
  them (stdlib only) so it stands alone on this base.
- **Differential test owed at merge:** same snapshot + same
  person/team absence must give the same affected set from L's
  `forecast_unavailability` and S's `simulate_absence` (same
  propagation semantics, same depth, same backup rules). Any
  difference is a bug in one of the two — fix it before merging.
- **New package `app/predictive_continuity/`** — no `__init__.py`
  conflicts (K creates `app/digital_twin/__init__.py`; different
  package).
- **No migration in slice 1** — no 0104/0105 collision from this
  slice. Slice 3 minted `0106_forecast_records` (checked
  2026-11-28: no 0106 exists in any sibling worktree; 0104 is
  claimed 4× already: HR, WhatsApp AE, AI freshness,
  continuity; 0105 by AK, AI, AB — renumber at merge if another
  branch lands 0106 first; the table set here overlaps with no
  sibling).
- **Slice 4 wiring is done (this agent) and works pre-merge:**
  the loader reads the real AG table by reflection, so it needs
  no `app.hr` code on the branch — but the `hr_leave_requests`
  table must exist on the database (created by AG's migration
  0104 at merge, renumbered per map v5). Slice 5 needs Step S
  merged; slice 7 needs Step G drills (slices 5–7).
  Slice 4 prep (§11) is done without AG: pure functions on
  leave-record dicts; wiring maps AG leave-request rows (statuses
  onto ``PLANNED_STATUSES``) onto them.
- **Scheduler wiring (§11) is done:** ``python -m app.cli
  forecast-scan --tenant-id <uuid> --snapshot twin.json`` runs one
  standing scan and records it; ``--loop`` repeats it;
  ``--state-file`` persists the twin fingerprint (scan gating)
  and alert ``sent_at`` map (cooldown/dedupe) between runs. The
  snapshot is supplied as JSON until Step S merges; a
  ``--from-twin`` mode that builds it with ``build_twin_snapshot``
  is owed then. The state file is operator-side bookkeeping — it
  is never committed and never part of app data.
- **Integration map v5 (Agent 161, 2026-11-28 ~14:50 PKT)
  assigns L's migration 0120** — this branch's
  `0106_forecast_records` renumbers to `0120` at merge (Agent
  161 merges L next, after K). The renumber touches the
  migration filename + `revision`/`down_revision` only; no code
  change.
- **Slice 6 must not duplicate the AL engine** — call it when a
  scenario is needed. `app/whatif` does not exist on this branch,
  so slice 6 ships without scenarios; attaching an AL scenario
  reference to a proposal is owed at merge.
- **Slice 6 minted `0107_forecast_proposals`** (down 0106) for
  the `forecast_proposals` table — 0107 is claimed on sibling
  branches (K twin health alerts, W self-healing probes, J
  workforce budget); renumber at merge per the integration map
  (L's 0106 is 0120 there). Slice 6 also adds the
  `forecast.apply` / `forecast.apply.coverage` duties to
  `APPROVAL_REQUIRED` in `app/platform/access.py` and the matching
  extras to the `company_graph` mirror in `app/platform/sync.py`
  — the same shape as K's `twin.apply` / `twin.apply.coverage`
  wiring on the K branch; the integration reconciles both sets
  (forecast.* + twin.*) at merge.
- **Slice 7 minted `0108_forecast_reconciliations`** (down
  0107) for the `forecast_reconciliations` table — 0108 is
  claimed on a sibling branch; renumber at merge per the
  integration map (L's 0106 is 0120 there). Slice 7 also adds
  the `forecast-reconcile` CLI command (`app/cli.py` is mixed
  CRLF/LF — byte-level edits only).
- **Slice 8 adds no migration** (UI + docs + gates only). The
  page lives at `#/predictive-continuity` ("Predictive
  continuity" in the Research & insights sidebar group); the
  existing `#/forecasts` page is the Step AL what-if page and
  is untouched. Pure view logic is
  `frontend/src/predictive-continuity/predictiveContinuityView.mjs`
  (+ `.d.mts`), tested by
  `frontend/test/predictiveContinuityView.test.mjs` (12 tests).
- Full backend suite + Alembic + frontend gates are owed before any
  L slice merges (same quiet-VM gate as Step E).

## 8. Slice 1 — what was implemented (this branch)

**Module:** `app/predictive_continuity/forecast.py` (pure functions;
stdlib only — `hashlib`, `json`, `collections`, `datetime`,
`decimal`. No imports from `app.db`, no I/O, no network, no
secrets, no imports from Step S / K / AH / AL modules.)

- `snapshot_fingerprint(snapshot)` — SHA-256 over the canonical
  JSON of `entities` + `relationships` (`sort_keys=True`,
  `default=str`), byte-identical to
  `horizon.snapshot_fingerprint` and the K drift fingerprint for
  the same snapshot. A stored `fingerprint` field is trusted when
  present and non-empty.
- `forecast_unavailability(snapshot, unavailable_ids, *,
  horizon_days=7, now=None, visible_ids=None, depth=6)` —
  deterministic forecast:
  - Validation first (same strictness as S impact + K drift):
    snapshots must be dicts; entities/relationships lists;
    entity ids and relationship ids non-empty strings, no
    duplicates; every relationship endpoint exists (dangling →
    `ValueError`); money entities carry a numeric, non-negative
    `amount`; `due_at` must parse as ISO date/datetime;
    `sla_days` must be a non-negative int; attributes must be
    JSON-serializable; snapshots over `MAX_ENTITIES` (50,000) /
    `MAX_RELATIONSHIPS` (100,000) → `ValueError`;
    `unavailable_ids` non-empty, no duplicates, all known, kinds
    person/team/system/supplier only; `horizon_days` / `depth`
    positive ints; `visible_ids` None or a set/list of non-empty
    strings.
  - Propagation (S semantics, extended): person/team absence
    expands teams via `member_of` and risks what absent people
    hold authority over; a failed system/supplier is itself
    affected (distance 0, `failed: True`) and risk flows onward
    along `depends_on` (target → source) and `supplies` /
    authority edges (source → target). Cycle-safe BFS, depth 6
    default; depth limit sets `truncated`.
  - Backup coverage per affected entity (same rule as S): a
    recorded `backup_for` person counts only when not absent and
    holding every required skill; otherwise the reason is
    `no_backup_recorded` / `backup_missing_skills` /
    `backup_absent`.
  - Time-awareness: each affected entity gets `due_status` —
    `due_within_horizon` when `due_at` ≤ `now + horizon_days`
    (on-or-before counts) or remaining `sla_days` ≤
    `horizon_days`; `at_risk_later` when the deadline is beyond
    the window; `no_deadline` when the snapshot gives no time
    information. `due_at` takes precedence over `sla_days`.
  - Process-first view: affected entities of kind `process`,
    sorted by (distance, name, id), numbered 1..N stably. Each
    carries owners (authority holders + `owner_id`/`owner_ids`
    attributes), dependencies (affected `depends_on` targets +
    affected suppliers), per-process money at risk (linked money
    entities), `due_status`/`due_at`, uncovered reason,
    `covered_by`, the edge path that produced the impact, and
    `live_work_items` (affected workflow runs / invoice batches /
    payments / approvals linked to the process, with status).
  - Visibility: `visible_ids` filters the process list, the
    affected list, every count, and every money total. Hidden
    nodes are never named — a hidden unavailable root is reported
    with its id as its name, and hidden owners are omitted from
    `owner_ids`. Propagation itself runs on the full twin.
  - Result shape: `unavailable`, `horizon_days`,
    `horizon_end`, `forecast_at`, `fingerprint`, `processes`,
    `affected`, `uncovered`, `money_at_risk`,
    `money_at_risk_due` (due-within-horizon money only),
    `counts` (processes, processes_due_within_horizon,
    affected, uncovered), `recovery_waves`,
    `blocked_by_dependency_cycle`, `truncated`, `assumptions`,
    `as_of`.
- `forecast_summary(forecast)` — one deterministic line: process
  count, processes due within the horizon, uncovered count, and
  money at risk (total and due). Counts and totals only — no
  attribute values, no names, no secrets.
- `money_at_risk(entities)` — per-currency totals +
  batch/payment counts, same convention as S impact.
- Hostile text is data: names/attributes that look like
  instructions ("ignore previous instructions", "approve all
  payments") are compared as plain strings, appear in the
  forecast as data, and can never grant a permission, change a
  payee, or change an amount — the module has no approval,
  dispatch, or payment path at all.

**Tests:** `tests/test_predictive_continuity_forecast.py` — **48
passed, 0 failed** (pure run, `--noconftest`, 0.88s). Covers:
numbered process list with owners/dependencies/live work/money;
horizon changing the answer (1 vs 7 vs 30 days); `due_at` exactly
on the horizon end; money split by due status; team expansion +
backup breaking; recovery waves + dependency cycles; depth
truncation; system-down and supplier-down forecasts (distance,
dependencies, failed root, supplier not affected by system down);
combined person+system absence; visibility filtering (hidden
process/money never named or counted; hidden root name not
leaked); fingerprint stability + stored fingerprint; determinism;
summary text; standard horizons; money sums; hostile text as data;
all validation errors; size caps; naive `now` read as UTC.

**What slice 1 deliberately does NOT do:** no live sync, no graph
writes, no new tables, no migration, no API route, no UI, no
permission grants, no `execute_once` dispatch, no risk score of
its own (that comes from S horizon / AH when the forecast is
served), no standing scan (slice 2), no calendar awareness
(slice 4). It is the math the later slices build on.

## 9. Slice 2 — what was implemented (this branch)

**Module:** `app/predictive_continuity/watch.py` (pure functions;
stdlib only — `hashlib`, `dataclasses`, `datetime`, `zoneinfo`.
Imports slice 1's `forecast` from the same package only; no imports
from Step S / K / AH / AL modules — they do not exist on this base.)

- `find_critical_ids(snapshot)` — the plan §6 default critical set:
  systems/suppliers with dependents (something `depends_on` them,
  they `supplies` something, or they hold authority over something),
  plus people/teams holding authority over a process with required
  skills or linked to a money entity. A person who only backs up is
  not critical; a system with no dependents is not critical.
- `scan_forecasts(snapshot, critical_ids=None, *, horizons=None,
  now=None, visible_ids=None, previous_fingerprint=None,
  depth=6)` — one forecast per (critical root × horizon), default
  horizons (1, 7, 14, 30). Fingerprint-gated: when
  `previous_fingerprint` matches the current twin fingerprint the
  scan returns `refreshed: False` with no forecasts. Each entry
  carries root id/kind/name, horizon, process/due/uncovered counts,
  `money_due` as sorted (currency, amount) pairs, plus the full
  slice-1 forecast dict. Entries sorted by (root id, horizon);
  deterministic. Bounds: max 200 critical ids, max 8 horizons.
  A naive `now` is read as org time (Asia/Karachi) and converted to
  UTC for the forecast, matching the watch's quiet-hour convention.
- `fingerprint_changed(previous_fingerprint, snapshot)` — mirrors
  the Step S horizon helper; None means "never scanned".
- `ForecastRule` / `ForecastAlert` (frozen dataclasses, AH shape) +
  `DEFAULT_RULES` (4): `forecast_critical` (≥1 due + ≥1 uncovered,
  breaks quiet hours), `forecast_due` (≥3 due), `forecast_uncovered`
  (≥1 uncovered), `supplier_single_point` (supplier root, ≥1 due).
- `evaluate_forecast_watch(scan_result, *, now, rules=...,
  sent_at=None, ...)` — AH pattern: per (rule, root, horizon)
  SHA-256 dedupe fingerprint, cooldown suppression (reported, not
  dropped), quiet hours 22:00–07:00 PKT with non-breaking rules
  reported as `deferred`. Deterministic order: critical first, then
  most due, most uncovered, root id, horizon. Alert detail carries
  counts and money totals only — no process names.
- `is_quiet_hour`, `validate_forecast_rules`, `watch_fingerprint`,
  `format_watch_message` (plain-language `[InfraSpine •
  predictive continuity • <severity>]` line).

**Tests:** `tests/test_predictive_continuity_watch.py` — **49
passed, 0 failed** (pure run, `--noconftest`, 1.19s; run twice,
second run 49 passed). Slice-1 regressions re-run with it: **48
passed** (97 passed combined). Covers: critical-set definition
(incl. backup-only person and standalone system excluded);
12-forecast scan shape and counts; horizon changing the answer;
system/supplier roots; custom horizons; determinism; naive-now
org-time reading; fingerprint gating (skip + refresh); visibility
filtering (hidden process omitted, visible money still counted,
hidden money omitted); all scan validation errors (empty/duplicate/
unknown/wrong-kind critical ids, bad horizons, 200-cap,
bad previous fingerprint); all four default rules firing in
daytime (25 alerts: 9 critical + 1 due + 12 uncovered + 3
supplier) with the expected first alert; quiet hours (9 critical
fire, 16 deferred); cooldown suppress-then-release; disabled
rules; kind/horizon filters; rule validation errors; hostile text
as data (alert carries no approval/dispatch/payment field).

**What slice 2 deliberately does NOT do:** no persistence (slice 3
records forecasts), no API route, no UI, no dispatch or proposal
wiring (slice 6), no calendar awareness (slice 4), no live-work
wiring beyond what slice 1 already names (slice 5). The caller
keeps the last scan result and the `sent_at` map between runs —
the module is stateless.

## 10. Slice 3 — what was implemented (this branch)

**Modules:** `app/predictive_continuity/staleness.py` (pure;
stdlib only), `app/predictive_continuity/models.py` +
`app/predictive_continuity/records.py` (record service),
`app/api/routes/predictive_continuity.py` (read-only API),
`migrations/versions/0106_forecast_records.py`,
`app/core/permission_catalog.py` (`forecasts.read`).

- `assess_staleness(old, new)` (`staleness.py`) — a faithful
  reimplementation of Step K's `app.digital_twin.drift`
  (`agent/digital-twin-v2`, slice 1) because that module does not
  exist on this base: same validation, same weights (removed
  entity 10/cap 40, added 2/cap 10, changed 3/cap 15, removed edge
  3/cap 15, added edge 1/cap 5, changed edge 2/cap 10, money 20 ×
  largest per-currency move share), same trust bands (≤25
  trustworthy / ≤60 review / >60 stale; stale sources force
  stale), same report shape — so this module can be replaced by a
  thin import of `app.digital_twin.drift` when K merges.
  `trust_for_score(score)` derives the band for a stored score
  (`None` → `"unknown"`); `snapshot_fingerprint` mirrors the S/K
  canonical hash. A malformed snapshot raises `ValueError`,
  never a misleading score.
- `ForecastRecord` (`models.py`) — one row per scan forecast:
  tenant (FK `company_graph_tenants`, CASCADE), root id/kind,
  horizon (1–365), fingerprint, forecast + snapshot JSONB,
  `as_of`, superseded flag, drift score (0–100, nullable),
  created_by, created_at. Unique
  (tenant, root, horizon, fingerprint); indexes for
  root+created and tenant+created.
- `record_scan(db, tenant_id, scan_result, snapshot, ...)`
  (`records.py`) — persists one record per scan forecast;
  idempotent per fingerprint (re-recording returns the existing
  rows). A new fingerprint for the same (tenant, root, horizon)
  supersedes the older records and stamps each with the drift
  score between its snapshot and the new one (`None` when either
  snapshot is malformed — the record is still written).
  Called only by the scan path, never by a GET.
- `filter_forecast_visibility(forecast, visible_ids, *,
  snapshot=None)` — pure post-hoc filter: hidden processes are
  omitted from the process list (renumbered), every count, and
  every money total (top level and per process); hidden owners,
  backup people, dependency names, and live-work items are
  omitted the same way; a hidden unavailable root is reported by
  id only. Per-process money is recomputed from visible linked
  money — exactly from the stored snapshot's relationships when
  available, otherwise from the stored BFS paths (a safe lower
  bound, never a leak). `visible_ids=None` (graph owner) returns
  the forecast unchanged. Hardening note: slice 1's compute-time
  visibility does not filter `covered_by` backup names, so a
  hidden backup person's name can appear in a forecast computed
  without `visible_ids`; this filter drops hidden backup entries,
  so the leak cannot reach a reader.
- `list_forecasts` / `get_forecast` (`records.py`) — graph
  membership first via `scope_for` (404 for non-members), then
  org-scope `forecasts.read` via `platform.require` (403 for
  members without it); owner sees everything, other members see
  only their visible graph records. Read-only (SELECTs only).
  `serialize_record` marks a current record trustworthy by
  definition and derives a superseded record's trust from its
  stored drift score. `prune_forecasts` (maintenance only, never
  from a GET): deletes superseded records older than 90 days and
  superseded records beyond the newest 30 per (root, horizon);
  current records are never pruned.
- API (`app/api/routes/predictive_continuity.py`) —
  `GET /api/predictive-continuity/{tenant_id}/forecasts`
  (newest first, current only unless `include_superseded=true`,
  limit capped at 100) and `GET .../forecasts/{id}`. Both are pure
  reads: no writes, no approvals, no side effects.

**Tests:** `tests/test_predictive_continuity_staleness.py` —
**18 passed, 0 failed** (pure run, `--noconftest`), including an
8-pair cross-check against Step K's `diff_snapshots` (identical
scores, trust, diffs, money deltas) and the trust-band edges
(25/26/60/61). `tests/test_predictive_continuity_records.py` —
**13 passed, 0 failed** (throwaway `*_test` DBs): pure filter
tests (restricted process + money hidden from lists/counts/
totals, hidden root named by id only, hidden backup names
dropped, `None` passthrough) + service tests (idempotent
record, supersede with drift score 59 → review band,
owner/viewer views, 403 member without permission, 404
non-member/unknown tenant/unknown record, prune keeps 30 +
deletes aged). `tests/test_predictive_continuity_records_api.py` —
**8 passed, 0 failed**: HTTP list/get for viewer (filtered) and
owner (full), 403/404/401, `include_superseded` flag, and the
zero-side-effect proof (full `forecast_records` row dump
identical before/after a 5-GET burst as viewer + owner).
Slice-1/2 regressions re-run: **97 passed, 0 failed**.
Alembic: roundtrip upgrade head → downgrade base → upgrade head
+ `alembic check` clean on a throwaway DB (`ALEMBIC_RC=0`,
head `0106_forecast_records`).

**Bugs found by the tests (fixed in this slice):**
`prune_forecasts` used `db.scalars()` for a two-column select
(`ValueError: too many values to unpack`) — now `db.execute()`.
The supersede test assumed `created_at` orders two rows written
in one transaction, but `created_at` uses `now()` (the
transaction start), so the timestamps tie — the test now
identifies rows by fingerprint. The filter test's expected
uncovered count was wrong (4, not 5: Bilal holds the approvals
skill, so pr-1 is covered).

**What slice 3 deliberately does NOT do:** no scan scheduling
or CLI entry point that calls `record_scan` (a later slice wires
the watch to the store), no UI, no proposal/dispatch wiring
(slice 6), no calendar awareness (slice 4).

## 11. Scheduler wiring + slice 4 prep — what was implemented (this branch)

**Scheduler (`app/predictive_continuity/scheduler.py` +
`python -m app.cli forecast-scan`).** The owed slice-3 entry
point, wired to the store:

- `load_snapshot(path)` — twin snapshot JSON file (`-` reads
  stdin); non-object JSON and unreadable files raise
  `ValueError`. Strict shape validation happens in
  `scan_forecasts`, so a malformed snapshot writes nothing.
- `build_scan_report(...)` (pure) — one standing scan plus watch
  evaluation: fingerprint-gated (`previous_fingerprint` match →
  `refreshed: False`, no forecasts, no alerts), alerts as
  formatted plain-language lines, suppressed/deferred entries
  with reasons, and the updated `sent_at` map for the next run.
- `run_forecast_scan(db, tenant_id, snapshot, ...)` (async) — one
  pass: unknown tenant/creator → `ValueError`; unchanged twin →
  no writes at all; otherwise `record_scan` (idempotent per
  fingerprint — replays return existing rows, never duplicates),
  optional `prune_forecasts`, one commit. Returns a
  JSON-serializable summary (fingerprint, refreshed, counts,
  recorded new/existing, alert lines, suppressed/deferred,
  pruned, updated `sent_at`).
- `load_state` / `save_state` — JSON state file
  (`{"fingerprint", "sent_at"}`); a missing file means "never
  scanned", a malformed file warns and also means "never scanned"
  (a corrupt state file must never block a scan). Saved
  atomically (temp file + rename).
- CLI (`app/cli.py`, `forecast-scan`): `--tenant-id` (required),
  `--snapshot` (required), `--critical-ids` / `--horizons`
  (defaults: critical set, 1/7/14/30), `--created-by`,
  `--state-file`, `--dry-run` (scan + watch only, no DB, no state
  save), `--prune`, `--force` (ignore stored fingerprint),
  `--loop` + `--interval-seconds` (min 60; re-reads the snapshot
  file each pass; stops on SIGINT/SIGTERM). Exit 0 on a completed
  pass (including an unchanged twin), 2 on usage/validation
  errors. A snapshot load failure inside a loop skips that pass
  and retries next interval instead of killing the scheduler.

**Calendar (`app/predictive_continuity/calendar.py`, pure;
stdlib only).** Slice 4 prep — leave records are plain dicts
(`person_id` / `start` / `end` / optional `status`); no AG
tables are read:

- `normalize_leave_records` — validates (non-empty person,
  ISO dates, end on/after start, ≤365 days inclusive) and sorts
  by (start, person). Records whose status is not in `statuses`
  (default `{"approved"}`) are skipped, not rejected — a pending
  request is not a planned absence.
- `merge_leave_intervals` — per-person merged intervals
  (overlapping or adjacent leave is one absence).
- `leave_window_forecasts` — one slice-1 forecast per leave
  window: horizon = window length in days (inclusive), anchor =
  leave start at midnight UTC. Unknown person → `ValueError`.
- `backup_overlap_warnings` — for every process with recorded
  backup holders, a warning fires when a backup holder's leave
  overlaps the process window ([today, `due_at`], or the next 30
  days when the process has no due date). Each warning names the
  process, the backup holder, the overlapping days, and the lead
  time in days until the overlap starts (0 when already started).
  Severity is `critical` when every primary holder
  (`AUTHORITY_KINDS`: owns / responsible_for / operates /
  approves) is also on leave covering the whole overlap,
  otherwise `warning`. Sorted soonest first. Processes whose due
  date already passed get no warning. Advisory only.

**Tests:** `tests/test_forecast_scheduler.py` — **15 passed, 0
failed** (25:20, full file incl. DB + CLI subprocess tests):
pure report/gating/quiet-hours/cooldown, snapshot + state files,
DB record/idempotence/prune, CLI help/dry-run/usage errors/real
run. `tests/test_forecast_calendar.py` — **13 passed, 0 failed**
(pure run, `--noconftest`): normalize
errors + pending skip, merge collapse, window forecasts (leave
length = horizon; 1-day vs 27-day horizon changes the answer
for a 20 Dec deadline), unknown-person errors, backup warnings
(warning vs critical vs partial-primary vs no-overlap vs
no-backup), soonest-first ordering, bad-input errors.

**Two real bugs found by the first run (fixed on this branch;
the first full-file run was 4 failed / 11 passed):** (1)
`run_forecast_scan` read `entry["fingerprint"]`, but scan
entries carry the fingerprint only inside the nested `forecast`
dict — `KeyError` broke record/idempotence/prune and the CLI
real run. Fixed by resolving the fingerprint the same way
`record_scan` does (top-level key, else nested). (2) The
scheduler defaulted `now` to UTC, but the watch reads the wall
clock of `now` as org time (Asia/Karachi) and never converts —
23:00 PKT (18:00 UTC) was judged daytime, so nothing deferred.
Fixed: the scheduler defaults to `datetime.now(ORG_TZ)`
(documented in the docstrings; callers passing an explicit
`now` must pass org time), and the test fixtures use PKT-aware
datetimes, with a pinning case that UTC 18:00 defers nothing.

**What this slice deliberately does NOT do:** no `--from-twin`
snapshot mode (owed when Step S merges — the snapshot is
supplied as JSON until then), no alert dispatch anywhere (alert
lines print to stdout; control-room surfacing is slice 8 / Q-AF,
not merged), no UI, no proposal/dispatch wiring (slice 6).

**Slice 4 wiring — AG leave into the calendar math (this agent).**
`app/predictive_continuity/ag_leave.py` + `python -m app.cli
forecast-calendar` (see module docstring for the full rationale):

- `map_ag_leave_request` / `map_ag_leave_requests` — pure mapping
  of AG rows (ORM row, SQLAlchemy Row, or plain dict) onto calendar
  dicts. Statuses grounded in AG's model (`agent/hr-module` HEAD
  `4d07feb`): the CHECK allows only `pending` / `approved` /
  `rejected` / `cancelled`; only `approved` counts as planned
  (pending/rejected/cancelled skipped, not rejected). There is NO
  `applied` status — an executed request keeps `approved` and is
  marked by `applied_at` (recorded as `applied: True` for
  context). A half-day leave maps to its whole day (conservative:
  calendar math works in whole days; advisory-safe). Malformed
  rows (unknown status, missing person/dates, end before start,
  >365 days) raise `ValueError`.
- `load_ag_leave_records(session, group_id, *, start, end, limit)`
  (async, read-only) — reflects the `hr_leave_requests` table, so
  no `app.hr` import is needed and the wiring works before AG
  merges; selects `approved` rows for one department overlapping
  the window, ordered by (start, person), capped at 200 (same cap
  as AG's own `list_team_leave`). Missing table or columns raise
  `AgLeaveUnavailable` (a merge/deployment state, not a bad
  input). The service-layer path is the same mapping: a caller
  holding a principal uses `HrService.list_team_leave(group_id)`
  and passes the rows to `map_ag_leave_requests`.
- `calendar_forecasts_from_ag(snapshot, session, group_id, ...)`
  (async) — end to end: loads approved leave overlapping
  `[today, today + 365 days]` by default, runs
  `leave_window_forecasts` + `backup_overlap_warnings`, returns
  `{"group_id", "leave_records", "window_forecasts",
  "backup_warnings"}`. Advisory only; unknown people in the
  snapshot still raise `ValueError`.
- CLI (`app/cli.py`, `forecast-calendar`): `--group-id`
  (required, must be an existing department — a typo'd id exits 2
  instead of silently returning nothing), `--snapshot`
  (required), `--window-days` (default 30). Prints the JSON
  result. Writes nothing. Exit 0 on a completed run, 2 on usage
  or validation errors. (`app/cli.py` is mixed CRLF/LF — the
  command was inserted with a byte-level script preserving line
  endings.)

**Tests:** `tests/test_forecast_ag_leave.py` — **22 passed, 0
failed** (pure mapping 9; loader 6; end-to-end 3; CLI 4): the DB
tests create `hr_leave_requests` from the REAL AG model DDL
(loaded from the sibling `agent/hr-module` worktree, skipped
when it is absent), insert rows through the real AG model (so
Postgres CHECK/FK constraints enforce the status set and the
person/group references), and read them back through the loader
— genuinely end-to-end on real AG data. The loader's column list
is pinned against the real AG model, and one test proves the
`applied` status is rejected by the database CHECK.

**Two test bugs found by the first runs (fixed on this branch;
the first DB run was 7 passed / 6 failed):** (1) the seed helper
flushed the department row but never committed it — the AG
table's `group_id` FK rejected every leave insert. Fixed by
committing the seed. (2) The unknown-person test inserted leave
for a random UUID, but the AG `employee_id` FK requires a real
user — fixed by using the seeded admin (a real user who is not a
person in the snapshot).

**Owed after slice 4:** `--from-twin` snapshot mode when Step S
merges; swap `staleness.py` for the `app.digital_twin.drift`
import when K merges (`app/digital_twin` does not exist on this
branch — cross-check test pins equivalence until then); full
backend suite before merge (load control); slice 5 (live-work
wiring) is next.

**Slice 5 wiring — live work into the forecast (this agent).**
`app/predictive_continuity/live_work.py` + scheduler/CLI wiring
(see module docstring for the full rationale):

- `normalize_live_work_item(s)` — pure validation. Kind must be
  one of `workflow_run` / `invoice_batch` / `payment` /
  `approval` (the slice-1 kinds); id a non-empty string; optional
  name/status/process_id/due_at (ISO date stays a bare date, a
  datetime keeps its time)/amount→float/currency upper/subject/
  source. Unknown keys raise `ValueError`; cap 500 items.
- `live_work_fingerprint` (SHA-256 over canonical JSON) /
  `live_work_changes` (added/removed/changed with changed field
  names) / `live_work_refresh_needed` (true when the twin OR the
  live-work fingerprint differs; no previous record → True) /
  `summarize_live_work` (counts only — no amounts, no names).
- `refresh_snapshot_live_work(snapshot, items)` — pure refresh:
  matches snapshot live-work entities to live items by
  `(kind, id)` and replaces only `attributes.status` with the
  live status. Live items with no snapshot entity are reported
  as `added`, never inserted (the snapshot owns the graph);
  snapshot entities with no live row are reported as `stale` and
  keep their snapshot status. **Contract for the snapshot
  builder (`--from-twin`): it must use the DB row id as the
  snapshot entity id, or the match silently reports everything
  stale/added.**
- `attach_live_work(snapshot, items)` — explicit attach: items
  with `process_id` land in that process's `live_work_items`
  (live item wins on a same `(kind, id)` dedup); unmatched items
  are reported, never dropped.
- Loaders (async, read-only, by reflection — no source-module
  imports, so they work pre-merge; missing table/columns raise
  `LiveWorkUnavailable`): `load_workflow_runs` reads the unified
  engine `wf_runs` (in-flight `queued` / `running` / `paused` /
  `compensating`; name from `wf_definitions`; `due_at` =
  `deadline_at`) plus the legacy mirror `automation_runs`
  (in-flight `pending_approval` / `queued` / `processing`; name
  from `automation_workflows`); `load_pending_approvals` reads
  `platform_approval_requests` `pending` only (`due_at` =
  `expires_at`; amount/currency carried); `load_invoice_batches`
  reads `fin_invoice_batches` in-flight (`open` / `verifying` /
  `awaiting_approval` / `paying`); `load_payments` reads
  `fin_payments` in-flight (`awaiting_approval` / `approved` /
  `dispatched`; subject `batch:<uuid>`). `load_live_work`
  combines all four, per-source cap 200, total cap 500. Status
  sets are verbatim from each table's DB CHECK. **No tenant
  filter on the loaders — the snapshot is the scoping boundary**
  (only items matching snapshot entities ever reach a forecast).
- Scheduler (`run_forecast_scan(..., live_work_items=None)`):
  refreshes the snapshot before scanning and records the
  *refreshed* snapshot; the summary gains a `live_work` block
  (counts + fingerprints only) and `format_scan_summary` adds a
  `live-work updated=… added=… stale=… matched=…` line.
  `build_scan_report` is unchanged (pure, takes the
  already-refreshed snapshot).
- CLI (`app/cli.py`, `forecast-scan --live-work`): loads live
  work inside the DB session and passes it to the scan;
  `--dry-run --live-work` exits 2; `LiveWorkUnavailable` exits 2.
  (`app/cli.py` is mixed CRLF/LF — the flag was added with a
  byte-level edit preserving line endings; CRLF count 299→300,
  only the one added argparse line.)

**Tests:** `tests/test_forecast_live_work.py` — **30 passed, 0
failed** in one process (pure 13; loaders 10; end-to-end refresh
1; scheduler 3; CLI 3): the DB tests seed
one in-flight + one terminal row per source through the real
models, and the loader column lists are pinned against the real
models. The end-to-end test proves a seeded run/batch/approval/
payment flows from the live tables into a recorded forecast's
`live_work_items` with live statuses, and the scheduler tests
prove a scan refreshes only when live work changes (run 2 same
snapshot+items → refreshed False; run 3 after pausing the run →
refreshed True + supersede).

**Three real bugs found by the first runs (fixed on this
branch; the first full run was 16 passed / 14 failed):**
(1) `_coerce_due_at` tried `datetime.fromisoformat` first, so a
bare date `"2026-12-02"` became `"2026-12-02T00:00:00"` — fixed
by trying `date.fromisoformat` first (bare date stays a bare
date, matching slice 1). (2) The seed used
`AccessGroup(name=...)` without `kind`, which the DB rejects
(NOT NULL + CHECK) — fixed with `kind="department"`. (3) The
`automation_runs` table is trigger-guarded (found by reading
migration `0025`): INSERT must begin `pending_approval` with
attempts 0, reaching `queued` needs a real
`automation_approvals` row from a *different*, active admin
approver, and only the legal transitions may follow — the seed
now walks the machine (insert pending → approve → queued →
processing → failed, one flush per hop). The test snapshot also
had to include the legacy automation run as a `workflow_run`
entity, since both run tables map to that one kind.

**Owed after slice 5:** `--from-twin` snapshot mode when Step S
merges (the snapshot builder must use DB row ids as entity ids
— see the contract above); swap `staleness.py` for the
`app.digital_twin.drift` import when K merges; full backend
suite before merge (load control); slice 6 (forecast-driven
proposals) is next.

**Slice 6 — forecast-driven proposals (this agent).**
`app/predictive_continuity/proposals.py` (pure; stdlib only) +
`app/predictive_continuity/proposals_records.py` (service) +
`ForecastProposal` in `app/predictive_continuity/models.py` +
migration `0107_forecast_proposals` + API in
`app/api/routes/predictive_continuity.py`. Pattern: Step K v2
slice 6 (`agent/digital-twin-v2`, read as a reference — no K
code copied, L has its own table, duties, and action kinds).

- `findings_from_forecast(forecast, *, tenant_id)` — every
  process with no backup coverage is an `uncovered_process`
  finding (`critical` when due within the horizon, `warning`
  otherwise; money = that process's money at risk); every
  unavailable supplier root with ≥1 due process is a
  `supplier_spof` finding (`critical`; money = the forecast's
  due money) — the same condition as the watch rule
  `supplier_single_point`. A healthy forecast yields nothing.
  `findings_from_calendar(warnings, *, tenant_id)` — every
  `backup_unavailable` warning is a `backup_overlap` finding
  with the warning's own severity and no money. Findings are
  sorted and deduped by fingerprint; anything unclassifiable
  is rejected (fail-closed).
- `draft_proposal` — three action kinds only, approver class
  fixed by the finding (plan §6 safe default):
  `forecast.request_backup` (coverage) for uncovered processes,
  `forecast.request_supplier_review` (owner) for supplier
  single points of failure, `forecast.request_backup_review`
  (coverage) for leave overlaps. The canonical ActionSpec +
  hash are built by a byte-identical port of the platform
  canonical form, pinned by a DB test that rebuilds the real
  ActionSpec and compares hashes.
- `ForecastProposal` — one row per (tenant, finding, action):
  finding summary (money owner-only, redacted for non-owners
  at read), canonical action + hash, required permission, duty,
  lifecycle `draft → pending_approval → approved | rejected |
  expired | cancelled → dispatched → verified | failed`.
  `create_proposal` is idempotent per finding (re-proposal
  returns the existing row); rejected/expired/failed may be
  re-proposed with a fresh approval; cancelled stays cancelled.
  `decide_proposal` mirrors the platform approval outcome
  (expired approvals mark the proposal expired too).
  `execute_proposal` dispatches through `execute_once` (duty
  `forecast.apply` / `forecast.apply.coverage`, idempotency key
  bound to the approval) publishing a durable
  `forecast.proposal_action` outbox event, verified by
  read-back; three-way separation of duties (executor may be
  neither proposer nor approver). `cancel_proposal` (proposer
  only) cancels the pending approval with it. `prune_proposals`
  deletes terminal proposals only (newest 100 / 90 days);
  pending, approved, and dispatched proposals are never pruned.
- Platform wiring (same shape as K's twin duties):
  `APPROVAL_REQUIRED` gains `forecast.apply` →
  `company_graph.role.owner` and `forecast.apply.coverage` →
  `company_graph.role.approver`; the `company_graph` mirror in
  `app/platform/sync.py` gives the owner both duties +
  `company_graph.role.approver` (superset of approver) and the
  approver `forecast.apply.coverage`.
- API — `POST /{tenant_id}/proposals` (exactly one of
  `forecast_record_id` + `finding_code` + `resource_id`, where
  the server extracts the finding from the stored record so it
  cannot be fabricated, or a caller-built `finding` dict
  validated fail-closed), `GET .../proposals` (+ `/counts`),
  `GET .../proposals/{id}`, `POST .../decide` (presented hash),
  `POST .../execute`, `POST .../cancel`. Graph membership
  first (404 non-members); money redacted for non-owners.

**Tests:** `tests/test_forecast_proposals.py` — **22 passed, 0
failed** (pure run, `--noconftest`): extraction (due→critical,
later→warning, supplier SPOF only with due work, healthy
forecast proposes nothing), calendar warnings, validation +
eligibility, drafts (shape, owner/coverage classes,
determinism, hash binding), fingerprints, hostile text as data.
`tests/test_forecast_proposals_records.py` — **25 passed, 0
failed** (one process, throwaway `*_test` DBs): service create
(coverage + owner classes, calendar finding), idempotence,
draft-hash match against the real ActionSpec, full flow
approve→execute→replay (one outbox event), money proposal,
wrong-hash conflict, proposer/approver/executor separation of
duties, execute-before-approve conflict, expired approval
marks the proposal expired, reject-then-repropose,
cancel-blocks-reproposal, prune keeps actionable, unknown
finding code; API create (from record + caller finding),
missing finding 404, exactly-one-source, full flow via HTTP,
viewer money redacted, 404/401. Alembic: roundtrip upgrade
head → downgrade −1 → upgrade head + `alembic check` clean on
a throwaway DB (head `0107_forecast_proposals`); the downgrade
refusal was proven (a stored row blocks the downgrade).

**Three real bugs found by the tests (fixed on this branch;
the first full DB run was 19 passed / 6 failed):** (1)
`findings_from_calendar` used an en-dash in the finding detail
— the throwaway test cluster is SQL_ASCII, so the insert
failed; details now use plain ASCII. (2)
`prune_proposals` deleted rows but never committed, so the
prune rolled back and the rows stayed — it now commits after
the delete (the K pattern it mirrors has the same latent gap).
(3) The money-finding fixture assumed a backup's absence
uncovers its process — it does not (a backup being absent
never puts a process in the affected set; only the owner being
absent does), so the fixture now drops the backup edge instead.
Two test-only fixes: the 401 API test cleared the shared
client's admin session cookie first; the calendar-validation
test built its bad-type dict the wrong way round.

**Owed after slice 6:** `--from-twin` snapshot mode when Step S
merges; swap `staleness.py` for the `app.digital_twin.drift`
import when K merges; attach an AL scenario reference to a
proposal when AL merges (`app/whatif` does not exist on this
branch — no second engine built); full backend suite before
merge (load control); slice 7 (drill reconciliation) is next.
`assessment/CHANGES-step-L.md` created, documenting slices
1–6 in plain language with zero `[PENDING]`.

**Slice 7 — drill reconciliation (this agent).**
`app/predictive_continuity/reconciliation.py` (pure; stdlib only)
+ `app/predictive_continuity/reconciliation_records.py`
(service) + `ForecastReconciliation` in
`app/predictive_continuity/models.py` + migration
`0108_forecast_reconciliations` + API in
`app/api/routes/predictive_continuity.py` + CLI
`forecast-reconcile` in `app/cli.py`. Pattern: Step K v2 slice 5
(`agent/digital-twin-v2`, read as a reference — no K code
copied; L compares its own forecast records, not twin
simulations, and acks per reconciliation report, not per
finding). Step G drills slices 5–7 are DONE
(`agent/stepg-continuity` HEAD `445435d`, merged into
integrate); G is not on this branch, so the G side arrives as
the plain dict G's `drill_comparison` returns — the CLI reads
it from a JSON file, the API takes it in the request body. When
G merges, the scheduler can call `drill_comparison` directly;
nothing in the pure layer needs to change.

- `reconcile(forecast, g_comparison, *, tenant_id,
  drill_run_id=None, forecast_record_id=None)` — every forecast
  entity the drill did not recover is a `forecasted_not_drilled`
  finding (`critical` when a process due within the horizon,
  `warning` otherwise; money = that process's money at risk);
  every drilled entity the forecast never named is a
  `drilled_not_forecasted` finding (`warning` — the miss that
  feeds back into trust); G's own timing differences are adapted
  verbatim and graded by the same severity map K uses (the codes
  are G's contract). Findings are deduped by fingerprint, capped
  at 500, sorted deterministically; `matches` is true only when
  neither side reports a difference. Detail text is plain ASCII
  (SQL_ASCII test cluster — slice 6 lesson).
- `miss_penalty(findings)` / `apply_miss_penalty(drift_score,
  findings)` — a real miss costs 15 drift points, a critical
  finding 10, a warning 5, info 0; capped at 100. The service
  folds the penalty into the linked forecast record's stored
  drift score (raised, never lowered — a matching drill never
  lowers drift, because drift measures twin change). The
  forecast content and the drill record are never rewritten.
- Service: `evaluate_reconciliation` (pure reconcile + idempotent
  store per (tenant, drill run, forecast record) + penalty +
  prune) is the scheduled entry; `list_reconciliations`
  (filters: acknowledged / worst severity / drill run),
  `reconciliation_counts`, `get_reconciliation`,
  `acknowledge_reconciliation` (marks seen, who + when; the
  report, forecast and drill are untouched),
  `prune_reconciliations` (newest 100 / 90 days). The service
  commits its own writes (the proposals pattern — the request
  layer does not commit).
- API: `POST /{tenant_id}/reconciliations`
  (`forecast_record_id` + `drill_comparison` + optional
  `drill_kind`), `GET .../reconciliations`,
  `GET .../reconciliations/counts`,
  `GET .../reconciliations/{id}`,
  `POST .../reconciliations/{id}/acknowledge`. Graph membership
  first (404), then `forecasts.read` (403); finding money is
  owner-only (redacted for non-owners).
- CLI: `forecast-reconcile --tenant-id <uuid>
  --forecast-record-id <uuid> --drill-comparison <json>
  [--drill-kind <kind>] [--dry-run]`; exit 0 on a completed run,
  2 on a usage/validation error. `app/cli.py` is mixed CRLF/LF
  on this branch — the parser/dispatch edits were byte-level so
  the line endings are preserved.
- `tests/test_forecast_reconciliation.py` — **20 passed, 0
  failed** (`--noconftest`): match, over-prediction critical /
  warning, miss, G grading verbatim, unknown G code, run-id
  mismatch, dedupe, fingerprint stability + scoping, 500 cap,
  validation (missing run id / processes / fingerprint / drill /
  differences, non-boolean matches), penalty weights + cap +
  never-lowers.
- `tests/test_forecast_reconciliation_records.py` — **20 passed,
  0 failed** (one process, throwaway `*_test` DBs): service match
  (nothing queued, drift untouched), over-prediction + G
  difference (2 critical findings, drift None → 20), evaluate
  idempotence, miss (penalty 15), second reconciliation only
  raises drift (15 → 25), wrong-tenant + malformed comparison,
  list/counts/acknowledge, prune (keep + age), money redaction;
  API create/list/get/counts, idempotent re-create, acknowledge,
  viewer money redacted, 403 without `forecasts.read`, 404
  non-member/unknown ids, malformed comparison 400/422, 401;
  CLI help, unknown record exits 2, real run (dry run writes
  nothing, real run records + raises drift, re-run idempotent).
  Regressions in one process each: pure L files **170 passed**
  (forecast 48 + watch 49 + staleness 18 + calendar 13 +
  proposals 22 + reconciliation 20); DB L files **133 passed**
  (records 13 + records_api 8 + scheduler 15 + ag_leave 22 +
  live_work 30 + proposals_records 25 + reconciliation_records
  20). Alembic: roundtrip upgrade head → downgrade −1 →
  upgrade head + `alembic check` clean on a throwaway DB (head
  `0108_forecast_reconciliations`); the downgrade refusal was
  proven (a stored row blocks the downgrade).

**Two real bugs found by the tests (fixed on this branch):**
(1) The service layer never committed: `evaluate_reconciliation`
flushed but the request layer does not commit, so the API create
returned 200 with the report while the list showed 0 rows — the
service now commits its own writes (the proposals pattern), in
`evaluate_reconciliation` and `acknowledge_reconciliation`.
(2) Two pure-test regexes asserted the wrong error wording
("different drill run" vs the actual "is for drill run") —
test-only fixes.

**Owed after slice 7:** `--from-twin` snapshot mode when Step S
merges; swap `staleness.py` for the `app.digital_twin.drift`
import when K merges; attach an AL scenario reference to a
proposal when AL merges; a direct G `drill_comparison` call from
the scheduler when G merges (today the CLI/API take the plain
comparison dict as input); full backend suite before merge (load
control); slice 8 (forecast UI + docs + gates) is next.

**Slice 8 — Forecast UI + docs + gates (this agent).**
`frontend/src/components/PredictiveContinuityPage.tsx` +
`frontend/src/predictive-continuity/` (`types.ts`, `api.ts`,
`predictiveContinuityView.mjs` + `.d.mts`) +
`frontend/test/predictiveContinuityView.test.mjs` (12 tests).
Wired at `#/predictive-continuity` ("Predictive continuity" in
the Research & insights sidebar group, next to What-if
forecasts); the existing `#/forecasts` page is the Step AL
what-if page and is untouched.

- The page loads tenants through the company-graph API
  (default: first tenant; a picker when there is more than
  one) and shows four header stats (current forecast records,
  processes due in the selected horizon, actionable proposals,
  unacknowledged reconciliations), then three tabs.
- **Forecasts tab:** horizon switcher 1 / 7 / 14 / 30 (the
  switcher filters both the standing forecast and the record
  list), a "show superseded" toggle, the standing forecast
  (newest current record per unavailable root, with trust
  badge + drift score), and the record list newest first.
  Selecting a record shows its numbered process list with
  due-within-horizon highlighting, owners, dependencies,
  coverage reason / covered-by, money at risk, and the live
  work items in flight per process. An uncovered process
  offers "Propose backup coverage" and a supplier root with
  due processes offers "Propose supplier review" — both call
  `POST /proposals` with the finding taken from the stored
  record (the server extracts it, so it cannot be fabricated)
  and land on the Proposals tab; proposing only asks.
- **Proposals tab:** every proposal with its finding, severity,
  action kind, approver class, action hash (short form) and
  money (or "Money hidden — owner only" when the server
  redacted it). Buttons follow
  `allowedProposalActions` exactly: Approve / Reject only
  while `pending_approval` (a confirm step shows the action
  hash the decision binds to, plus an optional note),
  Dispatch only once `approved`, Cancel only while draft or
  pending. Every other state is display-only; the server
  enforces the same rules independently.
- **Review queue tab:** unacknowledged reconciliations newest
  first (match / difference, worst severity, findings with
  money or the hidden note, adjusted drift score) with a
  "Mark as seen" button, plus the acknowledged list below.
  Acknowledging changes only the queue row.
- Pure view logic lives in `predictiveContinuityView.mjs`
  (trust/severity badges, finding + status labels, the
  three-pair action mapping, the allowed-actions discipline,
  horizon filter, standing forecast, due/uncovered processes,
  record summaries, money formatting with redaction as null,
  review-queue filter, hash shortening) so the discipline is
  unit-tested without a browser: 12 tests, all green.
- No migration in this slice. No backend code changed.

**Tests:** frontend `npm test` **34 passed, 0 failed** (22
existing + 12 new); `tsc --noEmit` clean; `vite build` clean
(`PredictiveContinuityPage-*.js` emitted). Backend
regressions in one process each: pure L files **170 passed**
(forecast 48 + watch 49 + staleness 18 + calendar 13 +
proposals 22 + reconciliation 20); DB L files **133 passed**
(records 13 + records_api 8 + scheduler 15 + ag_leave 22 +
live_work 30 + proposals_records 25 +
reconciliation_records 20). Alembic: roundtrip upgrade head →
downgrade −1 → upgrade head + `alembic check` clean on a
throwaway DB (head `0108_forecast_reconciliations`).

**One test-only fix in this slice:** the PKR money test
expected "$10.00 · PKR 5,000.00", but Intl formats PKR with
zero fraction digits and a non-breaking space after the code
("PKR 5,000") — the expectation was corrected to the real
Intl output (implementation unchanged).

**Owed after slice 8 (merge-time, unchanged):** `--from-twin`
snapshot mode when Step S merges; swap `staleness.py` for the
`app.digital_twin.drift` import when K merges; attach an AL
scenario reference to a proposal when AL merges; a direct G
`drill_comparison` call from the scheduler when G merges;
full backend suite before merge (load control). **Step L is
CODE-COMPLETE — no slice 9.**
