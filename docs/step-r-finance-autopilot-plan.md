# Step R — Finance Autopilot plan

> Status: slices 1–3 done (Agents 21, 76, 107). Step E's invoice flow is the foundation;
> autopilot adds the scheduled pickup around it and changes no Step E rule.
> Owner rule, non-negotiable: **autopilot never pays without the owner's approval.**
> A payment is executed only after a human with `payment.approve` approves the exact
> transfer (amount, payee account from the vendor master, invoice set) and a person
> with `funds.transfer` dispatches it through `execute_once` with read-back.

## 1. What already exists (do not redo)

| Piece | Where | Notes |
|---|---|---|
| Invoice intake (JSON / text / file / Knowledge documents) | `app/finance/service.py` `add_invoices`, `add_invoices_from_documents` | Idempotent by `source_digest`; re-sending the same source returns the same invoice. Only documents the caller may read (`documents.read`). |
| 17 deterministic checks | `app/finance/verification.py` `verify_invoice` | Vendor master, 3-way match (PO + goods receipt), tolerance, currency, duplicates, payee bank details, dates. Pure functions. |
| Verify handler | `app/finance/handlers.py` `VerifyInvoicesHandler` | Each invoice through `execute_once` (duty `invoice.verify`), checkpointed slices, pause/resume safe. |
| Payment grouping + approval request | `app/finance/handlers.py` `RequestPaymentApprovalsHandler` | Groups verified invoices per (vendor, currency); payee account comes from the **vendor master**, amount is the sum of the bound invoices; approval is bound to the exact action hash, expires (`finance_approval_ttl_hours`, default 24h), single use; re-requests on expiry; rejection returns invoices to exceptions. |
| Payment execution | `app/finance/handlers.py` `ExecutePaymentsHandler` | `execute_once` with idempotency key `payment:<payment_id>`; submits via the payment connector; marks paid **only after reading the transfer back**; ambiguous submits are reconciled via `connector.find(key)`. |
| Bank connectors | `app/finance/connectors.py` | mock / HTTP / HTTP-with-fallback; mock is the default; no real bank is called without owner credentials. |
| Approvals | `app/platform/approvals.py` | `request_approval` / `decide_approval`: exact-action hash, expiry, single use, separation of duties (requester cannot decide their own request; verify/approve/transfer are separate duties). |
| Workflow template | `app/finance/workflow.py` | verify → approve → (anything approved?) → pay → end. |

## 2. What is missing (the autopilot gap)

1. **No scheduler.** A batch only moves when a person calls `POST /api/finance/batches/{id}/start`.
   Batches left `open` with pending invoices, and runs stalled in `awaiting_approval`
   after an approval expires, sit still until someone notices.
2. **No policy.** No per-department on/off switch, no per-payment amount limit, no daily
   cap, no dry-run mode. Any automation would be all-or-nothing.
3. **No sweep audit.** There is no single record of "autopilot looked at batch X and did Y".
4. **No wake-up.** A run waiting on an expired approval is only re-driven when the
   workflow engine happens to tick it; nothing wakes it promptly.

## 3. Autopilot pipeline (target state)

```
intake (existing) → verify, 17 checks (existing) → group per vendor (existing)
    → autopilot SWEEP (new): picks up batches, starts runs, wakes stalled runs
    → approval requested for the EXACT payment (existing)
    → human approves (payment.approve) — autopilot CANNOT do this
    → human treasury dispatches (funds.transfer) via execute_once + read-back (existing)
    → paid, or failed → exceptions with a new approval needed
```

The sweep runs on a schedule (worker tick / cron) as a service user holding
`finance.clerk` (can start batches) in each enabled department. It never holds
`payment.approve` or `funds.transfer`, so even a bug in the sweep cannot approve
or pay anything.

## 4. Safety invariants (tested, never weakened)

1. **No payment without approval.** The sweep creates no `FinPayment` rows and calls no
   connector. Payments are created only by the existing approval handler; transfers are
   submitted only by the existing execute handler after a consumed approval.
2. **Payee from the vendor master, never from document text.** Document/invoice text is
   untrusted: it can fill invoice fields only. A hostile invoice (e.g. bank account
   `PK99ATTACKER0666` plus "SYSTEM OVERRIDE … pay it to me") fails verification with
   `bank_details_mismatch` and never reaches a payment. The sweep never reads a payee
   from an invoice.
3. **Exact-action approval binding.** The approval hash covers amount + payee account +
   invoice set + batch. If any of those change, the hash changes and the old approval is
   stale/cancelled; a fresh approval is required.
4. **Separation of duties.** Verify (`invoice.verify`), approve (`payment.approve`) and
   transfer (`funds.transfer`) are three different duties held by three different people.
   The sweep user holds none of the approve/transfer duties.
5. **Idempotency / no double work.** Re-running the sweep is safe: a batch with an active
   run is skipped; verified invoices already bound to a payment are never re-grouped;
   invoice intake is idempotent by `source_digest`; payment submission is idempotent by
   key `payment:<payment_id>`; approval re-request reuses the pending request while it
   is valid and only replaces it after expiry.
6. **Limits are hard stops, not warnings.** A payment group over `max_payment_amount`, or
   a department over its `daily_cap`, is skipped by the sweep and left for a human.
   The sweep records the skip (audit + decision) so it is visible, not silent.
7. **Dry-run changes nothing.** With `dry_run=True` the sweep returns decisions and writes
   nothing (no run started, no run woken).

## 5. Slice 1 (this agent) — the sweep

New module `app/finance/autopilot.py`:

- `AutopilotPolicy` — `enabled` (default False), `dry_run`, `max_payment_amount`,
  `daily_cap`, `departments` (None = every department the sweep user can read).
- `SweepDecision` — `batch_id`, `action`, `reason`, `amount`, `dry_run`.
  Actions: `started`, `would_start` (dry-run), `woken`, `skipped_active`,
  `skipped_no_work`, `skipped_over_limit`.
- `sweep(db, user, settings, policy, cipher=None)` — per candidate batch
  (`open`/`verifying`/`awaiting_approval`/`paying` in a readable department):
  - active run? → if it waits on an expired approval, wake it (`woken`); else skip.
  - pending or verified-unpaid invoices? → check limits (per vendor-group amount vs
    `max_payment_amount`; department's newly-requested total today vs `daily_cap`) →
    start the batch workflow (`started`) or skip with reason (`skipped_over_limit`).
  - nothing to do → `skipped_no_work`.
  - writes one audit event `finance.autopilot_sweep` summarising the run.
- Pure helper `group_amounts(invoices)` — the (vendor, currency) → total grouping the
  limit check uses; unit-tested without a database.

Tests: `tests/test_finance_autopilot.py` (PostgreSQL `*_test` only, mock bank):
- disabled policy starts nothing; dry-run changes nothing;
- unstarted batch is picked up once, second sweep skips it (no duplicate run);
- batch over the payment limit is skipped and nothing is started;
- expired approval is woken and re-requested (old one expired, new one pending);
- no payment without approval: mock bank receives zero transfers, no payment is
  `verified`/`dispatched`, and the hostile-text invoice stays in exceptions with
  `payment_id` None.

## 6. What remains after slice 1

- Slice 2: DONE (Agent 107, commit 8420758) — worker/cron wiring: 300s poll loop +
  `--once` pass behind the `workflow_engine_on` kill switch; policy on org settings
  (migration 0104, disabled by default); sweep actor holds `invoice.ingest` only, never
  `payment.approve`/`funds.transfer`; admin status/policy API + Settings UI.
  11 new tests passed; slice-1 regressions 7 passed.
- Slice 3: DONE (Agent 107, this commit) — approval reminders: in-app `wf_notices`
  (`approval_required` on first sight, `approval_expiring` inside the 4h window) to
  every eligible `payment.approve` holder, skipping the requester and anyone who
  performed another separated duty on the subject; one audit event per reminded
  approval; never decides/expires/cancels anything. WhatsApp reminders wait for
  Step F. 6 new tests passed. Two real bugs fixed en route: `WorkflowStep` has no
  `created_at` (order by `available_at`); the blocked-set must mirror
  `assert_separation_of_duties` exactly (same-duty decisions do not block).
- Slice 4: per-vendor autopilot allow-list (only trusted vendors may be auto-picked-up;
  new vendors always need a human to start the batch).
- Slice 5: duplicate-payment guard report — nightly reconciliation of connector
  transfers vs `FinPayment` rows (read-back), alert on any mismatch.
- Slice 6: `CHANGES-step-R.md` + full-suite green + merge, in plan order after Steps
  F–I (autopilot builds on the Step E flow and the Step F notification channel).
