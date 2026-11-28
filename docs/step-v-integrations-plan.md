# Step V — Wider integrations plan (Slack, Teams, Gmail, Google Drive, bank, ERP)

> Prepared by Agent 26 (Step V preparation), branch `agent/integrations`,
> base `step-e-gaps-wip` `ca956c2`. Status: **preparation + slice 1 only**.
> No real credentials exist anywhere in this work; every external call in
> tests goes through a mock transport. Nothing here touches the live folder,
> live DB, `.env`, uploads, or data.

## 1. Goal

One place where Slack, Microsoft Teams, Gmail, Google Drive, bank and ERP
systems live together with InfraSpine: messages and files flow in, actions
flow out, and **nothing arriving from outside is ever trusted** — external
text can never grant a permission, change a payee, or change what an
approval covers (HANDOVER-PROMPT.md §5 rule 4).

## 2. What already exists (mapped 2026-11-28, base `ca956c2`)

| Area | Location | State |
|---|---|---|
| Generic connector framework | `app/integrations/` (`catalog.py`, `service.py`, `adapters.py`, `transport.py`, `oauth.py`, `security.py`, `worker.py`, `models.py`, `schemas.py`) | **Done.** Code-owned catalog, sealed credentials, signed webhooks, idempotent jobs, approval-gated writes, bounded transport (no redirects, 2 MB cap), circuit breaker, durable worker with lease + reconciliation. |
| Providers in catalog | `app/integrations/catalog.py` `CATALOG` | `rest`, `graphql`, `soap`, `microsoft365` (Graph: Mail/Calendar/Files), `gmail`, `google_calendar`, `google_drive`, `slack` (`verify` + `slack.send`). |
| OAuth (PKCE, one-use state) | `app/integrations/oauth.py` | Microsoft 365 + Google services only. Slack/Teams use bot tokens, not OAuth here. |
| WhatsApp | `app/integrations/whatsapp.py`, `app/services/agent_studio_whatsapp.py` | Done (Step F prep by Agent 13 continues it). Kept separate from RAG. |
| Google Drive sync | `app/services/cloud_connectors/` (`base.py`, `google_drive.py`, `sync_engine.py`) | Done: delta sync, bounded pages, ACL reconciliation that fails closed. |
| SharePoint / OneDrive | `app/services/cloud_connectors/sharepoint_onedrive.py` | Done (Graph-backed). |
| Bank connector | `app/finance/connectors.py` | Done: mock / HTTP / HTTP-with-fallback; Gap 4 fix keeps a usable primary when a fallback cannot be built. Admin shows the active connector. |
| ERP | — | Only the generic `rest` / `graphql` / `soap` catalog entries; **no ERP-specific connector, no ERP field mapping, no ERP reconciliation.** |
| Teams | — | **No dedicated provider.** Teams messages are reachable only through the generic `microsoft365` Graph entry (no `teams.send` operation, no channel-message schema). |
| Slack | — | Catalog entry + `SlackMessage` schema + `slack.send` validation exist, but **no Slack connector class, no inbound event handling, no registry** to resolve a provider to its connector. |
| Gmail send/read | — | Catalog entry + `Email` schema + `email.send` validation exist; no Gmail connector class of its own (generic `adapters.execute_write` path only). |

## 3. Gaps to close (Step V, in order)

1. **Connector registry** — one place that resolves a provider name to its
   connector class, lists supported operations, and answers the required
   scope. (Slice 1, this agent.)
2. **Slack connector** — `verify` (`auth.test`), `slack.send`
   (`chat.postMessage`), inbound event normalisation. (Slice 1, mocked.)
3. **Teams connector** — new catalog operation `teams.send` (Graph
   `POST /teams/{id}/channels/{id}/messages`), channel-message schema,
   inbound webhook normalisation. Needs a Graph scope decision
   (`ChannelMessage.Send`).
4. **Gmail connector class** — thin wrapper over the generic path with
   Gmail-specific error mapping (quota, invalid grant) + label/thread
   handling.
5. **Drive connector class** — thin wrapper over `cloud_connectors`
   exposing the generic registry interface (`verify`, `sync`, file fetch).
6. **ERP connector** — `rest`-based, with a declarative field map
   (invoice/order/customer), idempotency keys, and a reconciliation read
   (submit → read-back → only then mark paid/posted), mirroring the bank
   connector rule: an ambiguous submit is reconciled, never blindly replayed.
7. **Bank connector in the registry** — expose `app/finance/connectors.py`
   through the same registry interface so admin + jobs see one list.
8. **Admin UI** — one connectors page listing every provider, health,
   scopes, last sync, and pending approvals (frontend work, after slices 1–7).

## 4. Connector framework (the rules every connector follows)

- **Code-owned catalog only.** Users cannot install executable adapters
  (`catalog.py` is the allow-list). A provider not in the catalog cannot be
  created (`IntegrationService.create` rejects it).
- **Auth.** OAuth PKCE for Microsoft/Google; bot token (sealed at rest) for
  Slack/Teams; API key / mTLS note for ERP. Credentials are sealed with the
  org cipher, never logged, never returned in API responses
  (`credential_configured: bool` only).
- **Scopes.** Least privilege, declared in the catalog; a write operation
  without its scope is rejected before any network call
  (`WRITE_SCOPES` + `validate_operation`).
- **Transport.** Bounded HTTPS only: official base URL, no redirects, no
  credentials in URLs, 20 s timeout, 2 MB response cap, no response-body
  logging (`app/integrations/transport.py`). 429 → `rate_limited` with
  `Retry-After` (capped 900 s); 5xx on a write → `ambiguous`, never blindly
  retried; 401/403 → credentials rejected, circuit opens.
- **Retries / circuit breaker.** Worker leases jobs (600 s), max 5 attempts,
  exponential back-off; repeated failures open the circuit (`circuit_until`).
  Only definitely-unsubmitted or read-only failures are retriable
  (`IntegrationService.retry`).
- **Idempotency.** Every job carries an `idempotency_key`; re-submission
  with the same key and same action digest returns the original job, a
  different digest conflicts. Every side effect goes through `execute_once`
  with read-back verification (handover rule 4).
- **Approvals.** Every write operation starts `pending_approval`, bound to
  the exact action digest, expiring in 1 h, single use, decided by the
  owner. A write without a live approval raises `approval_required`.
- **Tenancy.** Every lookup/dispatch is scoped by tenant + owner
  (`app/integrations/security.scope`, `authorize`); cross-tenant rows are
  404, never leaked.
- **Webhooks.** HMAC signature with a 300 s timestamp window, 1 MB cap,
  one connector lock, duplicate delivery deduped by external id + digest.

## 5. Trust rules (non-negotiable)

- External content — Slack/Teams messages, emails, Drive documents, ERP
  records, webhook payloads — is **untrusted data**. It is normalised into
  an allow-list of fields only (`normalize_inbound_event`); any field
  claiming a role, permission, approval, payee, or amount outside the
  allow-list is dropped, never acted on.
- A normalised inbound event is always marked `trusted: False`. It can
  start a workflow run or a draft, never a privileged side effect.
- No inbound text can change what an approval covers: approvals are bound
  to the digest created at enqueue time.
- No secret, token, or credential ever appears in a log, error message, or
  test fixture (use `mock-token` / `synthetic-*` placeholders).

## 6. Slice 1 — implemented by this agent

- `app/integrations/registry.py` — `ConnectorRegistry` (register / resolve /
  list / `supports_operation` / `required_scope`), `MockTransport`
  (records calls, returns canned responses, fails closed on uncanned paths),
  `normalize_inbound_event` (allow-list normalisation, always
  `trusted: False`).
- `app/integrations/slack.py` — `SlackConnector` on an injected transport:
  `verify()` (`auth.test`), `send_message(channel, text)` validated by the
  existing `SlackMessage` schema, `receive_event(raw)` normalised via the
  registry helper.
- `tests/test_integrations_registry_slack_unit.py` — 11 DB-free unit tests
  (see §7). No DB touched, no network, no real credentials.

## 7. Test list

**Slice 1 (done, `tests/test_integrations_registry_slack_unit.py`):**

1. Registry lists every catalog provider, incl. `slack`, `gmail`,
   `google_drive`, `microsoft365`.
2. `supports_operation`: Slack supports `verify` + `slack.send`, not
   `email.send`; Gmail supports `email.send`; unknown provider raises.
3. `required_scope`: `slack.send` → `chat:write`; `verify` → none.
4. Handler register/resolve round-trip; duplicate registration raises;
   resolving an unknown provider raises.
5. `MockTransport` records calls in order and returns canned responses.
6. `MockTransport` fails closed on an uncanned path (no accidental real call).
7. `SlackConnector.send_message` validates channel pattern + text length
   and posts exactly one call with the right payload.
8. `SlackConnector.verify` returns team/user from canned `auth.test`;
   `ok: false` raises.
9. `receive_event` normalises to allow-listed fields, marks
   `trusted: False`, and drops hostile fields (`role: admin`,
   `approve: true`) without acting on them.
10. Registry + Slack end-to-end: resolve the handler, send via mock,
    assert the recorded call.
11. `normalize_inbound_event` rejects an unknown provider and a non-object
    payload.

**Slices 2–3 (done, Agent 75):** Teams connector + Gmail connector class
(17 new tests + 15 regressions = 28 passed).

**Slices 4–5 (done, Agent 78, branch `agent/integrations`):**

- Drive connector class `app/integrations/drive.py` — thin wrapper over the
  Drive v3 surface used by `cloud_connectors` (`verify` `/about`, `sync` /
  list files, `fetch_file`, `download_file`), read-only, allow-listed file
  fields, typed auth/quota errors; 9 tests in
  `tests/test_integrations_drive_unit.py`.
- `teams.send` branch added to `adapters.execute_write` (Graph
  `POST /teams/{id}/channels/{id}/messages`, missing id → ambiguous
  `missing_message_identifier`, mirroring the `email.send` provider split);
  3 tests in `tests/test_integrations_teams_write_unit.py`.
- ERP connector `app/integrations/erp.py` + catalog entry `erp` — declarative
  field maps (invoice/order/customer), money via `Decimal` (never float),
  approval-gated submit, submit → read-back reconciliation,
  `ERPAmbiguousError` + `reconcile()` by idempotency key (never blindly
  replayed); 12 tests in `tests/test_integrations_erp_unit.py`.
- Full Step V unit suite: **52 passed, 0 failed** (28 earlier + 24 new).
  `tests/test_integration_connector_schema.py` cannot be collected in this
  environment (pre-existing: it imports `tests/conftest`, which requires
  `DATABASE_URL`; verified identical on the clean base).

**Slice 8 (done except the full-suite gate, Agent 84):**

- Alembic gate: **CLEAN** — upgrade head → downgrade base → upgrade head
  on a throwaway database + `alembic check` clean (single head `0103`).
- Frontend gates: **CLEAN** — `npm test` 26 passed, `tsc --noEmit` exit 0,
  `npm run build` exit 0.
- Full backend suite: **NOT COMPLETED — environmental.** Four attempts
  (single-process ×2, chunked ×1, minus-heavy ×1) all died before a
  verdict: three OOM kills and one runtime-restart kill, VM load 46–62
  with ~30 competing pytest processes; one run reached 82% and its `E`
  wall was diagnosed as the restart killing the private test cluster
  (postmaster), not a code failure. **Zero test failures attributable to
  Step V code in any attempt.** Re-run on a quiet VM with the command in
  `CHANGES-step-V.md` ("Re-run"); target ≥ 1347 passed, 0 failed. The 10
  excluded heavy files are the same ones Agent 8 flagged for a quiet
  re-run on the pre-merge base.
- `CHANGES-step-V.md` written in `assessment/` with the full file list,
  test counts, and the attempts log above.

**Slices 6–7 (done, Agent 84, branch `agent/integrations`):**

- Bank connector in the registry: catalog entry `bank` (`verify` +
  `bank.transfer`) + `app/integrations/bank.py` — `BankConnector` wrapping
  a finance `PaymentConnector` (mock in tests, `get_connector` in
  production): read-only `verify` probe (reported, never raised),
  approval-gated `bank.transfer` with exact-action digest, read-back
  verification, `BankAmbiguousError` resolved only by `reconcile()` —
  never by replay; `ConnectorUnavailable` re-raised unchanged (definitely
  not accepted). Money is `Decimal` only, never float. 23 tests in
  `tests/test_integrations_bank_unit.py`.
- Admin UI connectors page: `IntegrationsWorkspace` in
  `frontend/src/components/ModuleWorkspaces.tsx` now shows (a) a **Pending
  approvals** card — jobs with status `pending_approval` with Approve /
  Reject buttons posting the job digest to the decision endpoint (the
  server re-checks the digest before changing anything); (b) operations +
  scopes on every catalog provider card; (c) a **Bank / payment
  connector** card from `GET /api/finance/connector` (active connector,
  source, availability, problem) — read-only here; changing the mode stays
  in Finance settings. Pure display logic lives in
  `frontend/src/integrations/connectorsView.mjs` (+ `.d.mts`) with 5 tests
  in `frontend/test/connectorsView.test.mjs`.
- Full Step V unit suite: **75 passed, 0 failed** (52 earlier + 23 bank).
  Frontend `npm test`: all pass (26 tests incl. 5 new connectorsView
  tests). Frontend typecheck + build: **verified clean 2026-11-28 ~04:23
  PKT** (`tsc --noEmit` exit 0, `npm run build` exit 0 in 10.19 s) on the
  quiet post-restart VM, after two earlier OOM kills under load.
  `tests/test_integration_connector_schema.py` still cannot be collected
  without `DATABASE_URL` set (pre-existing: it imports `tests/conftest`;
  verified identical on the clean base; it collects fine when
  `DATABASE_URL` is set, as in a full-suite run).

## 8. What remains for Step V

Slices 6–8 in §3 (bank-in-registry, admin UI), then the full-suite gate and `CHANGES-step-V.md`. No live push
without owner approval + verified backup. Real provider credentials are
the owner's to supply later (Slack bot token, Teams app, Google OAuth
client, ERP endpoint + key) — this step never asks for them and never
stores them.
