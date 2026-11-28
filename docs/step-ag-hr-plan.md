# Step AG — HR / Employees module plan

Owner-approved 2026-11-28. Leave and attendance managed automatically, salary
reminders on time. Reminders only — the software never pays anyone by itself.

## 1. What exists today (mapped, not rewritten)

| Piece | Where | Reusable for HR |
|---|---|---|
| Identity: user ↔ role ↔ scope, time-bounded, revocable | `app/platform/models.py` `PlatformMembership` | An employee IS a user with a membership; no separate employee identity |
| Company graph member: role owner/approver/editor/viewer + departments | `app/company_graph/models.py` `GraphMember` | Department membership for leave visibility |
| Department manager, scoped to one department, never global admin | `app/db/models.py` `DepartmentManager` | **The natural leave approver** — a manager approves only their own department |
| Employee process knowledge, visible to author + department managers | `app/db/models.py` `EmployeeHandoff` | Pattern for HR records visible to employee + their manager |
| Departments via access groups | `app/api/routes/departments.py` | Leave scope = department |
| Approval machinery: `authorize` / `require`, exact-action approval, single-use, expiry, separation of duties, `execute_once` | `app/platform/` | Leave approvals reuse it exactly like finance |
| Personal tasks + reminders | `app/db/models.py` `PersonalTask`, `PersonalTaskReminder` | Pattern for salary reminders |

## 2. What is missing (everything HR)

- No leave types, policies, balances, or leave-request records anywhere.
- No attendance records (check-in/out, work-from-home, half-day).
- No salary schedule, pay-day, or reminder records.
- No HR permissions: `hr.leave.request`, `hr.leave.approve`,
  `hr.attendance.record`, `hr.salary.remind` do not exist.
- No HR API routes, no HR UI.
- No policy for weekends/holidays: Pakistan working week is Mon–Fri in most
  companies, some work Saturday half-day — weekend days must be configurable
  per org, never hard-coded to one country.

## 3. Design rules (non-negotiable)

1. **Leave approval = finance approval.** A leave request is a workflow run on
   the unified engine (`app/workflows/unified.py`): employee requests
   (`hr.leave.request`), department manager approves (`hr.leave.approve`) with
   an approval bound to the exact action (employee + dates + leave type),
   expiring, single-use. Separation of duties: nobody approves their own
   leave; the requester and the approver must differ
   (`SEPARATED_DUTIES` gains `hr.leave.approve`).
2. **Manager scope is department scope.** A manager sees and approves leave
   only for their own department (`DepartmentManager`); anyone else gets 404.
3. **Balances are computed, never trusted from input.** Used/pending days come
   from approved/pending leave records in the DB; a request carrying a
   "remaining balance" in its payload is ignored.
4. **Salary reminders never move money.** A reminder is a notification
   (WhatsApp/email, Step AA) with pay-day, amount, and payee from the payroll
   record. No auto-pay, no payee change from document or message text.
5. **Attendance is a record, not surveillance.** Check-in/out times, leave
   days, and holidays are facts; no location tracking, no keystroke data.
6. **Every lookup/dispatch through `app/platform`.** `resolve_principal`,
   `authorize`, `require` → 404 non-members, 403 members without permission.
7. **Org time is Asia/Karachi** (`org_today`); leave dates are dates, not
   datetimes; half-day = 0.5 day, never a time range.
8. **No HR data leaves the tenant.** A user sees only their own leave and
   their department's (as manager); HR admin sees the org.

## 4. Data model (slice 3)

- `hr_leave_policies` — org + leave type (`annual`/`sick`/`casual`),
  yearly entitlement days, carry-forward cap, max consecutive days,
  weekend days (e.g. `sat,sun`), accrual (up-front vs pro-rata).
- `hr_leave_balances` — derived per employee per year (entitled + carried −
  used − pending); recomputed from records, stored as a cache only.
- `hr_leave_requests` — employee, leave type, start/end dates, half-day flag,
  status (`pending`/`approved`/`rejected`/`cancelled`), workflow run id,
  approval id. Overlap with an approved/pending request is rejected.
- `hr_attendance` — employee, date, status (`present`/`leave`/`holiday`/
  `work_from_home`), check-in/out times (nullable).
- `hr_payroll_schedules` — employee, pay-day of month, monthly amount,
  currency; `hr_salary_reminders` — generated reminders (due date, sent at,
  channel). Amounts are display-only.
- Migration `0104_hr_leave_attendance.py` (after 0103).

## 5. Slices

1. **(DONE now)** Pure-function leave-balance calculator + salary-reminder
   helper, with tests. No DB change.
2. Models + migration 0104 + `hr.*` permissions + role seeding.
3. Leave request API on the unified engine (request → manager approval →
   balance update via `execute_once`), with tests on a `*_test` DB.
4. Attendance recording API + monthly attendance summary. (DONE,
   Agent 171, `eaf3e67` — 15 tests.)
5. Salary-reminder scheduler (read-only builder, delivery via Step AA).
   (DONE, Agent 171, `cf3aad1` — 12 tests.)
6. HR frontend pages (My leave, Team leave for managers, Attendance,
   Payroll reminders) + `CHANGES-step-AG.md` + full suite / Alembic /
   frontend gates, then merge to main. (DONE, Agent 171 — frontend
   30 passed, tsc + build clean, Alembic check + roundtrip clean;
   full backend suite owed on a quiet VM.)

## 6. Test list

- Business-day counting: weekends skipped, holidays skipped, half-day = 0.5,
  single day, end before start rejected, backdated requests rejected.
- Balance: entitled + carried − used − pending; carry-forward capped;
  pro-rata accrual for mid-year joiners; insufficient balance denied with
  the exact shortfall.
- Max consecutive days enforced per leave type.
- Overlap with an approved or pending request denied.
- Salary reminders: due 3 days before pay-day, on pay-day, overdue flag;
  never generated for a cancelled schedule; amounts are copied, never edited.
- Authorization (slice 3+): non-member 404, member without permission 403,
  manager of another department 404, self-approval rejected.
- Approval binding (slice 3+): approval covers exactly one request; reuse
  and expiry rejected; second approval after cancellation rejected.

## 7. Owner inputs needed

- Leave policy per type (annual/sick/casual entitlement days, carry-forward
  cap) — defaults proposed: annual 20, sick 10, casual 8, carry-forward cap 5.
- Weekend days for the org (default Sat+Sun) and the public-holiday list.
- Pay-day of month and whether salary reminders go by WhatsApp, email, or both.
