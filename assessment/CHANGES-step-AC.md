# CHANGES — Step AC: Finance improvements (cash-flow forecast, receipt photo, tax report)

Step AC makes the finance module look forward as well as back: a cash-flow
forecast that is always labelled as a forecast, receipt photos that become
`pending` invoices in the approval queue, and a one-click tax report. Nothing
in this step pays, approves or changes a payee — the forecast, the photo
intake and the tax report are all read-only or queue-only. The same 17
verification checks run for every intake path, and every number comes from
departments the caller can read (`finance.read`).

## What changed

### Slice 1 — pure-function cash-flow forecaster (Agent 49, HEAD `be6bfd5`)

- `app/finance/cashflow.py` (stdlib-only, no DB/network/app imports): `forecast_cash_flow()`
  over monthly history + unpaid invoices. Scheduled outflow = pending invoices due
  that month (no due date → invoice date + 30-day terms; no date at all → month 1
  with a warning). Recurring estimate = average monthly outflow over the last 6
  history months. Expected inflow = history average unless the caller supplies
  planned inflows. Every month carries `basis` (`scheduled` / `recurring` /
  `mixed`) and `confidence` (`high` ≥ 6 history months, `medium` 3–5, `low`
  otherwise). Money is `Decimal`, quantised to 2 decimals; one 3-letter currency
  per run. Nothing is silently dropped: undated invoices, other-currency rows and
  empty history all produce warnings, never a zero presented as certain.

### Slice 2 — receipt-photo intake (Agent 49, uncommitted at handover; verified by Agent 153)

- `app/finance/receipts.py`: image validation (PNG/JPG/WebP only, magic-byte
  check, 10 MB limit) → local offline OCR (`app.rag.ocr` RapidOCR, no network)
  → the same deterministic `facts_from_text()` extractor as every other intake
  path. A photo OCR cannot read is an error, never a guessed invoice. OCR text
  is untrusted data: it fills invoice fields only — it cannot choose a payee
  account (the vendor master does), grant a permission or change an approval
  target. `receipt_digest()` (SHA-256 of the image) makes re-sending the same
  photo idempotent.
- `FinanceService.add_receipt_photo()`: `invoice.ingest` is checked before the
  photo is read. The invoice is created with `source_kind='file'` (the table
  check constraint allows only json/text/file/document) and the photo origin is
  recorded as `extraction.receipt_photo: true`, so the queue can tell a receipt
  photo apart. The invoice lands as `pending`; the same 17 verification checks
  run when the batch starts. Audited as `finance.receipt_photo_ingested`.
- `POST /api/finance/batches/{batch_id}/invoices/receipt-photo` (201): image
  upload (10 MB + 1 byte read cap), returns the created invoices.

### Slice 3 — cash-flow service + API (Agent 49, uncommitted at handover; verified by Agent 153)

- `FinanceService.cash_flow()`: history from `spend()` (verified/approved/paid
  only, up to 36 months), pending invoices from `readable_groups()` only
  (statuses `pending` / `verified` / `exception` / `approved`, never
  `paid`/`rejected`). `FinInvoice` has no due-date column, so due dates are
  derived by the forecaster. No inflow history is recorded anywhere, so expected
  inflow is 0 with a `no_inflow_history` warning rather than a number presented
  as certain. A `group_id` the caller cannot read raises 404/403 via `require()`.
- `GET /api/finance/cash-flow` (currency required, months 1–12, optional
  `opening_balance` ≥ 0): every month labelled `kind: 'forecast'`.
  `GET /api/finance/cash-flow.csv`: one-click CSV export of the same projection.
- `cash_flow_out()` / `cash_flow_csv()` serialisers: amounts as strings, same
  numbers in JSON and CSV.

### Slice 4 — tax report (Agent 153)

- `FinanceService.tax_report()`: totals by period (month) + department +
  currency over `verified` + `paid` invoices only, per currency, with per-currency
  grand totals. Read-only; permission-filtered via `readable_groups()`. The
  invoice schema has no separate expense-category column, so the department
  (falling back to the department group's name, as `spend()` does) is the
  breakdown dimension — no category is invented. Invoices with no date or no
  amount cannot be bucketed or totalled and are counted in warnings
  (`skipped_no_date`, `skipped_no_amount`), never silently dropped.
- `GET /api/finance/tax-report` (months 1–12 default 12, optional `group_id`,
  optional `currency`) and `GET /api/finance/tax-report.csv` (one-click download,
  same rows as the JSON view).
- `tax_report_out()` / `tax_report_csv()` serialisers.

### Slice 5 — finance-page UI (Agent 153)

- `frontend/src/finance/api.ts`: types `CashFlow`, `CashFlowMonth`, `TaxReport`,
  `TaxReportRow`, `TaxReportTotal` + methods `cashFlow()`, `uploadReceiptPhoto()`,
  `taxReport()`, `downloadCsv()`.
- `frontend/src/finance/financeView.mjs`: `cashFlowSeries()` helper (expected
  outflow + closing balance per month, max for the chart scale) + node test;
  `financeView.d.mts` declaration added so `tsc` sees it.
- `frontend/src/finance/FinancePage.tsx`: tabs extended to
  `batches | exceptions | approvals | spend | cashflow | tax`; new `CashFlowTab`
  (forecast chart, every number labelled "forecast", warnings, CSV download)
  and `TaxReportTab` (period/department/currency table, totals, CSV download);
  receipt-photo upload button in BatchDetail (accept `.png,.jpg,.jpeg,.webp`,
  title explains pending-queue + local OCR).
- Two defects found and fixed during the gates: the `SpendTab` function header
  was dropped by the edit that inserted the new tabs (tsc TS1128), and
  `cashFlowSeries` had no `.d.mts` declaration (tsc TS2305/TS7006).

## Tests

- Slice 1 forecaster: **23 passed** (Agent 49, HEAD `be6bfd5`).
- Slice 2 receipt photo: **10 passed, 0 failed** on the rebuilt cluster (port
  55531) — 7 DB tests in two halves (4 passed in 404s + 3 passed in 572s) and
  3 pure validation/extraction tests (3 passed in 158s). Agent 49's 3 test
  bugs were already fixed in the uncommitted files; the re-run proves it.
- Slice 3 cash flow: **4 passed, 0 failed** on the rebuilt cluster (port
  55531) — half A (projection + CSV, 2 passed in 183s after the test fix
  below) + half B (other-department invisible + bad params, 2 passed in
  321s). One real finding: `test_cash_flow_projection` first failed on the
  total (`425000.00` vs the test's expected `325000.00`). Root cause is a
  **test bug, not a product bug**: the test forgot that the two verified
  invoices are unpaid, so per the plan's pending definition (`pending` /
  `verified` / `exception` / `approved`) they fall due in month 2 (invoice
  date + 30-day terms) and are scheduled there — 100,000 scheduled + 100,000
  recurring = 200,000 in month 2. The service follows the plan; the test's
  arithmetic assumed verified invoices are history-only. Fixed the test
  (total `425000.00`, closing `-225000.00`, plus month-2/3 assertions pinning
  the behaviour); the re-run passes. The cluster died mid-way (VM reboot
  ~07:14 UTC wiped the `ac153` OS user and shut the postmaster down cleanly);
  the user was recreated (`useradd -u 1020`) and the cluster restarted —
  data intact, no re-init needed.
- Slice 4 tax report: **6 passed, 0 failed** on the rebuilt cluster (port
  55531) — half A (totals by period/currency + paid invoices count +
  pending-excluded, 3 passed in 333s) + half B (currency filter + CSV
  matches JSON + other-department invisible, 3 passed in 299s). One seed bug
  found and fixed on the way: TX-004 shared PO-1001 with TX-001 (50,000 +
  50,000 against a 50,000 PO trips the `po_overbilled` check, 2% tolerance);
  TX-004 now uses its own PO-1003. **Test-seed bug, not a product bug.**
  Half B's first attempt errored 3× at setup (`ProgrammingError` on
  `INSERT INTO users`) because a second pytest session (the slice-1
  regression) was migrating the same test DB at the same time — never run
  two pytest sessions against one test DB; re-run alone passed.
- Slice 5 frontend: **npm test 23 passed, 0 failed** (full `test/*.test.mjs`,
  incl. the new cash-flow series test); **`tsc --noEmit` clean** (after fixing
  the two defects above); **`vite build` clean** (`BUILD_RC=0`, built in
  25.38s).

## Gates

- Alembic: no migration in this step (no model changes) — `alembic check`
  clean against the test DB (`No new upgrade operations detected`, head
  `0103_unified_studio_governance`). Note: a bare `alembic check` reads
  `DATABASE_URL` (the never-migrated `infraspine_ac` DB) and wrongly reports
  "not up to date" — the check must run with `DATABASE_URL` pointed at the
  test DB. Every pytest session's fixture already runs a downgrade→upgrade
  cycle on the test DB, so the migration chain is exercised on every run.
- Full backend suite: NOT run (shared VM cannot complete it right now; the
  Step E fifth full-suite pass died with the VM reboot ~07:14 UTC — see the
  Step E verification gate). Owed before merge, like for every other step.

## Merge notes

- **No migration** — nothing to renumber.
- **Two finance-page surfaces:** this branch adds the cash-flow tab, receipt
  upload and tax button to `frontend/src/finance/FinancePage.tsx`; the live
  PPTX-export edits touch `frontend/src/components/ModuleWorkspaces.tsx` only —
  no conflict expected, reconcile at merge if the redesign branch moved the
  finance page.
- **Test cluster (Agent 153):** private PG16 cluster `/home/hatch/.pg-ac153/cluster`,
  port **55531**, role `ac153`, DBs `infraspine_ac` / `infraspine_ac_test`,
  password in `/home/hatch/.ac153-db-pw`, env in `/home/hatch/.test-env-ac153.sh`
  (both chmod 600, never committed). Agent 49's cluster (port 55449) was wiped
  by a VM restart; the empty dir `/home/hatch/.pg-ac49` is dead — do not reuse.

## Owner decisions still open

- None from this step's plan. Defaults implemented: 30-day payment terms,
  12-month tax-report window, receipt photos limited to PNG/JPG/WebP at 10 MB,
  OCR is local/offline only (no cloud OCR, no upload of photos anywhere).
