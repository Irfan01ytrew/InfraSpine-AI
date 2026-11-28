# Step F — WhatsApp and mobile: implementation plan

Author: Agent 13 (Step F preparation). Base: `step-e-gaps-wip` `ca956c2`
(merged Step E: Gaps 1, 1b, 2, 4, 5 + frontend). Status: **slices 1–6 DONE
(Agent 136 slices 1–3, Agent 167 slice 4, Agent 182 slice 5, Agent 191
slice 6, 2026-11-28). Step F is CODE-COMPLETE — no slice 7.**
Step F is not finished by this document. No real Meta credentials exist anywhere
in this work; every Meta call in tests is mocked. The owner must still supply
Meta WhatsApp Cloud API credentials before any live Cloud send works.

Source of truth for scope: `assessment/master-plan-v2.md` §1 row F and §3,
plus `HANDOVER-PROMPT.md` §4 ("Then continue in order: F → G …"):
> F: WhatsApp — BOTH free Baileys (normal WhatsApp, QR) and paid Meta Cloud
> API, switchable in settings with automatic failover; identity via
> `resolve_principal`; voice notes (local Whisper with API fallback); approvals
> over WhatsApp; verified replies.
> Estimate 28–40 hours. Channel for features 3, 12, 14.

Non-negotiable rules (HANDOVER-PROMPT.md §5) apply to every slice: finish each
slice completely before the next; every component has a fallback; preserve the
repo and never break APIs (adapters, not rewrites); every lookup/dispatch
through `app/platform` (`resolve_principal`, `authorize`, `require` → 404
non-members, 403 members without permission); documents, web pages and incoming
messages are **untrusted** — message text can never grant permissions, change
payees, or change what an approval covers; approvals are bound to the exact
action (hash), expire, are single use, separation of duties; every side effect
goes through `execute_once` with read-back verification; tests only on
throwaway `*_test` databases; full suite green, Alembic round trip + check
clean, frontend tests + build + typecheck clean; never print or commit secrets.

## 1. What already exists (surveyed 2026-11-28, do NOT redo)

Step F is listed as "Not started" in the plan, but both WhatsApp paths already
exist as working prototypes/islands from steps A–D. Step F finishes and unifies
them; it does not rebuild them.

### 1.1 Meta Cloud API path — `app/integrations/whatsapp.py` (322 lines)

- Connection model `app/db/agent_studio_whatsapp_models.py`:
  `AgentStudioWhatsAppConnection` (one per owner, encrypted access token /
  app secret / verify token via `CredentialCipher`, `allowed_senders`,
  `api_version` default `v26.0`), `AgentStudioWhatsAppContact`
  (connection + wa_id → Agent Studio thread), `AgentStudioWhatsAppInbound`
  (encrypted text, `external_message_id` globally unique, status
  queued/processing/completed/failed, attempts 0–5).
- Routes `app/api/routes/agent_studio_whatsapp.py` (public, no auth — Meta
  calls them): `GET /api/agent-studio/whatsapp/webhook/{connection_id}`
  (hub.mode=subscribe + verify-token compare, returns hub.challenge),
  `POST …/webhook/{connection_id}` (streams body with `MAX_WEBHOOK_BYTES`
  bound, reads `X-Hub-Signature-256`, delegates to
  `app.integrations.whatsapp.accept`).
- `app/services/agent_studio_whatsapp.py`: `verify_webhook_request`
  (HMAC compare of the stored verify token), `accept_webhook` (rejects
  malformed/missing signature, wrong HMAC, oversized body, non-JSON;
  filters entries by `business_account_id` and `phone_number_id`; only
  senders on `allowed_senders` are stored; text + interactive button/list
  replies flattened to text; dedup via `on_conflict_do_nothing` on
  `external_message_id`; text sealed with the cipher before storage),
  `verify_and_subscribe` (live Meta `GET {phone_number_id}` + subscribe —
  the only live Meta calls, admin-only), `_meta_request` (bounded HTTPS).
- Worker `app/integrations/whatsapp.py`: `accept` (stores inbound + creates
  `WhatsAppDispatch` rows, applies delivery receipts with monotonic rank
  sent → delivered → read), `claim` (fencing, stale-processing recovery,
  max 5 attempts), `process` (re-binds identity on every step, enforces the
  24-hour messaging window, sends a progress message, runs the task through
  `AgentStudioService.start_run(..., idempotency_key=f"whatsapp-cloud:{inbound.id}", execute_inline=True)`,
  seals the reply before sending, delivers artifacts, sends the final reply
  exactly once via `send_text_once` → `dispatch_once` /
  `execute_once` with duty `channel.reply`; ambiguous failures leave the
  dispatch `uncertain`, never double-sent). Registered in `app/main.py`
  lifespan as `agent-studio-whatsapp-worker`.
- Receipts: `tests/test_irfanix_whatsapp_receipts.py`.

### 1.2 Baileys (free, normal WhatsApp, QR) path

- `app/services/agent_studio_baileys.py` (418 lines) + routes
  `app/api/routes/agent_studio_baileys.py` (authenticated, owner-scoped):
  connect (QR), status, disconnect, cancel queued command. Bridge in
  `integrations/whatsapp-baileys/` (Node). Worker
  `run_baileys_worker` in `app/main.py` lifespan. Self-chat mode, commands
  queue (`BaileysMessage`), identity via `resolve_whatsapp_sender`
  (`channel="whatsapp_baileys"`, `auto_link_owner` for the self-chat).
- UI: `frontend/src/components/AgentStudioWhatsAppQrPanel.tsx`.

### 1.3 Identity — `app/platform/channels.py`

- `resolve_whatsapp_sender(db, channel, sender, allowed, owner_id)` resolves
  a sender to a platform principal through `platform_channel_identities`
  (via `resolve_principal`); an unmapped number gets no data; the connection
  allow-list is a second gate. `link_identity` never silently reassigns a
  number linked to someone else. `sync_allowed_senders` mirrors legacy
  allow-list numbers to the connection owner as
  `verification_method=legacy:allowed_senders` — the file itself notes this
  lasts "until the step F verification flow".
- `app/integrations/whatsapp.py::bound_identity` re-binds the sender, the
  user (must be active) and the agent (must exist + enabled) on every worker
  step; a revoked owner/identity/agent raises `identity_revoked` and the
  inbound is never executed.

### 1.4 Unified runtime (Step E, Gap 1) — the intake already lands here

- `AgentStudioService.start_run` mirrors every run to the unified engine
  (`_mirror` → `app/agent_studio/unified.py::mirror_new_run`, creation mirror
  plus the pending sweep). The mirrored run is `origin='studio'`,
  `origin_ref=<studio run id>`, executed through the `agent_studio.execute`
  handler with `execute_once` per attempt (`agent_studio:<run>:attempt:<n>`,
  duty `agent.run`, read-back verified). Pause (step A) is honoured before
  every attempt, including WhatsApp entry points (`_paused_now` in
  `start_run`); stopping the workflow run cancels a queued or
  approval-waiting Studio run (`on_workflow_cancelled`).
- So a WhatsApp inbound is **already** a unified workflow run — but no test
  proved it end to end before slice 1 (Gap 1 tests prove API-created runs
  only). Slice 1 adds that proof (see §3).
- `wf_definitions.origin` is check-constrained to
  `('workflow','automation','system','studio','governance')` (migration
  `0103`). A dedicated `origin='whatsapp'` would need a migration widening
  that check; slice 1 deliberately does **not** add one — WhatsApp intake
  uses the existing `origin='studio'` mirror.

### 1.5 UI

- `frontend/src/components/AgentStudioWhatsAppPanel.tsx`: a QR/Cloud mode
  toggle that only switches which panel is shown, plus connection setup,
  verify-and-subscribe, and status. It is **not** a runtime path switch and
  there is no failover. No WhatsApp settings exist in admin / Control Center
  (master-plan §3: "Settings for WhatsApp path (Baileys vs Cloud, failover)
  do not exist until F").

## 2. What is missing (the Step F work list)

| # | Gap | Evidence | Slice |
|---|-----|----------|-------|
| 1 | No test proves a verified WhatsApp webhook becomes exactly one unified workflow run (incl. duplicate delivery, unmapped sender, untrusted text) | Gap 1 tests cover API runs only; `tests/test_irfanix_cloud_webhook.py` has 1 test (queue bounds + auth) | **1 (this task)** |
| 2 | No sender verification flow beyond the legacy allow-list mirror | `app/platform/channels.py` docstring: "until the step F verification flow" | 2 |
| 3 | ~~No runtime path setting (Baileys vs Cloud) and no automatic failover~~ — **DONE slice 3 (Agent 136)**: per-owner setting (`cloud`/`baileys`/`auto`, default `auto`) in `whatsapp_path_settings` + `app/integrations/whatsapp_failover.py`; failover once on the other path under the same idempotency key, audited | Panel toggle is UI-only; only `app/finance/connectors.py` has a failover connector to copy the pattern from; no `org_settings` / settings field for the WhatsApp path | 3 |
| 4 | No voice notes on either path (Cloud non-text kinds get "[NEEDS INPUT] resend as text"; no audio/Whisper wiring in any WhatsApp file) | grep: no `audio`/`voice`/`whisper` in `app/integrations/whatsapp.py`, `app/services/agent_studio_whatsapp.py`, `app/services/agent_studio_baileys.py` | 4 |
| 5 | No approvals over WhatsApp (awaiting-approval only tells the user to open the web UI) | `app/integrations/whatsapp.py:246`, `app/services/agent_studio_whatsapp.py:477`, `app/services/agent_studio_baileys.py:314` | 5 |
| 6 | No "Done (verified)" wording contract on WhatsApp replies (replies say `[SUCCESS]` / `[FAILED]` / `[IN PROGRESS]`) | master-plan §12 acceptance: "WhatsApp / UI copy says 'Done (verified)' only after verify succeeds (F)" | 5 |
| 7 | No per-sender rate limiting / abuse control on webhook intake (only payload-size bound + 5 attempts) | `accept_webhook` has no rate limit | 2 |
| 8 | No interactive/template handling for the 24-hour window (only an expiry error; no template message to re-open the window) | `process()` raises `messaging_window_expired` | 6 |
| 9 | ~~Baileys bridge health/QR-expiry handling is unproven in tests; no failover trigger when the bridge is down~~ — **DONE slice 3 (Agent 136)**: dead bridge fails over to Cloud once (test proves it); live-session check is `online` + process alive | `bridge_available()` 503 path only | 3 |
| 10 | ~~No admin/Control-Center settings surface for the WhatsApp path~~ — **DONE slice 3 (Agent 136)**: `PUT /whatsapp/path` + path card in the Agent Studio WhatsApp panel (active path, selector, availability) | master-plan §3, §14 | 3 |

Owner still needed (unchanged): Meta WhatsApp Cloud API credentials
(app secret, access token, verify token, phone-number ID, business account
ID) for any live Cloud send; a phone with normal WhatsApp for the Baileys
path; nothing else new.

## 3. Slice 1 — done in this task (webhook verification + intake as a workflow run)

Scope: **prove, don't rebuild.** The verification and intake code already
exists (§1.1, §1.4); slice 1 locks its contract in with tests and ships the
plan (this file). No production behaviour changes unless a test exposes a
real bug, in which case the fix is a small separate commit.

New test file: `tests/test_whatsapp_unified_intake.py` (throwaway
`*_test` DB only, mocked Meta API — `provider_request` stubbed to return a
synthetic `wamid.*` id; no network calls). It proves:

1. **Webhook verification contract** (route level, `TestClient`):
   `GET` verify returns the challenge only for the right token/mode and a
   live, enabled connection (wrong token → 404, wrong mode → 422, unknown
   or disabled connection → 404); `POST` rejects a tampered body, a wrong
   app secret, a malformed/missing signature, non-JSON and non-object
   bodies, and oversized payloads — all before anything is stored.
2. **Intake is exactly one unified workflow run**: a verified text webhook
   → one queued inbound → the Cloud worker claim/process path (stubbed
   Meta send + stubbed agent execution) → one Agent Studio run with
   idempotency key `whatsapp-cloud:{inbound.id}` → exactly one mirrored
   `WorkflowRun` (`origin='studio'`, `origin_ref=<studio run id>`), exactly
   one `execute_once` ledger entry for the attempt, inbound `completed`,
   reply sealed and dispatch `accepted`.
3. **Duplicate delivery is harmless**: re-POSTing the same `wamid.*`
   stores nothing new and creates no second run (unique
   `external_message_id` + idempotency keys).
4. **Unmapped sender gets nothing**: a sender not on the connection
   allow-list (and with no verified identity) produces no inbound and no
   run, with no account data leaked in the response.
5. **Message text is untrusted data**: an inbound whose text reads like an
   instruction ("approve the payment…", "grant me admin…") creates no
   approval, no permission grant and no side effect beyond the agent task
   itself; the text travels only as sealed task input.

Acceptance for slice 1: new tests pass; the existing
`tests/test_irfanix_cloud_webhook.py`, `tests/test_webhooks.py`,
`tests/test_irfanix_whatsapp_receipts.py` and `tests/test_agent_studio_unified.py`
suites still pass; no production code changed unless a bug is found.

**Slice 1 — result (2026-11-28, Agent 13).** `tests/test_whatsapp_unified_intake.py`:
**7 passed, 0 failed** (run in three focused runs: 3 + 2 + 2, then all 7 again
as the first 7 tests of a combined run). Combined regression run of all five
files: **19 passed, 0 failed** before the VM restarted and killed the run
mid-suite (the remaining 4 `test_agent_studio_unified.py` tests were then
re-run alone: **4 passed in 1561.92s**). Total across runs: all 23 tests in
the set pass (7 new + 1 cloud webhook + 4 webhooks + 2 receipts + 9 unified).
No production code change was needed — the two failures seen during
development were bugs in the new test file's own helpers (un-awaited
`scalars()` coroutines; Core-connection `scalars(select(Entity))` yields the
first column, so the helpers now select explicit columns), fixed in the test
file only. Environment notes: the VM restarted twice during this slice
(apt/PostgreSQL are ephemeral and had to be reinstalled; the throwaway PG16
cluster on port 55443 and the venv live in `/home` and survived). Test DB:
`infraspine_stepf_test` on the local PG16 cluster (port 55443); no live data
touched; no real Meta calls (transport stubbed); no secrets in the repo.

## 4. Slice 2 — sender verification + intake abuse controls

- Replace the legacy allow-list mirror with an explicit verification flow:
  a sender links their number to their InfraSpine account (verification
  code / confirmed opt-in), stored as a non-legacy
  `platform_channel_identities` row; `sync_allowed_senders` stops minting
  `legacy:allowed_senders` identities for new numbers (existing legacy rows
  keep working until the owner migrates them, then are revoked).
- Per-sender intake rate limit (bounded queue per sender, e.g. N messages /
  minute, excess rejected with 429-style handling and audit, never stored
  as tasks).
- Tests: unverified sender → no run; verified sender → run as themselves
  (never as the connection owner); revoked identity → run refused even if
  the number is still on the allow-list; rate-limit breach → no task queued.
- Files: `app/platform/channels.py`, `app/services/agent_studio_whatsapp.py`,
  new `tests/test_whatsapp_sender_verification.py`. No migration unless the
  identity table needs a column (prefer reuse).

## 5. Slice 3 — path setting + automatic failover (Baileys ↔ Cloud) — DONE (Agent 136, 2026-11-28)

Commits on `agent/stepf-whatsapp`: `ddf3ed1` (model + migration `0104_whatsapp_path` + adapter), `d07a4a8` (worker wiring), `0ece359` (API + panel card), `dda2988` (registry-tuple fix, see below), `854a1e4` (tests). 20 new tests in `tests/test_whatsapp_failover.py`, all passing (8 adapter units + 12 DB/API/worker; re-run `12 passed` after fixes).

What was built (per-owner table, not an `org_settings` column — the plan's
primary option, correct scope for per-owner connections):

- `WhatsAppPathSetting` (`whatsapp_path_settings`, migration
  `0104_whatsapp_path`, down `0103_unified_studio_governance` — **0104
  collides at merge, renumber**); `app/integrations/whatsapp_failover.py`
  (`resolve_primary`, `get/set_path_setting`, `CloudTransport`,
  `BaileysTransport`, `send_with_failover`, `live_baileys_session`).
  Failover only on errors that confirmed nothing (5xx, no message id,
  connection failure, 429, 401/403); `provider_rejected` is not wrapped,
  so a send that may have been accepted is never retried on the other
  path. Baileys timeout is ambiguous (bridge may deliver late).
- Both workers wired through the adapter inside `dispatch_once`, so the
  same idempotency key covers both legs. Cloud worker: `process()` /
  `send_text_once()` take the shared `app.state.baileys_sessions`
  registry (created in `main.py` lifespan). Baileys worker:
  `send_reply_once()` fails over to Cloud for phone JIDs only (`@lid`
  has no Cloud route). Failover audited as
  `integration.whatsapp_failover` (outcome success/failure).
- API: `GET /whatsapp` returns `path_setting` / `active_path` /
  `cloud_available` / `baileys_available`; `PUT /whatsapp/path`
  (owner-scoped, schema-validated, audited on change). Panel: path card
  above the mode toggle (visible in both modes).
- Real bug fixed by the tests (`dda2988`): the Baileys supervisor stores
  `(bridge, task)` tuples in the registry, but `live_baileys_session`
  read the value as a bare session — in production Cloud→Baileys
  failover would have raised `AttributeError` and never run. The tests
  also caught two test-harness bugs of their own (nested `run_db` calls;
  `run_db` hands a bare Connection, the setting helpers need a Session).

§9 gates status (owed from Agent 95, attempted by Agent 136): frontend
`npm test` rc=0 and typecheck rc=0 after `npm ci`; frontend **build
OOM-killed (rc=137)** — re-run owed on a quiet VM. Backend
`test_governance_workflow.py` + `test_workflow_engine.py` run died
after 2 dots with no summary (VM load) — re-run owed. Neither is faked
green; slice 3's own 20 tests are green, the gates are not.

Owner decisions still open (unchanged): Meta Cloud API credentials + a
Baileys phone; whether verified non-owners may use the connection's
agent (owner-only today, locked in by test).

- Add a per-owner (later per-org) WhatsApp path setting: `cloud`,
  `baileys`, `auto` (default `auto` once both are configured), stored with
  the connection / org settings, editable in admin settings and the Agent
  Studio WhatsApp panel. Migration only if a new column/table is needed.
- Failover: when the active path's send fails with a retryable provider
  error (or the Baileys bridge is down), the dispatch is retried once on
  the other path with the same idempotency key — never a duplicate message.
  Copy the `FailoverPaymentConnector` pattern from
  `app/finance/connectors.py` (unusable fallback is dropped, usable primary
  kept). Failover events are audited; the active path is shown in admin
  settings (same pattern as the Gap 4 bank-connector display).
- Tests: Cloud send 500 → Baileys send once; Baileys bridge down → Cloud
  send once; both down → dispatch `uncertain`, no duplicate; path setting
  respected; admin UI shows the active path.
- Files: new `app/integrations/whatsapp_failover.py` (adapter), settings
  surface in `app/api/routes/agent_studio_whatsapp.py` /
  `agent_studio_baileys.py` + frontend panel, `tests/test_whatsapp_failover.py`.

## 6. Slice 4 — voice notes (local Whisper, API fallback)

- Cloud `audio` messages and Baileys voice messages: download the media
  (bounded size, Meta media URL via the Cloud API / bridge), transcribe
  with local Whisper first, API transcriber as fallback (same fallback rule
  as the rest of the platform), then feed the transcript as the task text
  with `message_type='voice'` and the transcript marked as machine text.
  The audio file is removed after transcription (same rule as the existing
  voice-question upload tests).
- Voice replies stay text in this slice (voice replies are a later
  enhancement, not Step F).
- Tests: mocked media download + stubbed transcriber → transcript becomes
  the task; transcription failure → "[NEEDS INPUT]" reply, no crash; audio
  file deleted; oversized audio rejected.
- Files: `app/integrations/whatsapp_voice.py`, wiring in both workers,
  `tests/test_whatsapp_voice.py`. Depends on the existing voice upload
  helpers (`tests/test_voice_question_upload_unit.py`).

**Slice 4 — DONE (Agent 167, 2026-11-28). 20 new tests, all passing**
(`.venv/bin/python -m pytest tests/test_whatsapp_voice.py -q
-p no:cacheprovider` → `20 passed in 1876.38s`, EXIT=0, clean
single-process run after a VM reboot; the earlier 1-failed/4-errors run
was two problems since fixed/explained: a real def-time-default bug in
`download_cloud_audio`, and 4 setup errors from two pytest processes
sharing one test database). What was built:

- New `app/integrations/whatsapp_voice.py`: `suffix_for_mime` /
  `mime_for_suffix`, `download_cloud_audio` (Meta metadata via the
  transport, bytes via a fresh client pinned to the media URL origin;
  host must be `graph.facebook.com` or end `.fbcdn.net`; declared
  `file_size` checked before fetching; stream capped at 8 MB),
  `decode_baileys_audio` (base64, same 8 MB bound), `transcribe_voice_note`
  (local Whisper first, API transcriber only when local is *unavailable*;
  unintelligible audio is never sent to the API; staged audio always
  removed), `mark_machine_text` (`[Voice note transcript] ` prefix).
- `app/providers/openai_compatible.py`: `supports_audio_transcription` +
  `transcribe_audio` (OpenAI-compatible `/audio/transcriptions`, model
  `whisper-1`; DeepSeek builtin and Gemini excluded — no audio endpoint).
  `app/services/llm_runtime.py`: `get_audio_transcription_client` resolves
  the platform fallback provider only (empty = no fallback).
- Cloud worker (`app/integrations/whatsapp.py`): `audio` messages store
  the Meta media id in the sealed text field (no migration); the worker
  transcribes, sets `inbound.message_type = "voice"`, runs the agent on
  the marked transcript; any failure → "[NEEDS INPUT]" reply, inbound
  still completes, no retry, no crash. `app/services/agent_studio_whatsapp.py`:
  `_incoming_events` / `stored_text` admit `audio`.
- Baileys worker (`app/services/agent_studio_baileys.py` + bridge):
  voice notes carry no text so they need the 5-min follow-up window after
  a text activation ("/agent" / "irfanix"); the bridge downloads via
  `downloadMediaMessage(message, 'buffer')` and forwards base64 audio
  (or `audioTooLarge`); the worker transcribes and runs the agent, or
  answers "[NEEDS INPUT]" on failure. `event()` admits audio events;
  `execute()` gained an audio branch and the text branch now calls the
  extracted `_run_agent_task` (behavior-preserving).
- Voice replies stay text in this slice. No migration in this slice.
- Real bug fixed by the tests: `download_cloud_audio` bound
  `provider_request` as a def-time default, so the test monkeypatch never
  took effect and the Cloud path called the real Meta API — now resolves
  its transport callables at call time (production behavior unchanged).
- Gates-3 verdict (Agent 136's 3rd attempt, log
  `/home/hatch/agent136-gates3.log`, 58 min): **3 failed, 15 passed** —
  `test_duplicate_webhook_delivery_creates_no_second_run`,
  `test_untrusted_message_text_cannot_approve_or_grant_anything`,
  `test_whatsapp_receipts_are_scoped_to_the_reply_and_never_regress`.
  The basic text-intake test passed on the same run and the receipts
  failure is in code this slice did not touch; both runs shared one test
  database with this slice's own test run, so the failures are suspected
  cross-run interference, not a slice-4 regression — **confirmed**: clean
  single-process re-run of exactly those three tests passes
  (`3 passed in 229.81s`, EXIT=0, log `/home/hatch/tmp-agent167-gates-recheck.log`).
- Regression gates on the final HEAD (all single-process, clean runs):
  slice-3 failover `20 passed in 1005.41s`; cloud-webhook + webhooks +
  studio-unified `13 passed` plus 1 failure in the cloud-webhook file —
  a stale slice-1 stub (see below); governance + workflow-engine
  `9 passed in 688.26s`. Two pre-existing test-only breakages found and
  fixed by the gates (commit `90029d5`, no production change): the
  `baileys_sessions=` keyword (slice 3) vs `(*_args)`-only worker stubs
  in `test_irfanix_cloud_webhook.py` and
  `test_irfanix_cloud_agent_execution.py`, and the agent-execution test
  never linking its sender (slice-2 identity rule → `identity_revoked`;
  now links via `link_identity`, as the unified-intake tests do).
  Frontend gates (npm test + build + typecheck) still owed from slice 3
  — this slice touches no frontend code; full backend suite still owed
  before merge, as for all steps.
- Owner decisions still open: API transcription fallback provider must
  serve a Whisper-compatible model as `whisper-1`; Baileys voice notes
  need a text activation first, then the voice note within 5 minutes.

## 7. Slice 5 — approvals over WhatsApp + "Done (verified)" wording

- When a run is `awaiting_approval`, send an approval card over the same
  channel (interactive buttons on Cloud, text commands on Baileys) bound to
  the exact action hash; the reply is verified as coming from an authorised
  approver (identity from §4, separation of duties — the requester cannot
  approve their own action); approve/reject decides the run exactly as the
  web UI decision does, single use, expiring.
- Reply wording: `[SUCCESS]` becomes "Done (verified)" **only** after the
  `execute_once` read-back verify succeeds; a model utterance of "done"
  without a verify result never changes workflow state (master-plan §12).
- Tests: approve-by-WhatsApp completes the run; reject fails it; a forged
  / replayed / wrong-sender approval is refused; wording is "Done
  (verified)" only on verified completion.
- Files: both workers + `app/platform/approvals.py` reuse,
  `tests/test_whatsapp_approvals.py`.

**Slice 5 — DONE (Agent 182, 2026-11-28). 16 new tests, all passing**
(`.venv/bin/python -m pytest tests/test_whatsapp_approvals.py -q
-p no:cacheprovider` → `16 passed in 614.45s`, EXIT=0, clean single-process
run). Regressions in the same process afterwards: `test_whatsapp_unified_intake.py`
+ `test_whatsapp_voice.py` + `test_irfanix_cloud_agent_execution.py` +
`test_irfanix_cloud_webhook.py` + `test_irfanix_whatsapp_receipts.py` +
`test_webhooks.py` → 34 passed, 1 failed (a real bug: the new wording helper
assumed every run has an `id`; the artifact test's fake run does not — fixed
with a defensive `run_verified` guard, single test re-run passed);
`test_agent_studio_unified.py` + `test_governance_workflow.py` +
`test_workflow_engine.py` → 18 passed. What was built:
`app/integrations/whatsapp_approvals.py` (parse Cloud button ids +
Baileys APPROVE/REJECT commands; exact-action hash via `ActionSpec`;
card text/buttons; `decide_from_whatsapp` through `AgentStudioService.decide`;
`run_verified`/`completed_reply`); Cloud worker sends the card as an
interactive message (`send_interactive`/`send_interactive_once`, Baileys leg
sends the card text via the failover adapter) and intercepts decision
replies; Baileys worker sends the card as text and intercepts decisions;
webhook flattening keeps `wa-approve:`/`wa-reject:` button ids;
`whatsapp_artifacts.py` strips either marker and only claims
"Done (verified)" on a verified completion. No migration (no new table);
no `permission_catalog` change; `bridge.mjs` untouched. Test-only bugs
found while writing the tests (all in the tests, not production): the
approval seal must use the Agent Studio AAD namespace, not the WhatsApp
legacy one; `setup_scope` must run once per test (unique group name);
hash comparisons must fix the run id too; `decide()` queues a rejected run
(the engine terminates it — the test asserts queued + rejected, then a
terminal run with the approval still rejected). Observed gaps (not fixed):
WhatsApp runs execute inline, which bypasses the engine's `execute_once`
dispatch, so no `PlatformActionExecution` row exists for them and the
"Done (verified)" stamp only appears for runs that went through the engine
path; voice-note transcripts carry the machine-text prefix, so a spoken
approval command never parses (typed/button decisions only). Frontend
gates not run (no frontend change in this slice — owed at merge);
full backend suite before merge is owed (integration step).

## 8. Slice 6 — 24-hour window templates + hardening + CHANGES-step-F.md

- Template messages (utility, pre-approved) to re-open an expired 24-hour
  window; template sends also go through `execute_once`.
- Hardening: webhook replay window (reject messages with timestamps too
  far in the future/past — the Cloud worker already clamps future
  timestamps), per-connection webhook secret rotation, audit completeness
  check.
- Docs: `CHANGES-step-F.md` (plain language, file list, test counts),
  `docs/irfanix-release-checklist.md` update, frontend tests + build +
  typecheck, full backend suite green, Alembic round trip + check clean,
  merge to `main` only when green. Live push is a separate owner-approved
  step (backup first, never downgrade on live).

**Slice 6 — DONE (Agent 191, 2026-11-28). 21 new tests, all passing**
(`.venv/bin/python -m pytest tests/test_whatsapp_templates.py -q
-p no:cacheprovider` → `21 passed in 650.16s`, EXIT=0, clean single-process
run). What was built: `app/integrations/whatsapp_templates.py` (template
catalogue — `task_ready`, validate/render/summary) +
`app/integrations/whatsapp_audit.py` (audit-completeness check) +
`app/integrations/whatsapp.py` (`send_template`/`send_template_once`,
`_reopen_expired_window` on both 24h-window checks, timestamp hardening in
`accept()`: future/unparseable → arrival time, >7 days old → `stale_message`
at intake) + `app/integrations/whatsapp_failover.py` (`template=` threaded
through `send_with_failover`; Cloud sends the structured template, Baileys
sends the rendered text) + `app/services/agent_studio_whatsapp.py`
(`rotate_connection_secret`) + `POST /whatsapp/rotate-secret` route +
`app/integrations/whatsapp_voice.py` (`strip_machine_text_prefix`) +
`app/services/agent_studio_baileys.py` (spoken APPROVE/REJECT decided from
the raw transcript before running the agent). Two slice-5 gaps CLOSED:
spoken approval commands now parse (prefix stripped in both Cloud and
Baileys paths). One slice-5 gap NOT fixed: WhatsApp runs still execute
inline, bypassing `execute_once` — no `PlatformActionExecution` row, so
"Done (verified)" only appears for engine-path runs (follow-up for the
integration / engine owner). Regressions re-run in slice 6 (one process per
chunk, sequential): intake 7 + voice 20 + failover 20 + approvals 16 = 63
passed; cloud webhook + webhooks + receipts + studio-unified = 16 passed;
governance workflow + workflow engine = 9 passed — **88 passed, 0 failed**.
The 3 failover failures in the first chunk were a latent slice-5 test drift
(the failover test stub did not accept the `interactive` argument slice 5
threads through `send_with_failover`; same stub fixed the same way in the
integration merge) — fixed in the test file only, which now also accepts
`template`. Alembic roundtrip + check clean on a fresh UTF8 throwaway DB
(base → head `0104_whatsapp_path`, downgrade −1, upgrade, check reports "No
new upgrade operations detected"); no new migration. Frontend gates run
(no frontend change in this slice): npm test 22 passed, tsc exit 0, vite
build exit 0. Full backend suite before merge is owed (integration step).

## 9. Test list for the whole step (acceptance)

- Slice 1: `tests/test_whatsapp_unified_intake.py` (§3).
- Slice 2: sender verification + rate limits.
- Slice 3: failover matrix (Cloud down / Baileys down / both down) — DONE, `tests/test_whatsapp_failover.py`, 20 tests passing (Agent 136).
- Slice 4: voice transcription incl. failure + cleanup + size bound.
- Slice 5: WhatsApp approvals incl. forgery/replay/wrong-sender refusal;
  "Done (verified)" wording only after verify.
- Slice 6: template re-open, replay rejection, secret rotation.
- Regression gates every slice: `test_irfanix_cloud_webhook.py`,
  `test_webhooks.py`, `test_irfanix_whatsapp_receipts.py`,
  `test_agent_studio_unified.py`, `test_governance_workflow.py`,
  `test_workflow_engine.py`, frontend `npm test` + build + typecheck.
- Manual (owner): link a real phone via Baileys QR, send a task, verify the
  same answer on phone and dashboard; Meta Cloud verify-and-subscribe with
  real credentials; founder Roman Urdu voice note end to end (feeds Step H).
