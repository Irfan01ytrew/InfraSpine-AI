# Step Y — Finance anomaly detection (plan)

> Status: preparation + slices 1–3 done (Agent 31 slice 1; Agent 73 slices 2–3,
> branch `agent/anomaly-detection`).
> The software watches money flows and flags unusual invoices, duplicate payments
> and odd amounts **before** they are paid. Detection is **advisory only**: a flag
> blocks straight-through approval and forces explicit owner review, but it never
> auto-rejects a legitimate invoice, never changes a payee or an amount, and never
> grants or changes an approval. Approvals stay with the owner.

## 1. What already exists (not rewritten)

The deterministic invoice verification in `app/finance/verification.py`
(`verify_invoice`, 17 checks) is the existing gate, wired through
`app/finance/handlers.py` (`finance.verify_invoices`, via `execute_once`):

| # | Check | What it catches |
|---|-------|-----------------|
| 1 | `missing_fields` | required fields absent |
| 2 | `invalid_amount` | amount not positive |
| 3 | `unknown_vendor` | vendor not in vendor master |
| 4 | `vendor_blocked` | vendor blocked in master |
| 5 | `currency_mismatch` | invoice currency differs from PO |
| 6 | `po_missing` | no PO number |
| 7 | `po_not_found` | PO not found |
| 8 | `po_closed` | PO closed/cancelled |
| 9 | `po_vendor_mismatch` | PO belongs to another vendor |
| 10 | `receipt_missing` | no goods receipt for the PO |
| 11 | `amount_exceeds_po` | amount over PO beyond tolerance |
| 12 | `amount_exceeds_receipt` | amount over goods received beyond tolerance |
| 13 | `po_overbilled` | cumulative billing over PO beyond tolerance |
| 14 | `duplicate_invoice` | **same vendor + same invoice number already exists** |
| 15 | `bank_details_mismatch` | bank account differs from vendor master |
| 16 | `future_date` | invoice date in the future |
| 17 | `stale_date` | invoice older than the allowed age |

Payment safety already exists downstream: approvals are bound to the exact
transfer (amount, payee account from the vendor master, invoice set, batch),
expire, are single-use, and `execute_once` consumes the approval only when the
recomputed payment still matches; the payment connector submits with an
idempotency key and marks paid only after read-back.

## 2. What is missing (the gap Step Y closes)

The 17 checks are **per-invoice, deterministic, and point-in-time**. They do not
look at history or patterns, so they miss:

- **Near-duplicates**: same vendor, same amount, same/near date, but a *different*
  invoice number (re-typed, suffixed, or re-issued). Check 14 only catches the
  exact same invoice number.
- **Amount outliers**: an invoice 10x the vendor's usual amount, with a valid PO
  and matching receipt, passes every deterministic check.
- **First-time payees**: a vendor in the master with no payment history at all
  is treated exactly like a trusted long-term vendor.
- **Velocity**: 4 invoices from one vendor in a week, each individually valid,
  never raise a question.
- **Cross-invoice patterns**: the same amount paid twice to the same vendor
  under different numbers/dates.

## 3. Detection rules (slice 1: `app/finance/anomaly.py`, pure functions)

All rules are pure functions over invoice records; the caller loads history from
the department's own records only. Invoice/document text is untrusted data and
is never treated as an instruction.

| Code | Rule | Threshold (configurable) |
|------|------|--------------------------|
| `exact_duplicate` | same vendor + same invoice number in history | always (mirrors check 14, so the advisory layer is complete) |
| `near_duplicate` | same vendor + same amount + dates within window, invoice numbers differ | 7 days |
| `amount_outlier` | amount above median + k × MAD of the vendor's same-currency history; if MAD is 0, above 3 × median | ≥ 5 prior invoices, k = 5 |
| `first_time_payee` | vendor has no prior invoice in history | always |
| `rapid_repeat_payments` | ≥ N prior invoices from the vendor inside the window ending on this invoice's date | N = 3, window = 7 days |
| `bank_account_changed` | bank account on the invoice differs from the account this vendor used most recently (history, not the vendor master — check 15 already covers the master) | always, when a prior account exists |

Every flag has severity `warning` and carries machine-readable details
(median, observed amount, counts, window). `blocks_straight_through(flags)`
is true when any flag exists: the invoice goes to the exceptions queue for
explicit owner review instead of auto-approval. `requires_payee_verification(flags)`
is true for `bank_account_changed` and `first_time_payee`: those hold the payment
until the owner verifies the payee (approval-gated, never auto-approved).
Account numbers in flag details are masked to the last 4 characters.

## 4. Safety guarantees (non-negotiable)

- Advisory only: flags never reject, never pay, never change payee/amount.
- History is scoped to the caller's department; never compare across tenants.
- Amount comparison is same-currency only; missing/invalid amounts and dates
  skip the rules that need them (the deterministic checks already reject them).
- No network, no DB, no secrets in the detector; deterministic output for the
  same inputs (same flags, same order).
- Wiring (later slices) must go through `app/platform` authorization and the
  unified workflow engine; anomaly flags must be recorded on the invoice row's
  `reasons` alongside verification reasons, never as a silent side channel.

## 5. Slices (2–3 done by Agent 73)

2. DONE — flags wired into `finance.verify_invoices` (`app/finance/handlers.py`:
   `history_for()` + `_verify_one()`): flags are appended to the invoice `reasons`
   alongside the deterministic reasons (and their codes into the `invoice.verify`
   action spec), and any flag forces exceptions-queue routing. Handler tests in
   `tests/test_finance_anomaly_wiring.py`.
3. DONE — bank-account-change detection (`detect_bank_account_change()` in
   `app/finance/anomaly.py`, most-recent prior account as baseline, masked details)
   + new-payee verification hold (`requires_payee_verification()`; `PAYMENT_HOLD_CODES`):
   flagged invoices go to exceptions at verification, and payment grouping
   (`RequestPaymentApprovalsHandler._group`) refuses any still-verified invoice
   carrying a hold code, returning it to exceptions with `payee_verification_hold`.

Wiring decisions (recorded, because they shape the behaviour):
- **History is prior batches only.** Invoices in the batch being verified are
  contemporaneous intake, not history — counting them would flag any bulk intake
  (a batch's invoices share a date, so the velocity rule would fire on the whole
  batch). Same-batch exact duplicates are already caught by deterministic check 14
  (`seen_in_run`).
- **Cold start: a department's first batch is not flagged.** With no prior batch
  there is no baseline to compare against; a `first_time_payee` flag on every
  invoice of a first batch would carry no signal and would break the Step E
  acceptance (32 of 50 verified in the founder scenario). Once any prior batch
  exists, first-time vendors are flagged normally.
- A deterministically clean but anomaly-flagged invoice still reserves its PO
  capacity within the run (`verify_invoice` updates `billed_in_run` before the
  flags are known) — conservative: if the owner later approves it, it will bill
  the PO anyway.
4. Admin UI: anomaly queue with flag details, owner approve/reject with reason,
   per-vendor history view.
5. Tunable thresholds per department (org settings) + audit log of every
   flag review decision.
6. `CHANGES-step-Y.md`, full suite + Alembic + frontend gates, merge to main.

## 6. Test list (slice 1)

`tests/test_finance_anomaly.py` (pure, stdlib + pytest, `--noconftest`):
exact duplicate flagged / different vendor same number not flagged;
near-duplicate flagged within window, not outside it, not on different amount;
outlier flagged above median + 5×MAD, not flagged near median, not flagged with
< 5 history, MAD = 0 path (3× median); first-time payee flagged / known vendor
not flagged; velocity flagged at 3 priors in 7 days, not at 2, old priors outside
the window ignored; invalid/missing amounts and dates never crash and never
produce outlier flags; all flags severity `warning`;
`blocks_straight_through` true iff any flag; deterministic repeat output.
