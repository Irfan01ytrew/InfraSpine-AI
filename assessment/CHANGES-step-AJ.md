# Step AJ — Business continuity dashboard: what changed

> Branch `agent/continuity-dashboard`, HEAD `1ff3413` (slice 4) + this doc (slice 5).
> Base: `step-e-gaps-wip` `ca956c2`. No migration in this step; Alembic head stays
> `0103_unified_studio_governance`.

## In plain language

One screen on the Module 08 (Business continuity) page now answers, at a glance,
"is the company safe right now, and what needs me?":

- **Open risks** — uncovered critical resources and continuity gaps, worst first,
  with a "No plan" flag where no recovery plan covers the risk.
- **Active incidents** — recovery incidents that are not resolved or cancelled,
  with overdue-SLA flags.
- **Pending approvals** — every approval the viewer may decide, with
  expiring-soon flags.
- **Recovery progress** — every continuity run / recovery in flight, with
  verified-steps progress and past-RTO flags.
- **Health strip** — the Step G health roll-up (`ok` / `degraded` / `down`),
  passed through, not recomputed. It shows "not available yet" until the
  Step G merge supplies it.

The dashboard is **read-only**. It never executes an action, never dispatches
a workflow, never decides an approval. Its only buttons are links to the
existing detail pages (recovery incidents, approvals inbox); risk detail is
the resources and plans sections directly below it on the same page.

A department-limited member sees only their own scope: every section is
filtered to the viewer's scope before it is counted. A risk, incident,
approval, or recovery outside the viewer's departments never appears in a
count, a list, or a highlight — not even as a number. Non-members get 404
(the scope is not revealed); members whose role may not open the dashboard
(owner / approver / planner only) get 403.

## What was built, slice by slice

### Slice 1 — pure-function aggregator (Agent 46)
`app/continuity/dashboard_summary.py` — standard library only, no I/O.
`build_dashboard_summary()` rolls plain risk / incident / approval / recovery
inputs into one summary: overall status (`ok` / `attention` / `critical`),
counts by severity, worst-first top-5 lists, expiring-soon approvals,
recovery progress and RTO risk, and plain-language highlights (capped at 6).
Strict validation: unknown severity/status, negative step counts, and
`verified_steps > total_steps` all raise `ValueError` — a dashboard must
never silently show a wrong number.

### Slice 2 — service layer (Agent 63, `10d3e88`)
`app/continuity/dashboard.py::continuity_dashboard_summary()` wires the real
sources into the slice-1 aggregator: continuity gaps become risks (severity
by gap code), pending plans/runs become approvals (deciding roles only),
continuity runs become recovery items with verified/total steps and RTO,
recovery incidents come from the viewer's own recovery workspace, and the
platform approvals inbox is filtered per row exactly like the inbox itself.
Step AF alerts / Step AH predictions arrive as injected `RiskInput` lists
(the wiring point is here; the merge that lands AF/AH passes their output
in). Health is passed through unchanged (`None` until Step G merges).
No new tables.

### Slice 3 — API (Agent 63, `e3cda16`)
`GET /api/continuity/scopes/{scope_id}/dashboard` in
`app/continuity/routes.py` (scope id == tenant id). 404 for non-members and
unknown scopes, 403 for members whose role is not owner / approver /
planner. `Cache-Control: private, no-store`. Read-only: no writes, no
dispatches, no decisions.

### Slice 4 — frontend page (Agent 164, `1ff3413`)
The Module 08 continuity page (`ContinuityWorkspace` in
`frontend/src/components/ModuleWorkspaces.tsx`) shows the AJ summary at the
top, above the existing Step G dashboard:

- Overall-status alert with the highlights list first, plus "Updated … ·
  refreshes every 30 seconds".
- Four stat cards: open risks, active incidents, approvals pending,
  recovery progress.
- Four section cards: top risks (severity badge + "No plan" flag), active
  incidents (overdue flag, link to `#/recovery`), recovery in progress
  (progress bar + "Past RTO" flag), approvals (pending / expiring within
  24 h / requested by you, link to `#/approvals`).
- Health strip: the Step G roll-up when present; a muted "not available
  yet" line while it is `null`.
- Polls every 30 s (Step AF default, `AJ_REFRESH_MS` in
  `frontend/src/continuity/ajDashboard.mjs`). A 403/404 stops polling and
  shows a muted note — the Step G dashboard below still works for every
  role. Other failures show once and keep polling silently.

New frontend files: `frontend/src/continuity/ajDashboard.mjs` (pure display
helpers: status/severity tones, progress label, relative "updated … ago") +
`ajDashboard.d.mts`, tested by `frontend/test/ajDashboard.test.mjs`.
Types in `frontend/src/continuity/types.ts` (`AjDashboardSummary`);
client in `frontend/src/continuity/api.ts` (`bc.ajDashboard`).

The dashboard is a section at the top of the existing Module 08 page, not
a separate route — the workspace selector, Step G dashboard and detail
sections are already there, and a second page would duplicate them.

## Files changed (whole step)

| File | Change |
|---|---|
| `app/continuity/dashboard_summary.py` | Slice 1 aggregator (new) |
| `app/continuity/dashboard.py` | Slice 2 service layer (new) |
| `app/continuity/routes.py` | Slice 3 API route (1 route added) |
| `docs/step-aj-continuity-dashboard-plan.md` | Plan, updated through slice 4 |
| `frontend/src/continuity/types.ts` | `AjDashboardSummary` types |
| `frontend/src/continuity/api.ts` | `bc.ajDashboard` client |
| `frontend/src/continuity/ajDashboard.mjs` + `.d.mts` | Slice 4 display helpers (new) |
| `frontend/test/ajDashboard.test.mjs` | Slice 4 tests (new, 6 tests) |
| `frontend/src/components/ModuleWorkspaces.tsx` | `AjDashboardSection` + polling in `ContinuityWorkspace` |
| `assessment/CHANGES-step-AJ.md` | This file (slice 5) |

## Tests and gates

- Slice 1 pure aggregator: **32 passed, 0 failed** (`tests/test_continuity_dashboard_summary.py`,
  re-verified by Agent 164 in 0.45 s, `--noconftest`).
- Slices 2–3 service + API: **8 new tests passed + 47 regressions passed**
  (Agent 63, on throwaway `*_test` DBs via `scripts/irfanix_postgres_tests.py`;
  32 slice-1 + continuity, 15 recovery). Honest caveat: the combined run was
  OOM-killed in teardown after the last test (exit 137) — the evidence is
  dots, not a summary; one recovery test flaked once under load
  (`BrokenPipeError`) and passed on a clean single re-run.
- Slice 4 frontend: **6 new tests passed** (`frontend/test/ajDashboard.test.mjs`);
  full frontend suite **28 passed, 0 failed** (22 prior + 6 new);
  `tsc --noEmit` clean; `vite build` emitted a complete `dist/`
  (`index.html` + 112 assets, AJ strings verified in the emitted
  `ModuleWorkspaces` chunk). Note: the vite process itself was OOM-killed
  after writing (VM load 44, free RAM ~134 MB) — no build error, no
  missing output.
- Alembic: single head `0103_unified_studio_governance`; `alembic upgrade
  head` + `alembic check` clean ("No new upgrade operations detected") on
  throwaway DB `infraspine_aj_alembic` (dropped afterwards). No migration
  in this step.

## Still owed (not blocking this step's code, blocking the merge)

- **Quiet-VM full-file re-run** of `tests/test_continuity_dashboard_service.py`
  plus the slice-1 / recovery regressions in one process, producing a
  summary (Agent 63's runs never produced one). Not attempted by Agent 164:
  the Step E fifth full-suite pass was running (load control), then the VM
  rebooted (see below).
- Full backend suite green before merge (standard gate).

## Environment notes (Agent 164)

- The VM rebooted mid-task (~07:14 UTC, 2026-11-28): system PostgreSQL
  binaries, `/usr/share/postgresql/16`, and `/home/hatch/tmp-pg-dist` were
  wiped; the Step E fifth-pass runner (PID 19118) died and had not been
  relaunched at last check. The AJ cluster (`/home/hatch/.pg-aj/cluster`,
  port 55438) restarted ~07:16:52 UTC (PID 3378) from the pre-reboot
  `tmp-pg-dist` binary path — that path is now gone from disk, so the
  cluster **cannot be restarted from it again**. If it stops, reinstall
  `postgresql-16` from the apt cache (`dpkg -i
  /var/cache/apt/archives/postgresql-16_*.deb`, as Agent 124 did) or use
  `~/pg16-dist/bin` together with the restored `/usr/share/postgresql/16`
  (the `pg16-dist` copy alone fails with a missing `timezonesets` error).
  Test DBs: `infraspine_aj` / `infraspine_aj_test` (trust auth as
  `postgres`, port 55438).

## Owner decisions still open

- Dashboard roles are owner / approver / planner (viewer and executor get
  403) — **confirm intended**.
- Refresh interval: **30 s applied** (Step AF polling default).
- Whether the dashboard should also be reachable from the Step Q
  control-room page as a linked tab — still open.

## Merge notes

- No migration collision (no migration in this step).
- `frontend/src/components/ModuleWorkspaces.tsx` is also edited by the
  frontend redesign round 8 (Agent 106) and Step AH (Agent 82) — reconcile
  at merge.
- Step AF alerts / Step AH predictions: the merge that lands them passes
  their output into `continuity_dashboard_summary()` as `RiskInput` lists;
  nothing else changes. Step G health: the merge that lands
  `app/continuity/health.py` passes its roll-up as `health`; the frontend
  already renders it.
- Step AJ is code-complete (slices 1–5). No successor slices remain.
