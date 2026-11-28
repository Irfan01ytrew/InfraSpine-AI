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
10. **Drill reconciliation (slice 7).** Forecasts compared against
    real Step G drills: did the drill hit the processes the
    forecast named? Differences go to a review queue, never
    auto-applied.

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
4. **Slice 4 — calendar-aware forecasting (prep done, this agent;
   AG wiring owed).** Design + pure functions on leave-record
   inputs (§11): leave-window forecasts (leave length = horizon,
   leave start = anchor) and backup-overlap lead-time warnings
   (critical when every primary holder is also absent for the
   overlap). Needs AG leave data merged first — wiring is a thin
   mapping of AG leave-request rows onto these dicts.
5. **Slice 5 — live-work wiring.** Real workflow runs, approvals
   and invoice batches in flight feed the forecast explanation
   (uses S snapshot workflow-run entities; needs S merged).
   Forecast refreshes when live work changes, not only when the
   graph changes.
6. **Slice 6 — forecast-driven proposals.** Finding → proposal via
   platform approvals (exact-action hash, single-use, expiring,
   separation of duties) → `execute_once` dispatch with read-back.
   Audit trail for every proposal, approval, and dispatch. May call
   the AL engine for a scenario when a proposal needs one — never
   builds a second engine.
7. **Slice 7 — drill reconciliation.** Scheduled comparison of
   forecasts vs real Step G drills; differences → review queue
   (read-only list + acknowledge); never auto-applies, never
   rewrites drill records. Needs G slices 5–7.
8. **Slice 8 — Forecast UI + docs + gates.** Forecast dashboard
   (numbered process list, horizon switcher 1/7/14/30,
   due-within-horizon highlighting, standing-forecast timeline,
   staleness badges); `assessment/CHANGES-step-L.md`; full-suite /
   Alembic / frontend gates; merge readiness.

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
- **Slice 4 needs Step AG leave data merged first; slice 5 needs
  Step S merged; slice 7 needs Step G drills (slices 5–7).**
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
  scenario is needed.
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
not merged), no AG wiring (statuses map at wiring time), no UI,
no proposal/dispatch wiring (slice 6).
