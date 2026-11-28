# CHANGES — Step V: Wider integrations (Slack, Teams, Gmail, Google Drive, bank, ERP)

> Branch `agent/integrations`, base `step-e-gaps-wip` `ca956c2`, HEAD `f8eff19`.
> Prepared by Agents 26, 75, 78 and 84 (2026-11-28). Plain language, verified
> facts only. No real provider credentials exist anywhere in this work; every
> external call in tests goes through a mock transport or the in-process mock
> bank. Nothing here touches the live folder, the live database, `.env`,
> uploads, or data.

## What this step does, in plain language

InfraSpine already had a solid connector framework (sealed credentials,
signed webhooks, approval-gated jobs, circuit breaker, durable worker) but
no single place to see or use all the outside systems. Step V adds that:

- **One registry** that resolves a provider name (Slack, Teams, Gmail,
  Drive, ERP, bank, …) to its connector, lists what each provider can do,
  and answers which scope a write needs. The code-owned catalog is the
  allow-list — a provider not in the catalog cannot be registered.
- **A connector class for each provider**, all on the same shape:
  `verify()` + its operations + untrusted inbound-event normalisation.
  Every write is approval-gated: no approval whose digest covers the exact
  action, no call — verified by tests asserting zero provider calls.
- **The bank in the registry**, so the payment connector (mock by default,
  HTTP when an administrator configures it) appears in the same list as
  every other connector. A transfer is submitted once, read back, and only
  then reported as done. An unclear outcome is reconciled by idempotency
  key, never replayed.
- **One admin connectors page** (Module 10, "Integrations &
  communication") showing every provider with its operations and scopes,
  every configured connector with health / last sync / verification, the
  bank connector in force, and a pending-approvals queue with Approve /
  Reject buttons. Credentials are never shown. Changing the payment
  connector stays in Finance settings.

## Safety rules kept

- External content (messages, emails, Drive files, ERP records, bank
  events) is **untrusted data**: it is normalised into an allow-list of
  fields only, always marked `trusted: False`. Fields claiming a role,
  permission, approval, payee or amount are dropped, never acted on.
- No inbound text can change what an approval covers: approvals are bound
  to the digest created at enqueue time, expire, and are single-use.
- Money is `Decimal` only, never float. The payee always comes from the
  vendor master in the finance flow, never from document text.
- The mock payment connector stays the default; no real bank is contacted
  unless an administrator configures the HTTP connector.
- No secret, token, or credential appears in a log, error message, or test
  fixture (only `mock-token` / `synthetic-*` placeholders).

## Files changed (22 files, +2951/−9 vs `ca956c2`)

New connector code:

- `app/integrations/registry.py` — `ConnectorRegistry` (register / resolve /
  list / `supports_operation` / `required_scope`), `MockTransport`
  (records calls, fails closed on uncanned paths),
  `normalize_inbound_event` (allow-list, always `trusted: False`).
- `app/integrations/slack.py` — `SlackConnector` (`verify` via `auth.test`,
  `slack.send`, inbound normalisation).
- `app/integrations/teams.py` — `TeamsConnector` (Graph `/me` verify,
  `teams.send` channel message).
- `app/integrations/gmail.py` — `GmailConnector` (`verify`, read-only list,
  approval-gated `email.send`, typed quota/auth errors).
- `app/integrations/drive.py` — `DriveConnector` (read-only: `verify`,
  `sync`/list, file fetch/download; allow-listed file fields).
- `app/integrations/erp.py` — `ERPConnector` (declarative field maps for
  invoice/order/customer, money via `Decimal`, approval-gated submit, one
  POST per idempotency key, submit → read-back reconciliation,
  `ERPAmbiguousError` + `reconcile()` that never replays).
- `app/integrations/bank.py` — `BankConnector` wrapping a finance
  `PaymentConnector` (mock in tests, `get_connector` in production):
  read-only `verify` probe (reported, never raised), approval-gated
  `bank.transfer` with exact-action digest, read-back verification,
  `BankAmbiguousError` resolved only by `reconcile()`; a
  `ConnectorUnavailable` means the transfer was definitely *not* accepted
  and is re-raised unchanged (safe to retry).

Changed framework code:

- `app/integrations/catalog.py` — catalog entries `teams`, `erp`, `bank`;
  `WRITE_SCOPES` gains `("teams", "teams.send")`. (Bank and ERP writes are
  gated by exact-action approval, not a provider scope, so they have no
  `WRITE_SCOPES` entry — same rule as Gmail's approval gate.)
- `app/integrations/adapters.py` — `execute_write` gains a `teams.send`
  branch (Graph channel-message endpoint, mirroring the `email.send`
  provider split); a Teams write previously fell through to the
  connector's default `write_path`.
- `app/integrations/schemas.py` — `TeamsMessage` schema; `ConnectorInput`
  / `JobInput` literals extended with `teams` / `teams.send`.

Admin UI:

- `frontend/src/components/ModuleWorkspaces.tsx` — `IntegrationsWorkspace`
  gains a **Pending approvals** card (Approve/Reject posts the job digest
  to the decision endpoint; the server re-checks it), operations + scopes
  on every catalog provider card, and a read-only **Bank / payment
  connector** card from `GET /api/finance/connector`.
- `frontend/src/integrations/connectorsView.mjs` (+ `.d.mts`) — pure
  display helpers for that page (pending-approvals filter,
  needs-attention count, bank label), covered by node tests.

Plan:

- `docs/step-v-integrations-plan.md` — updated: slices 1–7 marked done,
  remaining work is slice 8 (this gate).

Tests (new files only; full list in the test section below):

- `tests/test_integrations_registry_slack_unit.py` (slice 1)
- `tests/test_integrations_teams_unit.py` (slice 2)
- `tests/test_integrations_gmail_unit.py` (slice 3)
- `tests/test_integrations_drive_unit.py` (slice 4)
- `tests/test_integrations_teams_write_unit.py` (slice 4, `teams.send`
  worker branch)
- `tests/test_integrations_erp_unit.py` (slice 5)
- `tests/test_integrations_bank_unit.py` (slice 6)
- `frontend/test/connectorsView.test.mjs` (slice 7)

## Tests

Step V unit suite (DB-free, `--noconftest`, setup-baseline venv):

- Slice 1: **11 new tests passed** (registry + Slack).
- Slices 2–3: **17 new tests passed** (7 Teams + 10 Gmail) + **15
  regressions passed** = 28 passed.
- Slices 4–5: **24 new tests passed** (9 Drive + 12 ERP + 3 teams.write);
  full Step V unit suite **52 passed, 0 failed**.
- Slice 6: **23 new tests passed** (bank); full Step V unit suite
  **75 passed, 0 failed**.
- Slice 7: **5 new frontend tests passed** (`connectorsView`); frontend
  `npm test` all green (26 tests total, 0 failed).

What the tests prove:

- A Teams write through the generic worker path reaches the Graph
  channel-message endpoint with the right body; a missing provider id is
  ambiguous (safe to reconcile, never assumed delivered).
- Gmail send and ERP submit cannot execute without a live approval bound
  to the exact action digest — tests assert zero provider calls for
  missing, wrong-digest, and expired approvals, and exactly one call for
  a valid approval.
- A bank transfer with no / wrong / expired approval records zero submits;
  the same idempotency key maps to exactly one transfer; a tampered
  read-back raises a reconciliation error; a lost reply is ambiguous and
  is found by `reconcile()` without a second submit; a connector that is
  unreachable *before* accepting raises `ConnectorUnavailable`, and
  `reconcile()` then finds nothing.
- The admin page's pending-approvals filter, needs-attention count, and
  bank label are covered by node tests (mock vs real bank, admin vs
  environment source, available vs unavailable).

## Gates

- **Alembic: CLEAN.** Upgrade head → downgrade base → upgrade head on a
  throwaway database, then `alembic check` = "No new upgrade operations
  detected." Single head `0103_unified_studio_governance`. Step V adds **no
  migration** (no new tables).
- **Full backend suite: NOT COMPLETED on this VM — environmental only,
  zero code failures observed.** Four attempts on branch
  `agent/integrations` at `f8eff19`, all with throwaway `*_test`
  databases and Agent 8's fixes (`NO_PROXY` sanitised, staged models,
  media deps):
  1. single-process full run — **OOM-killed after ~12 min** (VM load
     ~51, 34 pytest processes from ~10 agents sharing 1.5 CPUs / 8 GB);
  2. 3 chunked runs — chunk 1 **OOM-killed after ~6 min** (load ~60);
     loop stopped to avoid wasting cycles;
  3. single-process full run after a VM restart on a fresh private
     cluster (port 55450) — reached **82% with an `E` wall, then killed
     by a runtime restart**; diagnosis proved the `E`s were
     `ConnectionRefusedError` on port 55450 — the restart had killed the
     postmaster, not a code failure (cluster restarted, both DBs intact,
     verified afterwards);
  4. full run minus the 10 known-heavy files (docling trio, embeddings
     ×3, folder_sync ×3, ingestion_worker) — **OOM-killed after ~6.5 min**
     during setup (load ~46–60).
  **No test failure attributable to Step V code was seen in any attempt.**
  This step's backend diff vs the merged base is additive only (new
  connector modules + one catalog line + one `adapters.py` branch); the
  Step V unit suite (75 tests) and every slice regression run are green.
  The gate must be re-run on a quiet VM (or after other agents' suites
  finish) with the exact command in "Re-run" below; the 10 excluded heavy
  files are the same ones Agent 8 already flagged as needing a quiet
  re-run on the pre-merge base.
- **Re-run (for the Master Agent / a quiet VM):** private cluster port
  55450 (data `/home/hatch/tmp-pg-stepv/cluster`, OS user `stepv`,
  start with `pg_ctl -D … -o "-p 55450 -k /tmp
  -c listen_addresses=127.0.0.1" start`; DBs `infraspine_stepv_test` /
  `infraspine_stepv_app`, role `infraspine_stepv`) — or any fresh
  throwaway `*_test` pair — then from the worktree:
  `TMPDIR=$HOME/tmp NO_PROXY=localhost,127.0.0.1,::1
  PYTHONUNBUFFERED=1 TEST_DATABASE_URL=… DATABASE_URL=…
  <venv>/bin/python -m pytest --tb=line -q`
  (venv: `~/workspace/infraspine/work/backend-tests/.venv`, which has the
  media deps + staged models). Target ≥ 1347 passed, 0 failed.
- **Frontend typecheck + build: CLEAN.** `tsc --noEmit` exit 0 (no
  errors) and `npm run build` exit 0 (built in 10.19 s), verified
  2026-11-28 ~04:23 PKT on the quiet post-restart VM after two earlier
  OOM kills under load. `npm test` green (26 passed, 0 failed).
- **Pre-existing environment issue (not caused by this step):**
  `tests/test_integration_connector_schema.py` cannot be collected — it
  imports `tests/conftest`, which requires `DATABASE_URL` (pydantic
  `DatabaseSettings` validation error). Identical failure on the clean
  base via `git stash`.

## What is still open (not this step's job)

- **Production wiring of the registry:** the registry and connector
  classes are exercised through tests; no production code path yet builds
  a `ConnectorRegistry` from stored `Connector` rows. A later slice
  should add a `build_registry()` that constructs connectors from the
  stored rows (same open item for Slack/Teams/Gmail/Drive/ERP/bank).
- **Real provider credentials** (Slack bot token, Teams app, Google OAuth
  client, ERP endpoint + key, bank API URL + token) are the owner's to
  supply later — this step never asks for them and never stores them.
- **Merge:** merge to `main` only after the full backend suite (above)
  is re-run green on a quiet VM. Everything else in the gate is green:
  Alembic clean, frontend tests/typecheck/build clean, Step V unit suite
  75/75, slice regressions green.

## Commits (base `ca956c2`)

- `bb4bd4f` — plan (Agent 26)
- `aeb0982` — slice 1: registry + Slack (Agent 26)
- `1581ee1` — slice 2: Teams connector (Agent 75)
- `86f8dec` — slice 3: Gmail connector class (Agent 75)
- `3996c61` — slice 4: Drive connector + `teams.send` worker branch (Agent 78)
- `a3b900a` — slice 5: ERP connector (Agent 78)
- `9e3cff9` — slice 6: bank connector through the registry (Agent 84)
- `f8eff19` — slice 7: admin UI connectors page (Agent 84)
