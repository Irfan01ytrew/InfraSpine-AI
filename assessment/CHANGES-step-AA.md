# CHANGES — Step AA: Scheduled reports + alerts

Plain-language record of what Step AA added, how it was tested, and what is
still open. Step AA lets an admin schedule finance reports (daily cash
summary, weekly invoices, overdue alert) that are built only from rows the
recipient is allowed to read, and sent on WhatsApp, email, or in-app — never
twice for the same schedule and period, never dropped silently.

Branch: `agent/scheduled-reports`. Base: `ca956c2` (merged Step E gaps).

## What was built

### Slice 1 — schedule model + daily cash summary (Agent 33)
- `app/reports/scheduled.py` — pure schedule math (daily/weekly, timezone and
  DST-safe via `zoneinfo`), quiet hours including overnight windows, due /
  quiet / enabled gating, daily cash summary builder (totals per status and
  currency), overdue threshold, text rendering, schedule validation.
- Tests: 34 passed (`tests/unit/test_scheduled_reports.py`).

### Slice 2 — persistence + API (Agent 113)
- Migration `0104_scheduled_reports` — new tables `report_schedules`,
  `report_deliveries`, `report_preferences`. A delivery is unique per
  schedule / period / channel / recipient, so the same report can never be
  recorded twice.
- `app/db/report_schedule_models.py`, `app/reports/persistence.py`,
  `app/api/routes/scheduled_reports.py` — CRUD API under `app/platform`
  (`reports.manage` admin-only; `reports.view` reads only the caller's own
  deliveries; non-member 404, member without permission 403).
- **Real bug found and fixed:** the `reports.admin` / `reports.viewer` roles
  were never created in production, so every request failed with
  `NotFoundError: Role not found`. The roles are now seeded once at API
  startup (idempotent, following the `rebind_legacy_credentials` pattern);
  a failing-then-passing regression test proves the fix is genuine.
- Tests: 7 API tests passed.

### Slice 3 — worker poller (Agent 113)
- `app/services/scheduled_report_worker.py` — due-schedule poller (SKIP
  LOCKED claim, so two workers never build the same report), builds via
  `FinanceService` on only `finance.read`-visible rows, sends, records the
  delivery, retries with backoff (15 minutes doubling, 6-hour cap, 8 attempts).
  `in_app` is always delivered. WhatsApp/email go through the `deliver`
  callback (slice 4); until a channel is configured they record
  `retry` / `channel_not_configured` and are never dropped.
- Tests: 8 worker tests passed, 4 worker regression tests passed.

### Slice 4 — delivery channels (Agent 130)
- `app/services/scheduled_report_delivery.py` — `deliver_report(delivery,
  schedule_row, recipient, subject, body, ...)` plus `make_report_deliver`
  (the worker callback). Typed errors for every failure mode
  (`WhatsAppNotConfigured`, `WhatsAppNumberUnknown`, `WhatsAppNotAllowed`,
  `WhatsAppWindowExpired`, `WhatsAppSendFailed`, `EmailNotConfigured`).
- WhatsApp (Meta Cloud API only, via Step F): a new public wrapper
  `send_outbound_text` in `app/services/agent_studio_whatsapp.py` (thin
  wrapper over the private `_send_text`). The sender is the connection owned
  by the schedule creator; the recipient's `wa_id` comes from
  `AgentStudioWhatsAppContact` (only a number that already messaged the
  business can receive); the allow-list is checked explicitly; the 24-hour
  messaging window is checked against the latest inbound message from that
  `wa_id` (expired window is a typed error, retried, then failed — template
  messages for expired windows are a later slice and were never invented).
  Sends go through `dispatch_once` (duty `channel.reply`, key
  `scheduled-report:{delivery.id}`), so a crash after sending replays the
  stored provider id instead of sending twice. **No Baileys failover in v1**
  (the bridge only sends replies to inbound senders).
- Email: shared SMTP `deliver_message` from `app/services/personal_task_email.py`
  (via `asyncio.to_thread`), gated by a new setting
  `scheduled_report_email_enabled` (default `False`, added to
  `app/core/config.py` and its SMTP-required validator). Deterministic
  Message-ID `<scheduled-report-{delivery.id}@infraspine.local>` is returned
  as the provider ref. Bodies carry a safety footer ("never moves money or
  approves anything").
- Migration `0105_scheduled_reports_provider_ref` (down_revision
  `0104_scheduled_reports`) — adds nullable `provider_ref String(200)` to
  `report_deliveries`; the model field and the API's `_delivery_out` now
  include it.
- Worker contract change (deliberate): `deliver` is now
  `deliver(delivery, schedule_row, recipient, subject, body)` — the delivery
  row first, so the callback can use the delivery id as its exactly-once
  key. The worker stamps `delivery.provider_ref` on success. The delivery
  module opens its own session for WhatsApp sends (`execute_once`
  commits/rollbacks internally and must not share the worker's session).
- `app/worker.py` — `--once` and daemon paths pass the real `deliver`
  callback (`_report_deliver` helper, deferred imports; a broken cipher fails
  closed to a typed WhatsApp error while email/in-app still work).
- Tests: 11 new tests in `tests/test_scheduled_report_delivery.py`
  (WhatsApp happy path, exactly-once across retry — same delivery sent twice
  calls the sender once and leaves one `PlatformActionExecution` row,
  no-connection, no-contact, not-on-allow-list, expired window, email happy
  path, email disabled, duplicate delivery row rejected by the DB unique
  constraint, Message-ID stability, error-type distinctness).
  **11 passed in 1982.52s (0:33:02), RC=0** (`tests/test_scheduled_report_delivery.py -q`).

### Slice 5 — weekly + overdue templates + frontend (Agent 130)
- `app/reports/scheduled.py` — `InvoiceRow` gained optional `created_at`,
  `invoice_ref`, `vendor_name`; `PaymentRow` gained `vendor_name`. New
  `build_weekly_invoices_summary` / `render_weekly_invoices_text` and
  `build_overdue_alert` / `render_overdue_alert_text`.
  - Weekly: "new" means an invoice created in the week (by `created_at`,
    falling back to `invoice_date`), grouped by its CURRENT status — there
    is no status-change timestamp, so "verified this week" only means
    "created this week and currently verified". Top vendors top 5 by amount;
    exceptions needing a decision are ALL current exceptions of any age,
    capped at 10 listed. Payments verified in the week (by `verified_at`).
  - Overdue (v1 plan definition): pending/exception invoice or
    awaiting-approval payment older than `overdue_days`; oldest first,
    capped at 20 listed plus a "+N more" line; totals for all.
  - Renders are capped and contain no links or account numbers.
- Worker: `_subject_for` per template; `_build_body` branches per template
  (weekly covers the seven days ending on the firing day; weekly sends only
  if there is anything new or any exception; overdue sends only if something
  is overdue; daily unchanged). Vendor names resolved via one `FinVendor`
  query per call. Unknown templates are still never invented — the guard now
  rejects templates outside `TEMPLATES` instead of rejecting everything
  that is not the daily summary.
- **Real bug found and fixed during slice 5:** the weekly window was first
  derived as [firing day, firing day + 6 days], which looks into the future
  and misses the week that just ended. Corrected to [firing day − 6 days,
  firing day]; the worker test pins the window to 2026-11-22…2026-11-28.
- Frontend: `frontend/src/reports/api.ts` (reportsApi client),
  `frontend/src/reports/scheduledReportsView.mjs` (pure view helpers),
  `frontend/test/scheduledReportsView.test.mjs` (4 node tests passed),
  `frontend/src/reports/ScheduledReportsSection.tsx` (schedule list + create
  form with template/cadence/time/weekday/channels/recipients/quiet hours/
  overdue days, per-user preferences, delivery history with status filter),
  wired as a "Reports" tab in `frontend/src/components/SettingsPage.tsx`
  (alongside Organization / Models / Documents / Media / Index / Imports /
  Finance).
- Tests: 10 new unit tests in `tests/unit/test_scheduled_reports.py`
  (44 passed in that file: 34 slice-1 + 10 new, 5.88s, `--noconftest`). 2 new worker tests
  (weekly sends with totals; overdue sends only when something is overdue,
  and sends nothing past 30 days) in `tests/test_scheduled_report_worker.py`:
  the file run gave **8 passed, 1 failed in 1916.93s** — the weekly test
  passed; the overdue test failed twice on test bugs (see "Problems found"
  4–5 below), was fixed, and **passed standalone in 443.35s (RC=0)**. Worker
  file total: **9/9 passing** across the two runs. Slice-2/3 API regressions:
  **7 passed in 1311.20s (0:21:51), RC=0** (`tests/test_scheduled_reports_api.py -q`;
  the first attempt was OOM-killed after 3 passed, RC=137 — environmental,
  no code failure).

## Gates
- Alembic: **roundtrip clean on the final branch** — upgrade head RC=0
  (head `0105_scheduled_reports_provider_ref`), downgrade base RC=0,
  upgrade head RC=0, `alembic check` RC=0 ("No new upgrade operations
  detected"), on throwaway DB `infraspine_aa130_roundtrip` (cluster port
  55497).
- Frontend: **npm test 26 passed, 0 failed; typecheck RC=0; vite build
  clean (7.29s)** on the final tree (node helper tests 4 passed are included
  in the 26). Note: `npm ci` was OOM-killed three times (RC=137), so
  `node_modules` was copied from the frontend-redesign worktree instead —
  its `package-lock.json` is byte-identical to this branch's.
- Backend suite on this branch: full-suite attempt started 2026-11-28
  ~06:45 PKT (`pytest tests/ -q -p no:cacheprovider -rfEs`, log
  `/home/hatch/tmp-aa130/fullsuite.log`, pid 96479) — **OOM-killed during
  collection/setup, FULLSUITE_RC=137, 0 tests ran**. No code failure
  observed; the VM was at load ~36 with 159 MB free RAM. A chunked
  full-suite run on a quiet VM is still owed before merge. The focused
  gates above (delivery 11, worker 9, API 7, unit 44, frontend 26) are all
  green and are the branch's evidence for now.

## Problems found (all fixed)
1. `reports.admin` / `reports.viewer` roles never created in production
   (slice 2) — seeded once at API startup.
2. Weekly window pointed into the future (slice 5) — corrected to the seven
   days ending on the firing day.
3. Worker `deliver` contract had no delivery id, so the exactly-once key
   could not be the delivery row (slice 4) — contract changed, slice-3 test
   mocks updated, worker stamps `provider_ref`.
4. Slice-5 worker tests assumed the `deliver` callback fires for `in_app`
   (test bug, product correct): the `in_app` channel never calls the
   callback — the delivery record itself is the notification. Both new tests
   now include the `whatsapp` channel for the body capture and assert 2
   sent deliveries (in_app + whatsapp).
5. Slice-5 overdue test called `_setup` twice in one test DB (test bug):
   the second call re-created the "Finance" department and hit the unique
   `(kind, name)` constraint. The second schedule is now inserted directly
   into the same setup.
6. First frontend gate failed typecheck: `Badge tone="muted"` and `Button
   variant="outline"` do not exist on this branch's ui components (use
   `neutral` / the default variant), and the `.mjs` helper needed a
   `.d.mts` declaration (the repo's pattern for `.mjs` modules), not `.d.ts`.
   All fixed; tsc RC=0.
7. `npm ci` OOM-killed three times (RC=137) under VM load — worked around
   by copying `node_modules` from the frontend-redesign worktree (identical
   lockfile).
8. A VM restart mid-gates killed the API run at 4 dots and wiped the
   `postgres` user's shell state; the AA cluster was recovered (postgres
   uid 999 still owned the data dir; `/var/run/postgresql` re-chowned from
   `ah82`; stale `postmaster.pid` removed; cluster restarted on port 55497).
9. Agent 130's own roundtrip script printed the throwaway test-DB URL
   (password embedded) to its log — caught immediately, script killed, log
   shredded; the password never left the VM (test-only cluster credential).

## Leftovers / owner decisions still open
- Default send time 08:00 PKT (implemented as default) and quiet-hours
  override for overdue alerts (proposed: no) — still unconfirmed.
- WhatsApp number / email for delivery (Step F credentials still needed);
  until configured, WhatsApp/email deliveries wait in the retry queue.
- Template messages for expired WhatsApp 24-hour windows — later slice.
- Baileys failover for WhatsApp — not in v1.

## Merge notes
- Migration `0104` is also minted by HR, language preference, AI freshness,
  finance autopilot, AB, continuity/Step G, and foundry — renumber at merge.
  Migration `0105` is also minted by AK, AI, and AB — renumber at merge.
  Both AA migrations are self-contained (3 new tables + 1 added column, no
  existing-table changes), so they renumber cleanly.
- Step AA is code-complete: no successor slices remain.
