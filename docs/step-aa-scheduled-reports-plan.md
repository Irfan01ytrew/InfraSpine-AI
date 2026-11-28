# Step AA — Scheduled Reports + Alerts: Plan

**Status:** slices 1–3 done (Agent 113). **Branch:** `agent/scheduled-reports`.
**Goal:** the software itself tells the owner what matters — daily/weekly reports and
urgent alerts delivered automatically, WhatsApp first, email second.

## 1. What exists today (audited 2026-11-28, HEAD `ca956c2`)

| Piece | State | Where |
|---|---|---|
| On-demand admin usage reports (aggregate-only) | Done | `app/api/routes/admin_reports.py`, `app/services/report_export.py` (CSV/PDF/DOCX/XLSX/PPTX) |
| Report schemas | Done | `app/schemas/reports.py` (`UsageReport`, `DailyUsage`, `QuotaAlert`, …) |
| Reminder workers (approval / personal-task / training) | Done | `app/services/approval_reminders.py`, `personal_task_reminders.py`, `training_reminders.py` — poll loop, SKIP LOCKED claims, escalation, max reminders |
| Email sending (SMTP, verified TLS, content-free alerts) | Done | `app/services/personal_task_email.py` (`send_approval_email`, `deliver_message`) |
| WhatsApp channels (Baileys free + Meta Cloud API, switchable, failover) | Step F, in prep | `app/services/agent_studio_baileys.py`, `agent_studio_whatsapp.py` (Agent 13) |
| Document worker + workflow-engine worker | Done | `app/worker.py`, `app/services/automation_worker.py` |
| Transactional outbox pattern | Done (knowledge index only) | `app/services/outbox.py` |
| Finance data to report on | Done | `app/db/finance_models.py` (`FinInvoice`, `FinPayment`, `FinInvoiceBatch`), `app/finance/service.py` |
| **Scheduled reports / digests / alerts** | **Missing — this step** | — |
| Report schedules, per-user preferences, quiet hours | Missing | — |
| Delivery log / dedupe of sent reports | Missing | — |

## 2. What Step AA adds

### Report templates (v1)
1. **Daily cash summary** — invoices by status (pending / verified / exception /
   approved / paid) with counts + amounts per currency, payments awaiting approval,
   payments dispatched/verified today, overdue items. *(Slice 1: builder done.)*
2. **Weekly invoices** — new, verified, exception, paid this week; top vendors by
   amount; exceptions needing a decision.
3. **Overdue alert** — invoices/payments stuck past a threshold. NOTE: `FinInvoice`
   has **no due-date column**; "overdue" in v1 = `pending`/`exception` older than
   `N` days (default 7) or a payment `awaiting_approval` older than `N` days.
   A `due_date` column is a later slice, not v1.

### Delivery
- **Channels:** WhatsApp first (Step F channels; Baileys ↔ Cloud API failover),
  email second (existing SMTP transport). In-app notification as the always-on
  fallback when neither channel is configured.
- **Read-only by design:** a report is a summary. It never executes a payment,
  never changes an invoice, never approves anything. Any action link in a report
  opens the normal approval screen — the approval itself still needs the exact-action
  approval flow (§5 rule 4).
- **Dedupe:** one delivery record per (schedule, period, channel, recipient);
  the worker claims due schedules with SKIP LOCKED so two workers never send twice.
- **Quiet hours:** per-schedule `quiet_start`/`quiet_end` (org timezone, default
  22:00–07:00 Asia/Karachi). Non-urgent reports wait; only the overdue alert may
  break quiet hours, and only when the owner explicitly enables it.
- **Per-user preferences:** each recipient chooses templates + channels + cadence;
  defaults are owner-only, daily cash summary by WhatsApp at 08:00.
- **Permission filtering:** the builder receives only rows the recipient may read
  (`finance.read`, group-scoped). The builder itself never queries the DB and never
  widens visibility. Amounts are shown per currency, never converted (no invented
  exchange rates).

### Safety rules (from §5)
- Report text is **untrusted data** to any downstream consumer: it can never grant
  permissions, change payees, or change what an approval covers.
- No secrets in report bodies or logs (no account numbers, no tokens).
- All sends are audited (`reports.sent` with schedule id, template, channel,
  recipient, period) and visible in admin reports.

## 3. Slices

1. ✅ **Slice 1 — schedule model + daily cash summary builder (pure functions).**
   `app/reports/scheduled.py`: `ReportSchedule`, `next_due`, `is_quiet_hour`,
   `should_send`, `build_daily_cash_summary`, `render_daily_cash_summary_text`,
   `validate_schedule`. Tests: `tests/unit/test_scheduled_reports.py`.
2. ✅ **Slice 2 — persistence + API (Agent 113).** Alembic migration `0104_scheduled_reports`:
   `report_schedules`, `report_deliveries`, `report_preferences` tables; CRUD API under
   `app/platform` authorization (`reports.manage` admin-only, `reports.view` to read
   own deliveries); Alembic roundtrip + check clean. **Fix found in review:** the
   `reports.admin`/`reports.viewer` roles were only created by the tests — no code
   path created them (no role-creation endpoint; legacy admins not bridged for
   these permissions), so every request would 403 in production. The roles are now
   seeded once at API startup (lifespan, idempotent); regression test
   `test_report_roles_exist_from_startup_without_explicit_ensure` fails without it
   (`NotFoundError: Role not found`). Tests: 7 API tests pass.
3. ✅ **Slice 3 — worker (Agent 113).** Due-schedule poller (reuse the approval-reminder worker
   pattern: poll, SKIP LOCKED claim, build via `FinanceService`, send, record
   delivery, retry with backoff). Runs inside the existing worker process.
   `in_app` always delivered; WhatsApp/email go through the `deliver` callback
   (slice 4) and until then record `retry`/`channel_not_configured`, never dropped.
   Tests: 8 worker tests pass.
4. ✅ **Slice 4 — delivery channels (Agent 130).** WhatsApp send via Step F
   service (Meta Cloud API only, `send_outbound_text` wrapper, allow-list +
   24h window checked, `dispatch_once` exactly-once key
   `scheduled-report:{delivery.id}`), email via `personal_task_email`
   (`deliver_message`, gated by `scheduled_report_email_enabled`, default
   `False`, deterministic Message-ID as provider ref); migration
   `0105_scheduled_reports_provider_ref` (nullable `provider_ref` on
   `report_deliveries`); worker `deliver` contract now takes the delivery
   row first and stamps `provider_ref`; `app/worker.py` wired. Tests: 11
   delivery tests pass (11 passed in 1982.52s).
5. ✅ **Slice 5 — weekly invoices + overdue alert templates + frontend settings page
   (Agent 130).** Weekly (new = created in the week, current status; top-5
   vendors; all current exceptions) + overdue alert (v1 definition) builders
   and renders in `app/reports/scheduled.py`; worker branches per template
   (weekly = seven days ending on the firing day; sends only when there is
   something to say); frontend `src/reports/` (api client, pure view
   helpers + `.d.mts`, `ScheduledReportsSection.tsx`) wired as a "Reports"
   tab in `SettingsPage.tsx`. Tests: 10 new unit tests (44 in the file),
   2 new worker tests (file 9/9), frontend 26 passed, tsc + build clean;
   Alembic roundtrip clean (head 0105). **Step AA is code-complete — no
   successor slices remain.** See `assessment/CHANGES-step-AA.md`.

## 4. Test list
- Unit (slice 1, done): scheduling math (daily/weekly, timezone, DST-safe via
  `zoneinfo`), quiet hours incl. overnight windows, due/quiet/enabled gating,
  builder totals per status + currency, overdue threshold, empty input, text render,
  schedule validation.
- Slice 2: migration roundtrip, API authz (non-admin 403, non-member 404),
  preference scoping.
- Slice 3: worker claims each due schedule once, retries transient send failures,
  skips quiet hours, records deliveries.
- Slice 4: WhatsApp/email sends mocked; no duplicate delivery for same period;
  content-free failure paths.
- Slice 5: template totals on seeded finance data (throwaway `*_test` DB only);
  frontend tests + build + typecheck.

## 5. Open questions for the owner
- Default daily summary time (proposed 08:00 Asia/Karachi) and quiet hours.
- Should the overdue alert break quiet hours? (Proposed: no, unless enabled.)
- WhatsApp number / email for report delivery (Step F credentials still needed).
