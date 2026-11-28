# CHANGES — Step Y: Finance anomaly detection

> Branch `agent/anomaly-detection`, base `step-e-gaps-wip` (`ca956c2`).
> Status: slices 1–6 done. All focused gates green (see "Test evidence").
> Merge only after the Step E verification tally (Agent 96 / Agent 132) is
> green, and renumber migration `0104` at merge (see "Merge notes").

## What this step does (plain language)

The software now watches money flows and raises a question **before** an
unusual invoice is paid — never after, and never on its own authority.

- **Five advisory checks.** For every invoice, the software compares it with
  the department's own earlier invoices: an exact duplicate (same vendor,
  same invoice number), a near-duplicate (same vendor, same amount, a few
  days apart, different invoice number), an amount far above the vendor's
  usual amounts, a first-time payee (a vendor with no payment history), and
  several invoices from one vendor in a few days. A bank account that differs
  from the account the vendor used last time is flagged too.
- **Flags never decide anything.** A flag only stops the invoice from being
  approved automatically and sends it to the exceptions queue, where a human
  owner looks at it. A flag never rejects an invoice, never pays anything,
  never changes a payee or an amount, and never grants or changes an approval.
  Approvals stay with the owner.
- **Anomaly queue (admin UI).** The Finance page has a new "Anomaly queue"
  tab. Each flagged invoice shows which rules fired and why (amounts,
  counts, windows), with Approve / Reject buttons and a required reason.
  Approving accepts the invoice as-is; rejecting sends it back. Both
  decisions are written to the audit log, one row per decision, with who,
  when, what and why.
- **Per-vendor history.** From the queue you can open a vendor's earlier
  invoices, newest first, to judge whether the flag makes sense. Bank
  account numbers are shown masked (last 4 digits only).
- **Tunable thresholds per department.** Each department can tune the
  detection (how many prior invoices count as a baseline, how far above the
  usual amount is an outlier, the near-duplicate and repeat-payment
  windows). Defaults are the slice-1 values. A change never rewrites
  already-verified invoices — it only affects verifications that run after
  it — and every change is written to the audit log.

## The problem it fixes

The 17 deterministic verification checks (missing fields, unknown vendor,
amount over PO, duplicate invoice number, and so on) look at one invoice at
a time. They miss patterns that only show up across invoices: the same
amount paid twice under different invoice numbers, an invoice 10× the
vendor's usual amount with a valid PO, a brand-new payee treated exactly
like a trusted one, or four individually-valid invoices from one vendor in
a week. Step Y closes that gap with history-based, advisory-only flags.

## Files changed (vs `ca956c2`)

### Plan (Agent 31, `3fe3c54`)
- `docs/step-y-anomaly-plan.md` (new) — the rules, safety guarantees and
  slices. The plan is the authority for this step.

### Slice 1 — pure detector (Agent 31, `029b84a`)
- `app/finance/anomaly.py` (new) — `detect_anomalies()` plus the five rules
  (`exact_duplicate`, `near_duplicate`, `amount_outlier`,
  `first_time_payee`, `rapid_repeat_payments`), `blocks_straight_through()`,
  `requires_payee_verification()`, masked account details. No network, no
  DB, no secrets; deterministic output for the same inputs.
- `tests/test_finance_anomaly.py` (new, 18 tests at the time).

### Slice 2 — wire flags into verification (Agent 73, `68aa08e`)
- `app/finance/handlers.py` — `history_for()` (prior batches only, never
  the batch being verified) + `_verify_one()` appends flags to the invoice
  `reasons` alongside the deterministic reasons and into the
  `invoice.verify` action spec; any flag forces exceptions-queue routing.
- `tests/test_finance_anomaly_wiring.py` (new).

### Slice 3 — bank-account change + payee hold (Agent 73, `4b7cbf3`)
- `app/finance/anomaly.py` — `detect_bank_account_change()` (most-recent
  prior account as baseline, masked details) and
  `requires_payee_verification()` with `PAYMENT_HOLD_CODES`
  (`bank_account_changed`, `first_time_payee`).
- `app/finance/handlers.py` — payment grouping
  (`RequestPaymentApprovalsHandler._group`) refuses any still-verified
  invoice carrying a hold code and returns it to exceptions with
  `payee_verification_hold`.
- Fixed a real bug: `_verify_one` was called with 3 arguments where 4 were
  required (the handler contract), found by the wiring tests.

### Slice 4 — admin UI: anomaly queue, approve/reject, vendor history (Agent 127, `d67ad9e`)
- `app/finance/service.py` — `resolve()` gains the `approve` action: only
  when every reason is an anomaly flag (the payee-verification hold marker
  counts as one); sets the invoice verified, clears the reasons so payment
  grouping no longer refuses it, and audits
  `finance.invoice_anomaly_approved` with the flag codes that were
  reviewed. Approving an invoice that also has a deterministic reason is
  refused — the data must be corrected first. Also `vendor_history()` /
  `vendor_history_out()` (finance.read; masked bank accounts).
- `app/api/routes/finance.py` — `ResolveIn` accepts `'approve'`; new
  `GET /scopes/{group_id}/vendors/{vendor_id}/history`.
- `app/finance/anomaly.py` — public `ANOMALY_CODES` and `mask_account()`.
- `frontend/src/finance/FinancePage.tsx` — `AnomaliesTab` ("Anomaly queue"
  tab): flag details, Approve/Reject with a reason, per-vendor history
  panel.
- `frontend/src/finance/api.ts` — `vendorHistory` + `resolve` approve
  clients; `frontend/src/finance/financeView.mjs` (+ `.d.mts`) — pure view
  helpers that separate advisory flags from deterministic reasons.
- `tests/test_finance_anomaly_queue.py` (new, 5 tests) — full-flow proof
  through verification: flagged invoice lands in exceptions, approve marks
  it verified and audits, reject/correct behave as before, deterministic
  reasons can never be approved as-is.
- `tests/test_finance_anomaly_queue_api.py` (new, 8 tests) — the same
  resolve actions and vendor history seeded directly in the database, so
  the new code paths can be exercised without the workflow pumps (which
  take ~10 minutes per test on this VM).

### Slice 5 — tunable thresholds + audit of every decision (Agent 127, `452e99c`)
- `app/finance/anomaly.py` — `AnomalyConfig` is now the single source of
  thresholds; `config_from_settings()` / `validate_config_patch()` /
  `config_out()` / `SETTING_FIELDS` (6 tunable fields, strict ranges).
- `app/db/finance_models.py` — `FinAnomalySettings` (one row per
  department: merged thresholds JSONB); `migrations/versions/0104_fin_anomaly_settings.py`
  (new, down_revision `0103`).
- `app/finance/service.py` — `anomaly_settings()` (finance.read; effective
  values + which fields are customized) and `set_anomaly_settings()`
  (finance.manage; strict validation; audits
  `finance.anomaly_thresholds_changed` with the changed fields).
- `app/finance/handlers.py` — `anomaly_config_for()` loads the department's
  thresholds once per verification slice (`batch._anomaly_config`,
  transient, re-read every round); `_verify_one()` uses them, falling back
  to the slice-1 defaults when nothing is stored.
- `app/api/routes/finance.py` — `GET` / `PUT`
  `/scopes/{group_id}/anomaly-settings`.
- `frontend/src/finance/FinancePage.tsx` — `AnomalySettingsCard` in the
  Anomaly queue tab (finance.manage only): the six thresholds with a "custom"
  badge per changed field and a Save button; the card states plainly that a
  change never rewrites already-verified invoices.
- `tests/test_finance_anomaly.py` — slice-5 pure tests added (config merge,
  validation ranges, effective output); `tests/test_finance_anomaly_settings.py`
  (new, 6 tests) — API round-trip, strict rejection of bad values, 403
  without finance.manage, audit event per change, and the handler helper
  reading stored settings.

### Slice 6 — this document + gates (Agent 127, this commit)
- `assessment/CHANGES-step-Y.md` (this file).
- Gates: Alembic roundtrip + `alembic check`, frontend gates, full-backend-suite
  attempt (see "Test evidence").

## Test evidence

Commands run from `~/workspace/infraspine/work/anomaly-detection`
(`DATABASE_URL` / `TEST_DATABASE_URL` pointing at the throwaway
`infraspine_ai` / `infraspine_anomaly_test` databases on the private
cluster on port 55446; `NO_PROXY=localhost,127.0.0.1,::1`;
`TMPDIR=/home/hatch/tmp`).

- Pure detector + config tests:
  `python -m pytest tests/test_finance_anomaly.py --noconftest` —
  **33 passed, 0 failed**.
- Slices 2–3 wiring + regressions (Agent 73, final state): **64 executed,
  0 failed** (13 new tests, wiring 4/4, slice-1 regressions 33/33, pure
  27/27 — counts overlap across runs).
- Slices 2–3 wiring regressions on the final state (Agent 127):
  `pytest tests/test_finance_anomaly_wiring.py` — **4 passed, 0 failed**
  in 589.60s. (The first run was killed by a daemon restart at 07:15 UTC
  that wiped `/tmp` and shut down the test cluster; the cluster was
  restarted — the system PostgreSQL share dir had been wiped too, fixed by
  symlinking the stashed share at `/home/hatch/.pg-stepf-share16` into
  `~/pg16-dist/share/postgresql` — and the run was repeated from scratch.)
- Frontend: `npm test` — **23 passed, 0 failed** (includes the anomaly
  queue view-helper test); `tsc --noEmit` clean; `vite build` clean
  (23.73s). The first build attempt was OOM-killed (rc=137) while another
  agent ran `npm ci` at the same time; the re-run alone was clean.
- Seeded queue API tests:
  `pytest tests/test_finance_anomaly_queue_api.py` — **8 passed, 0 failed**
  in 367.67s (the re-run of the one failing test after the masking fix:
  1 passed in 390.12s). Three seed bugs were found and fixed while getting
  this file green (see "Problems found"), plus one real product bug
  (the vendor-history response leaked the vendor's full bank account —
  see below).
- Full-flow queue tests:
  `pytest tests/test_finance_anomaly_queue.py` — **5 passed, 0 failed**
  in 748.72s. (The first full-file run was OOM-killed mid-test-4 after
  1 failed / 1 passed / 1 failed; the two failures were the `flagged_pair`
  test bug above, fixed before the re-run.)
- Settings tests:
  `pytest tests/test_finance_anomaly_settings.py` — **6 passed, 0 failed**
  in 399.71s.
- Alembic (on the throwaway `infraspine_anomaly_test` database, port 55446):
  `alembic heads` shows a **single head, `0104_fin_anomaly_settings`**;
  `alembic check` rc=0. Roundtrip of the Step Y migration itself, verified
  by table presence: `fin_anomaly_settings` exists (1) → `alembic downgrade
  0103_unified_studio_governance` rc=0 → table gone (0) → `alembic upgrade
  head` rc=0 → table back (1) → `alembic current` = `0104` head.
  A full `downgrade base` is **refused by design** by the pre-existing
  Step E guard in migration `0101` (`RuntimeError: Unified-runtime runs
  exist... Restore a verified backup instead of downgrading`) — not a
  Step Y issue, and not worked around.
- Full backend suite on this branch: **NOT attempted.** The Step E
  verification pass owns the merge gate and its collector (Agent 143) may
  relaunch it at any time; starting a second full-suite pass would double
  the OOM pressure on a VM already at ~224 MB free with dozens of
  concurrent pytest processes (the Master Agent stopped Agent 90's
  driver for exactly this reason). Owed on a quiet VM after Step E
  merges — never claim green without a completed run. The focused gates
  above (every Step Y test file + frontend + Alembic) are green.

## Problems found (and fixed)

- **Real bug, `_verify_one` contract (Agent 73, slice 3).** The handler
  called `_verify_one` with 3 arguments where 4 were required; the wiring
  tests caught it and the call was fixed.
- **Real bug, vendor-history bank-account leak (Agent 127, slice 4).**
  The `GET .../vendors/{id}/history` endpoint masked the bank account on
  each *invoice* but returned the vendor object with the full bank account
  (`vendor_out`), so the full account still left the server in the same
  response. The history test caught it (`'PK00ALPHA0001' not in
  response.text` failed). Fixed in the route: the vendor's account is
  masked to its last 4 characters in this view only. The vendor-master
  list/detail endpoints keep their existing contract (out of scope).
- **Test bugs, seeded queue file (Agent 127, slice 4).** Three bugs in the
  test seeding only — product code unchanged:
  1. Raw SQL batch inserts omitted `tolerance_pct` and `tolerance_abs`
     (NOT NULL, ORM defaults do not apply to raw SQL) → added both
     (`2.00` / `0`, the model defaults).
  2. `:reasons::jsonb` in `text()` — SQLAlchemy's `::` cast conflicts with
     `:name` bind params (asyncpg "syntax error at or near ':'") →
     `CAST(:reasons AS jsonb)`.
  3. Date strings (`'2026-11-10'`) passed for the `Date` column
     `invoice_date` — asyncpg needs `date` objects →
     `date.fromisoformat(...)`.
- **Test bug, `audit_for` helper (Agent 127, slice 4).** Both queue test
  files called `.all()` on the coroutine returned by `conn.execute(...)`
  instead of awaiting it → the helper is now an async `read`.
- **Test bug, `flagged_pair` "clean" invoice (Agent 127, slice 4).** The
  full-flow helper's "clean" invoice was a Beta Traders invoice — but the
  history batch only had Alpha invoices, so Beta was correctly flagged
  `first_time_payee` (per the plan: once any prior batch exists, first-time
  vendors are flagged normally) and the "clean invoice is verified"
  assertion failed. The helper now uses an Alpha invoice with a different
  amount (45000.00, PO-1015) as the clean case. Product code unchanged —
  the flag was right, the test's assumption was wrong.
- **Racy grouping test (Agent 127, slice 4).** The first version of
  `test_approved_invoice_reaches_payment_grouping` approved the flagged
  invoice and then pumped, hoping grouping would run after the approval —
  grouping can run before the approve, so the test was rewritten
  deterministically: seed the exception + pending invoices, approve first,
  then start the run and verify grouping includes both.

## Leftovers / owed

- Verification-path proof that a *changed threshold actually changes
  detection* (e.g. a lower `velocity_max_prior` flags an invoice the
  defaults would not): the helper-level proof exists
  (`anomaly_config_for` reads stored settings), but a full verification
  round per threshold change takes ~10 minutes on this VM — a quiet-VM
  run is owed before merge.
- Full-backend-suite count on this branch (see "Test evidence").

## Merge notes

- **Migration `0104` is claimed many times over** (HR, AI freshness,
  finance autopilot, AB, G continuity, AA scheduled reports, language
  preference, foundry, scheduled reports, Step Y settings) and `0105` by
  AK / AI / AB. Renumber at merge: Step Y's
  `0104_fin_anomaly_settings` (down_revision `0103`) must be rebased onto
  whatever lands first.
- **Frontend:** `FinancePage.tsx` is touched by this step (AnomaliesTab +
  settings card) and by the frontend redesign rounds — reconcile at merge.
  `ModuleWorkspaces.tsx` conflicts noted elsewhere (live PPTX file +
  redesign + AD picker) do not involve Step Y.
- **Shared backend files:** `app/finance/handlers.py` and
  `app/finance/service.py` are shared with the finance-autopilot (R) and
  other finance work — the Step Y additions are additive (new methods,
  one new config lookup per verification slice) and do not change existing
  behaviour for departments with no stored settings (defaults apply).
- **Owner decisions still open:** none new for Step Y. The plan's
  safe defaults stand (thresholds = slice-1 values; audit of every
  flag-review decision; advisory-only throughout).
- **No successor slices remain for Step Y.** All 6 slices are done; the
  two items under "Leftovers / owed" are merge-gate runs, not new slices.
