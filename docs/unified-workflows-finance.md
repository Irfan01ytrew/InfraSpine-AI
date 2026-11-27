# Step E — one workflow runtime and invoice verification

Migrations: `0101_unified_workflow_runtime`, `0102_finance_invoices` (head). Both downgrade cleanly
(`alembic upgrade head && alembic downgrade base && alembic upgrade head && alembic check`).

## One runtime

Every multi-step process runs on the module 05 engine (`app/workflows`):

- **Definitions → versions → runs → steps.** A run records its `plan` (node order, handler, permission,
  attempts, timeout), an `origin` (`workflow`, `automation`, `finance`, `studio`, `governance`) and moves
  through `plan → execute → verify → recover → done` (`wf_runs.phase`, `wf_steps.phase`).
- **Action steps** (`app/workflows/action_runtime.py`) call a registered handler: `run(ctx)` does one
  slice and returns `more | waiting | done | failed`; `verify(ctx, output)` reads the result back.
  A step is only `verified` after the read-back; checkpoints are sealed; between items the handler
  re-checks run status, claim and the actor's permission (`ctx.should_stop()`).
- **Retries / backoff / timeouts** per node (`max_attempts`, `timeout_seconds`), run deadline, lease
  reclaim after a crash. **Pause / stop / resume** continue from the last verified step.
- **Platform core underneath:** `authorize` per scope, approvals bound to the exact `ActionSpec.hash`
  (expiry, single use, requester cannot approve, separation of duties), `execute_once` for side
  effects, events through the outbox (`workflow.blocked`, `finance.payment_verified`, …).
- **Old APIs kept:** the automation API and worker are a facade; each automation run is mirrored as a
  workflow run (`app/workflows/unified.py`). Agent Studio and governance actions are exposed in the
  handler catalog (`GET /api/workflow-engine/templates` → `actions`).
- New endpoints: `POST /api/workflow-engine/scopes/{s}/runs/{r}/steps/{id}/perform` (take an action
  step as yourself), control command `stop`, and the generic inbox `GET/POST /api/approvals`.
- The engine worker is on by default except when `APP_ENV=testing`
  (`WORKFLOW_ENGINE_WORKER_ENABLED=false` turns it off).

## Invoice verification (`app/finance`)

Tables: `fin_vendors`, `fin_purchase_orders`, `fin_goods_receipts`, `fin_invoice_batches`,
`fin_invoices`, `fin_payments`.

Workflow `finance.invoice_batch`: `verify` (permission `invoice.verify`) → `approve`
(requests one approval per vendor payment) → decision → `pay` (`funds.transfer`, critical) → end.

- **Ingest** PDF/DOC/text (step-A "Label: value" parsing, 10 MB cap) or JSON rows (only known keys).
  Document text is data: it can fill invoice fields, never permissions, payees or approval targets.
- **Checks** (reason codes in `app/finance/verification.py` `REASONS`): required fields and amount, vendor master (unknown/blocked vendor, bank account on the invoice ≠ master), PO exists,
  same vendor, open, received quantity/amount, already billed, amount within tolerance
  (max of absolute and %), currency, date (future / older than `FINANCE_MAX_INVOICE_AGE_DAYS`),
  duplicates (against history and within the batch).
- **Result:** "32 of 50 verified", every exception with reasons, remaining ids; exceptions queue with
  *correct* (clerk, invoice goes back for verification next round) or *reject* (verifier).
- **Roles per department scope:** `finance.viewer`, `finance.clerk` (invoice.ingest),
  `finance.verifier` (invoice.verify), `finance.approver` (payment.approve), `finance.treasury`
  (funds.transfer), `finance.admin`. SoD: the same person cannot verify, approve and transfer for one batch.
- **Payments:** payee account only from the vendor master; the approval hash covers batch, payment,
  payee, account, amount, currency and invoice ids and is recomputed from the database before
  transfer. Transfers use `execute_once` with an idempotency key and are marked paid only after the
  connector read-back; an ambiguous submit is reconciled by idempotency-key lookup.
- **Connector settings:** `FINANCE_PAYMENT_CONNECTOR=mock|http`, `FINANCE_PAYMENT_FALLBACK`,
  `FINANCE_BANK_API_URL`, `FINANCE_BANK_API_TOKEN`. The mock is refused in production unless
  `FINANCE_ALLOW_MOCK_IN_PRODUCTION=true`. Other: `FINANCE_VERIFY_SLICE` (25),
  `FINANCE_APPROVAL_TTL_HOURS` (24), `FINANCE_MAX_BATCH_INVOICES` (1000).
- **Spend chart:** `GET /api/finance/spend?months=12` (month × department × currency over verified,
  approved and paid invoices, organisation time zone).
- **UI:** sidebar *Operations → Invoices & payments* (batches, exceptions, approvals, spend);
  *05 · Workflow engine* shows runs with pause / resume / stop / perform.

## Tests

`tests/test_finance_invoice_flow.py` (E2E 50 → 32, SoD, exact-action approval, expiry and single use,
revocation, crash and lease reclaim, ambiguous payment, pause/resume, department scope, round two),
`tests/test_finance_units.py`, `tests/test_unified_workflow_runtime.py`, `frontend/test/financeView.test.mjs`.
