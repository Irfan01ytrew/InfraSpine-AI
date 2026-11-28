# Step AJ — Business continuity dashboard: plan

> Prepared by Agent 46 (continuity-dashboard), branch `agent/continuity-dashboard`,
> HEAD `ca956c2` (step-e-gaps-wip).
> Status: **slices 1–4 done** (see §5); slice 5 (docs + gates) remains.

## 1. What the dashboard is

One screen for the owner / department manager that answers, at a glance,
"is the company safe right now, and what needs me?":

- **Open risks** — uncovered critical resources, continuity gaps, predicted
  risks, and live alerts, counted by severity, worst first.
- **Active incidents** — recovery incidents that are not resolved/cancelled,
  with overdue-SLA flags.
- **Pending approvals** — every approval the viewer may decide (continuity
  plans/runs + platform approvals), with expiring-soon flags.
- **Recovery progress** — every continuity run / recovery in flight, with
  verified-steps progress (%), RTO-at-risk flags, and an average progress.
- **Health strip** — the Step G health roll-up (`ok` / `degraded` / `down`),
  passed through, not recomputed.

The dashboard is **read-only**. It never executes an action, never dispatches
a workflow, never decides an approval. Every button on it (later slices)
links to the existing place that does the action; the dashboard itself only
shows.

## 2. What already exists (mapped 2026-11-28)

| Need | Existing surface | Gap |
|---|---|---|
| Continuity data | `app/continuity/service.py::dashboard(db, scope)` — nodes, links, assessments, plans, scenarios, runs, `gaps` (`intelligence.analyze`), metrics: `critical_resources`, `bia_assessed`, `plan_covered`, `plan_coverage_percent`, `pending_approvals`, `verified_drills`, `active_recoveries` | Returns raw lists, not a rolled-up "what needs attention" view; no risk severity roll-up |
| Recovery incidents | `app/recovery/service.py::dashboard(db, scope)` — incidents, metrics: `active`, `by_state`, `overdue`, `recurring`, `mttr_seconds`, `recovery_success_percent`, `commands` | Separate screen; not combined with continuity risks/approvals |
| Pending approvals | `GET /api/approvals` inbox (`app/api/routes/approvals.py`) — pending, unexpired `PlatformApprovalRequest`s, each individually authorized via `authorize()`; continuity plans (`pending`) + runs (`pending`) in `app/continuity/service.py::dashboard` | No combined pending count; no expiring-soon flag |
| Health roll-up | `app/continuity/health.py::aggregate_health` (Step G slice 1, Agent 16) — probes → `ok`/`degraded`/`down`, stale-probe handling | Not shown on any continuity screen yet |
| Agent/workflow status | `app/control_room/service.py::control_room_overview` (Step Q slice 1, Agent 22) — worker flag, runs by status/origin, agents, blockers | Belongs to Q; AJ links to it, does not rebuild it |
| Live alerts | `app/control_room/alerts.py::evaluate_alerts` (Step AF slice 1, Agent 39) — 13 rules, severity, cooldown/dedup, quiet hours 22:00–07:00 PKT | Alert output is not yet fed into any dashboard |
| Risk prediction | Step AH (Agent 43, in progress) — dependency map + predicted risks with impact and recovery options | Not finished yet; AJ consumes its risk items once they exist, as plain inputs |
| Authorization | `app/continuity/access.py` (continuity scope: role + `all_departments`); `app/platform/access.py` (`resolve_principal`, `authorize`, `require` → 404 non-members, 403 no permission); `app/workflows/security.py::require_scope` | Nothing to add — reuse as-is |

Key finding: **no new tables or migrations are needed for slice 1, and none
are expected for the whole step.** Every section of the dashboard reads data
that already exists; AJ is a read-only aggregation over Step G, recovery,
platform approvals, Step Q, and Step AF — it must never become a second
execution path or a second copy of any of those stores.

## 3. Authorization model

- Every dashboard read takes a continuity `scope` (tenant + department
  scope, same as `app/continuity/service.py::dashboard`).
- The route (slice 2) resolves the caller with the standard `CurrentUser`
  dependency, then applies the continuity scope checks in
  `app/continuity/access.py`:
  - Non-member or unknown scope → **404** (the scope is not revealed).
  - Member without the permission → **403**.
- **Each section is filtered to the viewer's scope before it is counted.**
  A risk, incident, approval, or recovery outside the viewer's departments
  never appears in a count, a list, or a highlight — not even as a number.
  Pending platform approvals are filtered exactly like the approvals inbox
  does: each row individually authorized via `authorize()` on its own
  `scope_id` (never trust the list query alone).
- No sealed payloads are decrypted or returned: workflow run `payload`
  (AES-GCM envelope) and any ciphertext are never exposed; only status,
  counts, titles, severities, and timestamps are shown.
- The dashboard shows a recovery's progress and RTO risk; it never shows
  who approved what beyond the approval's own `requested_by`/`subject`
  fields already exposed by the approvals inbox.

## 4. Slice 1 — pure-function dashboard aggregator (done)

`app/continuity/dashboard_summary.py` — standard library only, no I/O, no
database, no network, no secrets. Inputs are plain dataclasses, so any
later slice can feed it from any source (SQL rows, service results, AF
alerts, AH predictions) without changing it.

- `RiskInput(id, title, severity, category, source, affected, has_plan)` —
  severity in `critical` / `high` / `medium` / `low`; `source` in `gap` /
  `incident` / `alert` / `prediction`; `has_plan` says a recovery plan
  covers it.
- `IncidentInput(id, title, severity, status, overdue)` — status is a
  recovery-incident status; `resolved` / `cancelled` are terminal and never
  count as active (matches `app/recovery/policy.py::TERMINAL`).
- `ApprovalInput(id, action_type, subject, expires_at, own_request)` —
  `expires_at` is a timezone-aware datetime or `None` (no expiry recorded).
- `RecoveryInput(id, title, status, verified_steps, total_steps,
  rto_minutes, elapsed_minutes)` — status is a continuity-run status
  (`pending` / `active` / `suspended` / `rejected` / `closed`); progress is
  `verified_steps / total_steps`.
- `build_dashboard_summary(*, risks, incidents, approvals, recoveries,
  health=None, now=None)` → one dict:
  - `overall_status`: `critical` if any critical risk, any active
    critical-severity incident, or any recovery past its RTO; `attention`
    if anything is open (any risk, active incident, pending approval, or
    open recovery); otherwise `ok`.
  - `risks`: total, counts by severity, `uncovered_critical` (critical
    risks with `has_plan=False`), `top` (worst 5: severity rank, then
    title).
  - `incidents`: `total_active`, counts by severity, `overdue` count,
    `top` (worst 5: overdue first, then severity rank, then title).
  - `approvals`: `pending`, `expiring_soon` (expires within 24 h),
    `own_requests` (pending approvals the viewer requested themself).
  - `recovery`: `active`, `closed`, `average_progress_percent` (mean over
    recoveries with steps, 1 decimal, `None` when nothing has steps),
    `at_risk` (active/suspended recoveries past RTO), per-item progress.
  - `health`: the Step G health roll-up passed through unchanged, or
    `None`.
  - `highlights`: plain-language "needs attention" lines, worst first,
    capped at 6 (e.g. "2 critical risks have no recovery plan").
  - `generated_at`: UTC ISO timestamp of the build.
- `recovery_progress(item)` — percent (1 decimal) or `None` when the
  recovery has no steps; rejects negative counts and `verified > total`.
- `recovery_at_risk(item)` — an `active`/`suspended` recovery whose
  elapsed time exceeds its RTO (both must be known).
- `summarize_dashboard(summary)` — one-line plain-language header, e.g.
  `"critical: 2 critical risks (2 uncovered), 1 active incident (1
  overdue), 3 approvals pending, recovery 62% complete"`.
- Validation is strict on purpose: unknown severity, unknown status,
  negative step counts, and `verified_steps > total_steps` all raise
  `ValueError` — a dashboard must never silently show a wrong number.

## 5. Test list

Slice 1 (implemented, `tests/test_continuity_dashboard_summary.py`):
- empty inputs → `ok`, all zeros, no highlights, `average_progress_percent`
  is `None`.
- risk counts by severity; `uncovered_critical` counts only critical risks
  without a plan; `top` is worst-first and capped at 5.
- incidents: `resolved`/`cancelled` never count as active; overdue counted
  separately; `top` puts overdue first.
- approvals: pending count, expiring-soon within 24 h, own-request count.
- recovery: progress math (including 0 steps → `None`), at-risk past RTO,
  average progress, `closed` counted separately from `active`.
- `overall_status`: `ok` → `attention` → `critical` transitions (critical
  risk, critical active incident, recovery past RTO).
- validation: unknown severity/status, negative steps, `verified > total`
  all raise `ValueError`.
- `summarize_dashboard` produces the expected one-line header for `ok`,
  `attention`, and `critical` summaries.

Still to test (later slices):
- service layer: dashboard endpoint returns the same numbers as the
  underlying continuity/recovery/approval sources for a seeded scope; a
  user outside the scope sees nothing (404) and a member without the
  permission sees 403.
- an out-of-scope risk/incident/approval/recovery never appears in any
  count, list, or highlight — not even as a number.
- frontend: dashboard page renders all four sections + health strip,
  polls, and shows `highlights` first; `npm test` + build + typecheck
  clean.

## 6. What remains for Step AJ

1. **Slice 2 — service layer. ✅ DONE (Agent 63, `10d3e88`)** `app/continuity/dashboard.py::continuity_dashboard_summary` pulls continuity dashboard rows, recovery dashboard rows, the approvals inbox, injected AF/AH risk lists, and the health pass-through; scope-filters each; feeds the slice-1 aggregator. No new tables.
2. **Slice 3 — API. ✅ DONE (Agent 63, `e3cda16`)** `GET /api/continuity/scopes/{scope_id}/dashboard` (scope id == tenant id), continuity scope checks; 404 non-members, 403 non owner/approver/planner roles. Cache-Control `private, no-store`.
3. **Slice 4 — frontend. ✅ DONE (Agent 164)** `ContinuityWorkspace` (Module 08 page) shows the AJ summary at the top: overall-status alert + highlights first, four stat cards, four section cards (top risks, active incidents, recovery in progress, approvals) + health strip; polls every 30 s (see §8); deep links to the existing `#/recovery` and `#/approvals` pages; a 403/404 stops polling and shows a muted note (the Step G dashboard below still works). Types in `frontend/src/continuity/types.ts` (`AjDashboardSummary`), client in `frontend/src/continuity/api.ts` (`bc.ajDashboard`), pure display helpers in `frontend/src/continuity/ajDashboard.mjs` (+ `.d.mts`), tested by `frontend/test/ajDashboard.test.mjs` (6 tests).
4. **Slice 5 — docs + merge.** `assessment/CHANGES-step-AJ.md` (plain language, file list, test counts), full suite green, Alembic check (no migration expected), merge to `main` only when green.

## 9. Slice 4 notes (Agent 164)

- **Refresh interval — 30 s default applied** (Step AF polling, §8). The page re-fetches silently every 30 s; the manual Refresh button also re-fetches. The interval is `AJ_REFRESH_MS` in `frontend/src/continuity/ajDashboard.mjs`.
- **Health strip is `null` on this branch** (Step G `health.py` is not merged here): the page shows a muted "Health roll-up not available yet — it arrives with the Step G merge" line instead of an error. When health arrives, its `status` (`ok` / `degraded` / `down`) renders as an alert.
- **AF alerts / AH predictions** still arrive as injected `RiskInput` lists (slice 2 wiring point); the page renders whatever the API returns, including `source: "alert"` / `"prediction"`.
- **No action buttons that execute anything.** The only buttons are links to the existing detail pages (`#/recovery`, `#/approvals`); risk detail is the resources/plans sections directly below on the same page.
- **Scope isolation is enforced server-side** (slice 2/3); the page never sees out-of-scope rows. A department-limited member sees only their scope's summary because the API scope-filters before aggregating.

## 10. Deviations from the plan (all documented)

- Slice 2/3 (Agent 63): dashboard roles are `owner` / `approver` / `planner` (plan §8 said "owner + department managers" — planner/approver are the managing roles; see §8, still to confirm).
- Slice 4 (Agent 164): the dashboard is a section at the top of the existing Module 08 continuity page, not a separate route — the workspace selector, Step G dashboard and detail sections are already there, and a second page would duplicate them. Deep links use the existing hash routes (`#/recovery`, `#/approvals`).
- Slice 4: a 403/404 from the AJ endpoint stops polling for that workspace (no retry storm) and shows a muted note instead of an error banner, because the Step G dashboard below still works for every role.

## 7. Boundaries (what AJ is not)

- **Step Q (control room):** AJ links to Q for agent/workflow status; it
  does not rebuild the agent registry or run roll-up, and it has no
  pause/resume controls.
- **Step AF (alerts):** AF *decides* when to alert (rules, cooldown,
  quiet hours). AJ only *displays* alert-derived risks; it never fires,
  silences, or re-times an alert.
- **Step AA (scheduled reports):** AA *sends* reports on a schedule. AJ
  is a live screen, not a report and not a delivery channel.
- **Step AH (resilience AI):** AH *predicts* risks and *proposes* recovery
  plans. AJ *displays* those predictions and the progress of the plans;
  it never scores a risk itself and never proposes a plan.
- **Step G (continuity engine):** G *owns* the graph, plans, drills, and
  activation. AJ reads G's data; it never creates a plan, runs a drill,
  or activates anything.
- **Step AL (what-if):** simulations stay in AL; AJ shows real state only
  — a simulated outage never appears as a real incident or risk.

## 8. What Step AJ still needs from the owner

- Which roles may open the dashboard: **applied as owner / approver / planner** (viewer and executor get 403) — **confirm intended** (still open).
- The default refresh interval: **30 s applied** (Step AF polling reference, see §9).
- Whether the dashboard should also be reachable from the Step Q control-room page (proposed: yes, as a linked tab) — still open; the AJ section currently lives on the Module 08 continuity page, with links out to `#/recovery` and `#/approvals`.
