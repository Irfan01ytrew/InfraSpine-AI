# CHANGES — Step R: Finance Autopilot (scheduled pickup, allow-list, duplicate guard)

> Branch `agent/finance-autopilot`, base `step-e-gaps-wip` (`ca956c2`).
> Status: slices 1–6 done. Focused gates green (see "Test evidence" and "Gates").
> Full-backend-suite count is covered by the merged-HEAD verification
> (Agent 96 / Agent 104 / Agent 151 / Agent 168 — 8th pass running, no
> verdict yet); merge only after that tally is green, in plan order after
> Steps F–I (autopilot builds on the Step E invoice flow and the Step F
> notification channel).

## What this step does (plain language)

Invoice batches no longer wait for a person to notice them. A scheduled
sweep picks up waiting batches, starts their workflow, and wakes runs that
stalled on an expired approval — while a human still approves every single
payment and a human still sends the money. Nothing in this step can pay
anyone on its own.

- **Scheduled pickup.** Every 5 minutes (worker tick, plus a `--once` pass
  a cron can call) the sweep looks at batches that are open or waiting and
  starts the ones with work, exactly as a clerk would by hand. A batch
  that already has a running workflow is left alone — no duplicate runs.
- **Wake-up.** A run waiting on an approval that expired is woken so the
  workflow asks for a fresh approval. The old, expired approval stays
  expired; nothing is decided by the sweep.
- **Policy, off by default.** An administrator switches the autopilot on
  per organisation, sets a per-payment maximum, a daily cap, and a dry-run
  mode (dry-run reports what it *would* do and changes nothing). Until an
  administrator enables it, the sweep does nothing.
- **Per-vendor allow-list.** Only vendors an administrator marked as
  trusted may be auto-picked-up. A batch naming a vendor that is not on
  the list — or no vendor in the vendor master at all (a new vendor) — is
  left for a human to start. New vendors are never allow-listed by
  default, and the flag is set only on the vendor page, never from
  invoice or document text.
- **Approval reminders.** When a payment approval is created, every person
  who may approve it right now gets an in-app notice; a second notice
  goes out when the approval is about to expire (inside 4 hours). A
  reminder never decides, expires, or cancels anything. WhatsApp
  reminders wait for Step F.
- **Duplicate-payment guard.** Once a day (and on demand from the admin
  page) the guard reads every dispatched/verified payment back from the
  bank connector and compares amount, currency, and payee account with
  the approved payment row, and checks that no invoice is covered by two
  payments. Any mismatch is reported; the guard itself never submits,
  creates, or changes a payment.

## The problem it fixes

Before this step a batch only moved when a person opened it and pressed
start, and a run stalled on an expired approval sat still until someone
noticed. There was no per-department on/off switch, no amount limit, no
daily cap, no dry-run mode, no single record of what the automation did,
and no reconciliation of what the bank actually received against what was
approved.

## The one rule that never bends

**Autopilot never pays without the owner's approval.** A payment is
executed only after a human with `payment.approve` approves the exact
transfer (amount, payee account from the vendor master, invoice set) and
a person with `funds.transfer` dispatches it through `execute_once` with
read-back. The sweep user holds `invoice.ingest` only — never
`payment.approve` or `funds.transfer` — so even a bug in the sweep cannot
approve or pay anything. The payee account always comes from the vendor
master, never from document text: a hostile invoice naming an attacker's
account fails verification with `bank_details_mismatch` and never reaches
a payment.

## Files changed (vs `ca956c2`)

### Slice 1 — the sweep (Agent 21, `9bdf724`)
- `app/finance/autopilot.py` (new) — `AutopilotPolicy` (enabled, dry-run,
  per-payment maximum, daily cap, departments), `SweepDecision`, `sweep()`
  (pickup + wake + limit checks + one `finance.autopilot_sweep` audit
  event), pure `group_amounts()`.
- `docs/step-r-finance-autopilot-plan.md` (new) — the plan, safety
  invariants, and slice list.
- `tests/test_finance_autopilot.py` (new, 7 tests).

### Slice 2 — worker wiring + org-settings policy (Agent 107, `8420758`)
- `app/services/finance_autopilot.py` (new) — `process_finance_autopilot`
  (300s poll + `--once`, behind the `workflow_engine_on` kill switch),
  `sweep_actor()` (earliest active `invoice.ingest` holder who holds
  neither `payment.approve` nor `funds.transfer`; `finance.autopilot_no_actor`
  audit when nobody qualifies), `last_sweep()` for the status page.
- `app/worker.py` — autopilot pass wired into the worker loop.
- `app/finance/autopilot.py` — `load_policy()` / `policy_from_row()` from
  the org-settings row.
- `app/platform/models.py` — `OrgSettings` gains
  `finance_autopilot_enabled` / `finance_autopilot_dry_run` /
  `finance_autopilot_max_payment_amount` / `finance_autopilot_daily_cap`
  (all default off / unset; upgrading never starts a sweep).
- `app/finance/service.py` — `autopilot_status()` / `set_autopilot_policy()`
  (admin-only, audited as `finance.autopilot_policy_changed`).
- `app/api/routes/finance.py` — `GET /api/finance/autopilot/status`,
  `PUT /api/finance/autopilot/policy` (admin-only).
- `frontend/src/finance/AutopilotSection.tsx` (new) + `SettingsPage.tsx` +
  `finance/api.ts` — autopilot status + policy controls in Settings.
- `migrations/versions/0104_finance_autopilot.py` (new) — the four policy
  columns on `org_settings`.
- `tests/test_finance_autopilot_slices2_3.py` (new, 11 tests in slice 2).

### Slice 3 — approval reminders (Agent 107, `a278b50`)
- `app/finance/approval_reminders.py` (new) —
  `process_finance_approval_reminders()` (300s poll): in-app `wf_notices`
  (`approval_required` on first sight, `approval_expiring` inside the
  4-hour window) to every eligible `payment.approve` holder, skipping the
  requester and anyone who performed another separated duty on the subject
  (mirrors `assert_separation_of_duties` exactly); one audit event per
  reminded approval; never decides, expires, or cancels anything.
- `app/worker.py` — reminder worker wired into the worker loop.
- `tests/test_finance_autopilot_slices2_3.py` (+6 tests in slice 3).
- Two real bugs fixed en route: `WorkflowStep` has no `created_at`
  (order by `available_at`); the reminder blocked-set must mirror
  `assert_separation_of_duties` exactly (same-duty decisions do not block).

### Slice 4 — per-vendor allow-list (Agent 160, 30ecb56)
- `app/db/finance_models.py` — `FinVendor.autopilot_allowed` (default
  False, server default False): only allow-listed vendors may be
  auto-picked-up.
- `app/finance/service.py` — `save_vendor(..., autopilot_allowed=False)`;
  the flag is set only here, by a finance manager through the vendor
  endpoint, never by the sweep and never from invoice/document text;
  audit `finance.vendor_saved` records the flag.
- `app/api/routes/finance.py` — `VendorIn.autopilot_allowed`, `vendor_out`
  returns it.
- `app/finance/autopilot.py` — pure `disallowed_vendors()` and sweep
  enforcement: a batch naming a known but unlisted vendor, or no vendor
  in the master at all, is skipped as `skipped_vendor_not_allowed` and
  left for a human to start. An invoice naming an unknown vendor does not
  block pickup when a vetted vendor is also present (verification rejects
  it with `unknown_vendor` and it never reaches a payment). The wake path
  is untouched: waking only re-drives a batch that was already started.
- `migrations/versions/0145_finance_autopilot_vendor_allowlist.py` (new; renumbered from 0105 at integrate merge — head 0144)
  — the `autopilot_allowed` column on `fin_vendors`.
- `tests/test_finance_autopilot_slice4.py` (new, 9 tests) —
  skip unlisted / mixed / no-known-vendor batches, dry-run skip,
  allow-listed start, unknown-vendor invoice does not block a vetted
  batch, API toggle, pure-helper cases, 0105 migration roundtrip.
- `tests/test_finance_autopilot.py`, `tests/test_finance_autopilot_slices2_3.py`
  — sweep-driven tests now allow-list the fixture vendors first.

### Slice 5 — duplicate-payment guard report (Agent 160, a7dfeff)
- `app/finance/duplicate_guard.py` (new) — `run_duplicate_guard()`
  (read-only: payment rows + `connector.find()` read-back only; never
  submits, creates, or changes anything): `missing_transfer`,
  `amount_mismatch`, `payee_mismatch`, `currency_mismatch`,
  `failed_transfer`, `duplicate_payment` (two dispatched/verified rows
  covering the same invoice), `read_failed` (reported separately when a
  read-back is unavailable); one `finance.duplicate_guard` audit event
  per run (payee account numbers stay out of the audit event; the report
  carries the details).
- `app/services/finance_autopilot.py` — `process_duplicate_guard()`
  (nightly: once per UTC day, gated on the last guard audit event; runs
  as the sweep actor) wired into the worker loop; `finance.duplicate_guard_no_actor`
  audit when nobody qualifies.
- `app/api/routes/finance.py` — `GET /api/finance/duplicate-guard/report`
  (admin-only, on-demand run).
- `tests/test_finance_duplicate_guard.py` (new, 10 tests) —
  all-matched, amount / payee / missing-transfer mismatches, duplicate
  payment, never submits + changes nothing, unsubmitted payments ignored,
  unread on connector failure, nightly once-a-day, API admin-only.

### Slice 6 — CHANGES + gates (Agent 160)
- `assessment/CHANGES-step-R.md` (this file).

## Test evidence

- Slice 1: 7 new tests passed.
- Slice 2: 11 new tests passed; slice-1 regressions 7 passed.
- Slice 3: 6 new tests passed. (Slice 2+3 proof was chunked — combined
  runs were OOM-killed on the shared VM; honest split recorded by
  Agent 107.)
- Slice 4: 9 new tests passed; slice-1 regressions 7 passed,
  slices 2–3 regressions 17 passed (16 + the 0104 roundtrip, which needed
  two environmental re-runs — see below).
- Slice 5: 10 new tests passed (9 at first, plus the unread test after a
  test-only fix — see below).
- Owed full run, `tests/test_finance_invoice_flow.py` (10 tests — the "31"
  in earlier notes was wrong; collection proves 10, no parametrisation):
  **10 passed, 0 failed in 36:26** (fourth attempt: attempt 1 passed 3 then
  died with the test cluster; attempt 2 errored on the stopped cluster;
  attempt 3 died in the VM reboot; attempt 4 green).
- Step R total new tests: 7 + 11 + 6 + 9 + 10 = **43**.

Two failures on the way, both fixed honestly, no product code weakened:

1. `test_guard_unread_when_connector_fails` failed at first: the test set
   `mock_bank.fail_reads = 10`, but the mock's `fail_reads` only gates
   `get()` — the Step E crash test depends on `find()` succeeding while
   `get()` fails once, so changing `find()` would have broken it. Fixed
   the TEST only: it now injects a `_FailingFindConnector` wrapper whose
   `find()` raises `ConnectorUnavailable`. Re-run: **1 passed**.
2. `test_migration_0104_roundtrip` failed twice for environment reasons
   only: first `psql` could not load `libpq.so.5` (VM reboots wiped the
   system Postgres libraries; fixed by exporting
   `LD_LIBRARY_PATH=/home/hatch/tmp/pg-dist/usr/lib/x86_64-linux-gnu`),
   then the columns were still present after the downgrade because the
   migration database had been left at head by the crashed run. Re-run
   from a manually reset migration database: **1 passed**.

## Gates

- Alembic: 0104 roundtrip clean (slice-2 test, green on re-run — see
  "Test evidence"); 0105 roundtrip green (standalone
  0105 → 0104 → 0105 on the throwaway migration database, plus the slice-4
  roundtrip test); `alembic check` at head = **"No new upgrade operations
  detected."** Single head `0145_finance_autopilot_vendor_allowlist` (renumbered from 0105 at integrate merge).
- Frontend: slice 2 added the Autopilot section in Settings;
  `npm test` **22 passed, 0 failed**; typecheck clean (exit 0);
  `vite build` clean on retry (exit 0, built in 2.85s — the first attempt
  was OOM-killed by the loaded VM). Slices 4–5 add no frontend code.
- Full backend suite: covered by the merged-HEAD verification
  (Agent 96 / Agent 104 / Agent 151 / Agent 168 — 8th pass running, no
  verdict yet) — not re-run on this branch; the branch changes the sweep,
  one vendor flag, one read-only guard module, and two admin endpoints,
  and all focused/regression suites above pass.

## Merge notes

- **Migration collisions (renumber at merge):** `0104_finance_autopilot`
  collides with HR, foundry, and WhatsApp-AE branches;
  `0105_finance_autopilot_vendor_allowlist` collides with AK, AI
  contradiction scan, AB expiry alerts, AA provider ref, and G handover
  grants. Whoever merges second renumbers; no data is affected.
  (At integrate merge: renumbered 0105 → 0145, down 0144.)
- **WhatsApp reminders wait for Step F:** slice 3 sends in-app notices
  only; the WhatsApp channel lands with Step F.
- **Owner input still open (plan):** none needed for these slices. The
  autopilot policy defaults (disabled, no caps) and the allow-list
  default (nobody listed) are the safe defaults until an administrator
  changes them.

## What is NOT in this step

- No payment path changed: verification (17 checks), approval binding
  (exact action hash, expiry, single use, separation of duties),
  execution (`execute_once` + read-back), and the bank connectors are
  exactly as Step E left them.
- The sweep and the guard never hold `payment.approve` or
  `funds.transfer`, never create a payment, and never call a bank
  connector that submits anything.
- No invoice, vendor, or payment data is ever read from document text
  for a payee or an approval: documents fill invoice fields only.
