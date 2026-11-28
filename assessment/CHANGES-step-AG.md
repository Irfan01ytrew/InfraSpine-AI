# CHANGES — Step AG: HR / Employees module

> Branch `agent/hr-module`, base `step-e-gaps-wip` (`ca956c2`).
> Status: slices 1–6 done. All focused gates green (see "Test evidence").
> Merge only after the Step E verification tally is green, and renumber
> migration `0104` at merge (see "Merge notes").

## What this step does (plain language)

The software now manages leave, attendance and salary reminders for every
department — and never pays anyone by itself.

- **Leave requests with a real approval.** An employee requests leave
  (annual, sick or casual) for dates; their department manager approves or
  rejects it. The approval is bound to the exact request — employee, leave
  type and dates — expires, is single-use, and nobody approves their own
  leave. Once approved, the manager applies it and the balance is updated
  exactly once, even if the apply button is pressed twice.
- **Balances are computed, never typed in.** Entitled days come from the
  department's leave policy (annual 20, sick 10, casual 8 by default,
  carry-forward capped at 5); used and pending days come from the leave
  records themselves. A request that would exceed the balance is refused
  with the exact shortfall.
- **Attendance, recorded not surveilled.** An employee records their own
  day (present, work from home, leave, holiday) with optional check-in and
  check-out times; a manager records for a member of their own department.
  One row per employee per day — recording the same day again corrects it.
  The monthly summary counts business days only (weekend days from the
  department policy, default Saturday + Sunday). A past business day with
  neither a record nor approved leave is shown as "unrecorded" — never as
  "absent". Future days are never counted as missing.
- **Salary reminders, never payments.** Each employee has a payroll
  schedule (pay-day of the month, amount, currency). A scheduler builds
  reminder rows three days before pay-day, on the day, and flags them
  overdue after it. Running the cycle twice never duplicates a reminder.
  A reminder is only a notification — it moves no money and changes no
  payee. Delivery (WhatsApp or email) is Step AA scheduled-report delivery,
  which is not on this branch yet: rows are created unsent and the HR page
  shows them as "pending delivery" until AA's delivery worker stamps them
  sent.
- **HR pages.** "HR & leave" in the sidebar, with four tabs: My leave
  (request form, balances per leave type, my requests with Cancel), Team
  leave (managers: requests awaiting their decision with Approve / Reject,
  and Apply for approved ones), Attendance (month picker, per-employee
  summary, record form), Payroll reminders (schedules, reminders with
  delivery state, Run reminder cycle for managers).

## The problem it fixes

Before this step there was no HR anywhere in the software: no leave types,
policies or balances, no attendance records, no salary schedules, no
`hr.*` permissions, no HR API and no HR page. Leave was tracked outside
the system (or not at all), so balances were whatever someone remembered,
and salary reminders depended on someone remembering the pay-day. Step AG
puts all three on the same platform rules as finance: scoped visibility,
exact-action approvals, single-use effects, and an audit trail.

## Files changed (vs `ca956c2`)

### Plan (Agent 40, `d03c860`)
- `docs/step-ag-hr-plan.md` (new) — the rules, safety guarantees and
  slices. The plan is the authority for this step.

### Slice 1 — pure functions (Agent 40, `f43ca68`)
- `app/hr/leave.py` (new) — `count_business_days()`, balance computation
  (entitled + carried − used − pending, carry-forward capped, pro-rata
  accrual, half-day = 0.5).
- `app/hr/reminders.py` (new) — pure `salary_reminders()` helper (upcoming
  3 days before pay-day, due on the day, overdue after; pay-day clamped
  to the month length).

### Slice 2 — models + migration + permissions (Agent 44, `e3762ec`)
- `app/hr/models.py` (new) — `HrLeavePolicy`, `HrLeaveBalance`,
  `HrLeaveRequest`, `HrAttendance`, `HrPayrollSchedule`,
  `HrSalaryReminder`.
- `migrations/versions/0104_hr_leave_attendance.py` (new) — creates the
  six HR tables, down revision `0103`.
- `app/hr/permissions.py` (new) — `hr.leave.request`, `hr.leave.approve`,
  `hr.attendance.record`, `hr.salary.remind`, `hr.manage`;
  `hr.employee` / `hr.manager` / `hr.admin` roles; `hr.leave.approve`
  added to `SEPARATED_DUTIES`.

### Slice 3 — leave-request API (Agent 44, `ea9b44f`)
- `app/hr/service.py` (new) — leave request / decide / execute / cancel,
  balance computation with read-back, manager scoping.
- `app/hr/workflow.py` (new) — leave approval + execute handlers on the
  unified engine.
- `app/api/routes/hr.py` (new) — `POST /api/hr/leave`, `GET /api/hr/leave`,
  `GET /api/hr/leave/team`, `GET /api/hr/leave/{id}`,
  `POST /api/hr/leave/{id}/decide|execute|cancel`,
  `GET /api/hr/leave/{id}/balance`.

### Slice 4 — attendance API + summary (Agent 171, `eaf3e67`)
- `app/hr/service.py` — `record_attendance()`, `list_attendance()`,
  `attendance_summary()` (business days only; approved **or applied**
  leave counts; weekend rows listed, not tallied).
- `app/api/routes/hr.py` — `POST /api/hr/attendance`,
  `GET /api/hr/attendance`, `GET /api/hr/attendance/summary`.
- `tests/test_hr_attendance.py` (new) — 15 tests.
- `migrations/env.py` — registers `app.hr.models` on the Alembic
  metadata. Without this line `alembic check` wanted to drop all six HR
  tables: they existed in the database but were invisible to Alembic.
- This commit's shared service/route files also carry the slice-5
  payroll methods and the slice-6 page-support endpoints (below); those
  are exercised by the slice-5 and slice-6 commits' tests.

### Slice 5 — salary-reminder scheduler (Agent 171, `cf3aad1`)
- `app/hr/scheduler.py` (new) — `run_salary_reminder_cycle()`; the
  unique `(payroll_schedule_id, pay_date, kind)` key makes a re-run a
  no-op. Amounts are copied from the schedule, never edited.
- `app/services/hr_salary_reminders.py` (new) — `process_salary_reminders()`
  (for `worker.py --once`) and `run_salary_reminders()` (hourly daemon
  poll), mirroring `personal_task_reminders`.
- `app/worker.py` — hooks both into `--once` and the daemon loop.
- `app/hr/service.py` + `app/api/routes/hr.py` — payroll-schedule
  create (upsert) / list / mine / deactivate and salary-reminder list /
  run endpoints.
- `tests/test_hr_salary_reminders.py` (new) — 12 tests.

### Slice 6 — HR frontend + this document (Agent 171, this commit)
- `frontend/src/hr/api.ts` (new) — `hrApi` client for every HR endpoint.
- `frontend/src/hr/hrView.mjs` + `hrView.d.mts` (new) — pure page
  helpers (month label, leave counts, balance text, attendance totals,
  reminder ordering, permission checks).
- `frontend/src/hr/HrPage.tsx` (new) — My leave / Team leave /
  Attendance / Payroll reminders tabs.
- `frontend/test/hrView.test.mjs` (new) — 8 tests.
- `frontend/src/components/AdminConsole.tsx` — lazy `HrPage`, `"hr"`
  view, `#/hr` hash route.
- `frontend/src/components/AppSidebar.tsx` — "HR & leave" entry in
  Operations.
- `app/hr/service.py` + `app/api/routes/hr.py` — page-support endpoints:
  `GET /api/hr/scopes` (departments where the caller holds any HR
  permission), `GET /api/hr/leave/approvals` (pending leave approvals
  with the action hash the manager must present),
  `GET /api/hr/leave/balance` (the caller's own balance per leave type).
- `docs/step-ag-hr-plan.md` — slices 4–6 marked done.

## Test evidence

All runs on throwaway `*_test` databases (cluster
`/home/hatch/.hr44-pg`, port 55441), never the live database. No full
backend suite was run — it is owed on a quiet VM (load control while the
Step E verification pass runs).

- Slice 1 pure: 23 passed (`tests/test_hr_leave_balance.py
  --noconftest`), re-verified by Agent 171.
- Slice 2 migration: 1 passed; slice 3 leave API: 20 passed (chunked
  4×5); slice-1/3 regressions: 25 passed (Agent 44). Total slices 1–3:
  69 passed, 0 failed.
- Slice 4 attendance: 15 passed (`tests/test_hr_attendance.py`,
  chunks 5 + 5 + 3 + 2). The last two ran after two fixes the tests
  caught (see "Bugs found").
- Slice 5 salary reminders: 12 passed
  (`tests/test_hr_salary_reminders.py`, chunks 6 + 6).
- Slice 6 frontend: `npm test` 30 passed, 0 failed (7 files, including
  the 8 new `hrView` tests); `tsc --noEmit` clean; `vite build` clean
  (built in 6.82s, `NODE_OPTIONS=--max-old-space-size=1024`).
- Regression note: the slice-4 summary tests exercise the full slice-3
  flow end to end (request → manager approve → execute → balance
  applied), so the leave pipeline is covered by the new tests too.
- Alembic: `alembic check` clean ("No new upgrade operations
  detected") against the test database at head `0104`; roundtrip green
  on the test database (downgrade `0104 → 0103`, HR tables 0; upgrade
  `0103 → 0104`, HR tables 6 back; check clean). The application
  database in this worktree was never migrated (no `alembic_version`
  table) — pre-existing; `alembic check` against it fails for that
  reason alone.
- Step AG total new tests: 27 (15 attendance + 12 salary reminders)
  plus 8 frontend helper tests.

## Bugs found

- **Summary missed executed leave (product bug, slice 4).** The
  monthly summary counted leave requests with status `approved` only.
  A request the manager approved *and applied* has status `applied`,
  so its days vanished from the summary (`leave` 0 instead of 3).
  Fixed: the summary counts statuses `approved` **and** `applied`.
  Regression test added: approved-but-not-yet-executed leave still
  counts too.
- **Leave test dates straddled a month (test bug, slice 4).** The
  summary tests used "next Monday–Wednesday", which crosses a month
  boundary when the next Monday is within two days of month-end (on
  Sat 2026-11-28 the block was Nov 30 + Dec 1–2, so the November
  summary could only ever show 1 leave day, not 3). Fixed with a
  first-Monday-of-next-month helper; the block now always sits inside
  one month.
- **HR tables invisible to Alembic (slice-2 gap, found in slice 6
  gates).** `migrations/env.py` never imported `app.hr.models`, so
  `alembic check` reported all six HR tables as extra and wanted to
  drop them. Fixed with one import line; check is clean.
- **Balance route took a request id, not an employee id (slice-6
  design catch).** `GET /api/hr/leave/{id}/balance` loads a leave
  request and returns *its* balance. The page needs the caller's own
  balance per leave type, so slice 6 added
  `GET /api/hr/leave/balance?group_id&year&leave_type` instead of
  misusing the request route.
- **Table component has no `headers` prop (slice 6).** The HR page
  first used `<Table headers={...}>`; this branch's `Table` is a plain
  `<table>` wrapper, so all six tables were rewritten with explicit
  `<thead>` / `<tbody>`, as `FinancePage` does. Caught by `tsc`.

## Boundaries and honest limits

- **Salary reminders are created unsent.** `sent_at` is null and
  `channel` is null until Step AA's scheduled-report delivery claims
  the row. The HR page labels these "pending delivery". No WhatsApp
  or email is sent from this step.
- **No public-holiday table exists yet.** Holidays in the summary come
  only from attendance rows marked `holiday`. The owner still owes a
  holiday list (plan §7).
- **No push.** Reminders are rows in the
  database plus a worker cycle; there is no push notification and no
  automatic payroll action of any kind.
- **Pay-day clamping.** A pay-day of 31 in February becomes Feb 28
  (last day of the month), tested.
- **Half-day leave** counts 0.5 day in balances and in the summary
  (via the leave-request `days` value, which the summary expands to
  whole calendar days — a half-day request still occupies its calendar
  day in the summary; the balance is where the 0.5 shows).

## Merge notes

- Renumber migration `0104_hr_leave_attendance` at merge (down revision
  `0103`). `0104` is also minted by HR, WhatsApp AE, AI freshness, AB
  enrichment, G continuity, AA scheduled reports, AM foundry, Z language
  preference, R finance autopilot, AF dispatch, AL rehearsals, O voice,
  J workforce, F path setting, W self-healing and Y anomaly settings.
- No other file conflicts are known from this branch: `ModuleWorkspaces.tsx`
  is untouched; `frontend/src/components/AdminConsole.tsx` gains one lazy
  page + one view branch; `AppSidebar.tsx` gains one nav entry.
- Merge only after the Step E verification tally is green (the branch
  base is `step-e-gaps-wip` `ca956c2`).
- Full backend suite still owed on a quiet VM before merge (load
  control: the Step E verification pass was running throughout this
  step, so only focused tests ran, in small chunks).

## Owner decisions still open (plan §7)

- Leave policy per type — defaults implemented: annual 20, sick 10,
  casual 8 days; carry-forward cap 5.
- Weekend days — default Saturday + Sunday, configurable per
  department policy.
- Public-holiday list — not yet provided; see "Boundaries" above.
- Pay-day of month — per-employee schedule, default 25 in the page
  form; no org-wide default yet.
- Reminder channel (WhatsApp, email, or both) — not yet decided;
  delivery is Step AA's job and it takes the channel from its own
  settings when it lands.
