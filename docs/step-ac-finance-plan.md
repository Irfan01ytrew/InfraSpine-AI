# Step AC — Finance improvements: cash-flow forecast, receipt photo → invoice, one-click tax report

Owner-approved 2026-11-28 (module-improvement round, "Har module mein aur kya behtar
ho sakta hai"). Agent 36 preparation + first safe slice.

## What exists vs what is missing

| Area | Exists (not rewritten) | Missing |
|---|---|---|
| Invoices | `FinInvoice` (`fin_invoices`): number, vendor, PO, department, amount, currency, date, bank account on invoice, `source_kind` json/text/file/document, `extraction` JSONB, status `pending/verified/exception/approved/paid/rejected`, `reasons` | No receipt-photo intake; no cash-flow forecast; no tax report |
| Verification | `app/finance/verification.py` `verify_invoice()` — 17 checks (missing fields, invalid amount, unknown/blocked vendor, bank mismatch, future/stale date, duplicate invoice, PO missing/not found/closed/vendor mismatch, currency mismatch, over-PO amount, …) | Nothing here — the 17 checks are reused as-is for every new intake path |
| Extraction | `app/finance/extraction.py` `facts_from_text()` — deterministic extractor over document text (`app.rag.financial_forms`); extracted text is untrusted data | Image intake: no OCR/text step for a photographed receipt; no mapping from a photo to `facts_from_text` |
| Spend | `FinanceService.spend()` — monthly aggregates by department/currency over `SPEND_STATUSES = (verified, approved, paid)`, up to 36 months, permission-filtered via `readable_groups()` | Backward-looking only; no forward projection, no opening/closing balance |
| Payments | `FinPayment`, approval flow bound to the exact action hash; `payment.approve` / `funds.transfer` are separate permissions | Forecast must never trigger or imply a payment |
| Forecasts (other) | `app/api/routes/forecasts.py` + `ForecastScenario` — owner-scoped deterministic what-if scenarios | That is scenario planning, not cash flow; Step AC builds a separate cash-flow forecaster and does not touch it |
| Tax | Nothing | No tax totals, no category/period report, no CSV export |

## Design rules (non-negotiable)

1. **A forecast is a forecast, never a fact.** Every forecast month is labelled
   `forecast`, carries its `basis` (`scheduled`, `recurring`, `mixed`) and a
   `confidence` (`high` / `medium` / `low`). The UI must show "forecast" next to
   every projected number. No number is ever presented as if it were already spent.
2. **Payments always need owner approval.** Forecast, receipt intake and tax report
   never pay, never approve, never change a payee. A receipt photo only creates a
   `pending` invoice in the approval queue, through the same 17 verification checks.
3. **Extracted text is untrusted data.** Receipt text (like invoice text) cannot
   choose a payee account (the vendor master does), cannot grant a permission and
   cannot change an approval target. An instruction written on a receipt is just text.
4. **Nothing is silently dropped.** A pending invoice without a usable date is
   bucketed into month 1 with a warning. A row in another currency is skipped with a
   warning count. Empty history yields `low` confidence and a warning, not a zero
   presented as certain.
5. **Permission-filtered, always.** Every later service/API slice reads only
   `readable_groups()` (`finance.read`) — the same scope rule as `spend()`.
6. **Pure functions first.** The forecaster is stdlib-only, no DB/network/app imports,
   so it is testable without a database and cannot move money by construction.
7. **Money is `Decimal`.** No floats anywhere in money math; amounts quantised to
   2 decimals; currency is a single 3-letter code per forecast run.

## Forecast method (slice 1)

Inputs (all plain data, no DB):
- `history`: monthly actuals — `[{month: 'YYYY-MM', outflow: Decimal, inflow: Decimal}]`
  from `spend()`-style aggregates (verified/approved/paid only).
- `pending`: unpaid invoices — `[{amount, currency, due_date | None, invoice_date | None}]`
  (statuses `pending` / `verified` / `exception` / `approved`; never `paid`/`rejected`).
- `opening_balance`: Decimal (the known cash balance today; 0 with a warning if unknown).
- `months`: how many future months to project (default 3, max 12).
- `payment_terms_days`: default 30 — an invoice with no due date is due
  `invoice_date + terms`; an invoice with neither date goes to month 1 with a warning.

Output per month: `{month, scheduled_outflow, recurring_estimate, expected_outflow,
expected_inflow, net, closing_balance, basis, confidence, warnings}`.
- `scheduled_outflow` = sum of pending invoices due that month.
- `recurring_estimate` = average monthly outflow over the last 6 history months
  (fewer months if less history; none if no history).
- `expected_outflow` = scheduled + recurring (recurring is the baseline for invoices
  not yet raised; scheduled invoices are NOT double-counted in the average).
- `expected_inflow` = history average monthly inflow (same window) unless the caller
  supplies planned inflows; labelled as an estimate either way.
- `closing_balance` = opening + running net; `confidence` = `high` when ≥6 history
  months and scheduled share is known, `medium` with 3–5 months, `low` otherwise.

## Slices (build order)

1. **DONE (Agent 36)** — this plan + pure-function forecaster
   (`app/finance/cashflow.py`) with tests. No DB, no API, no UI.
2. Receipt-photo intake — upload endpoint (image only, size/type limits) → OCR/text
   step → `facts_from_text()` → same 17 checks → `pending` invoice in the approval
   queue, `source_kind='file'`, `source_digest` of the image. Rejected/blurry photos
   return a clear error, never a guessed invoice.
3. Cash-flow service + API — `FinanceService.cash_flow()` on a throwaway `*_test` DB
   (history from `spend()`, pending from `invoices()` filtered to unpaid statuses,
   `readable_groups()` only) + `GET /api/finance/cash-flow` through `app/platform`
   (`finance.read`); forecast months labelled, CSV export of the projection.
4. Tax report — `FinanceService.tax_report()` (totals by department/category/period
   over paid + verified invoices, per currency) + `GET /api/finance/tax-report`
   (`finance.read`) + one-click CSV download; totals computed from DB rows only.
5. Finance-page UI — forecast chart (clearly marked "forecast"), receipt-photo upload
   with queue status, tax report button; `CHANGES-step-AC.md` + full suite / Alembic /
   frontend gates, then merge to main.

## Test list

- Slice 1 (this agent): forecaster unit tests — scheduled bucketing, recurring average,
  running balance, missing-date warning, empty-history low confidence, multi-month,
  invalid inputs, currency filtering, no double-counting.
- Slice 2: photo intake tests (mocked OCR text) — happy path, blurry/empty photo,
  duplicate invoice flagged by the 17 checks, unknown vendor flagged, no auto-pay.
- Slice 3: service tests on a throwaway `*_test` DB — history + pending wiring,
  permission filtering (other department invisible), CSV shape.
- Slice 4: tax report tests — totals by period/category, currency separation, CSV.
- Slice 5: frontend tests + build + typecheck green.
