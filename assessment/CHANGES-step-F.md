# Step F — WhatsApp control for Agent Studio: what changed

Plain-language record of the whole step. WhatsApp control means: the owner
sends a message or a voice note to the company's WhatsApp number, the
software does the work, and the answer comes back on WhatsApp. Approvals
can also be asked and answered on WhatsApp ("should I approve this
invoice?" → "yes"). Two ways to connect: **Meta Cloud API** (a WhatsApp
Business number + Meta credentials) or **Baileys** (free, the owner's normal
WhatsApp number, connected by scanning a QR code like WhatsApp Web — the
owner chose Baileys on 2026-11-28). Nothing in this step moves money or
grants permissions by itself: every privileged action still needs an
approval bound to the exact action, and message text from WhatsApp is
treated as untrusted data, never as an instruction to the system.

## Slice 1 — webhook verification + intake as one workflow run (Agent 13)

Proved the intake contract with tests instead of rebuilding it. A verified
Meta webhook becomes exactly one unified workflow run (idempotency key
`whatsapp:{inbound.id}`), with exactly one mirrored `WorkflowRun` and one
`execute_once` ledger entry. Duplicate deliveries are harmless (same
`wamid.*` stores nothing new, creates no second run). An unmapped sender
gets nothing and no account data leaks in the response. A message whose
text reads like an instruction ("approve the payment…", "grant me
admin…") creates no approval, no permission grant, and no side effect
beyond the agent task itself.

- Tests: `tests/test_whatsapp_unified_intake.py` — **7 passed, 0 failed**.
- No production code changed (the two failures seen during development were
  bugs in the new test file's own helpers, fixed in the test file only).

## Slice 2 — sender verification + intake abuse controls (Agent 95)

A sender links their number to their InfraSpine account through an
explicit verification flow (verification code / confirmed opt-in), stored as
a `platform_channel_identities` row. New numbers no longer get the legacy
allow-list mirror; existing legacy rows keep working until the owner
migrates them, then are revoked. Per-sender intake rate limit: 10 messages
per minute per sender per connection; excess is rejected, never stored as
a task. An unverified sender gets no run; a verified sender runs as
themselves, never as the connection owner; a revoked identity is refused
even if the number is still on the allow-list.

- Tests: `tests/test_whatsapp_sender_verification.py` — **8 new tests,
  all passing** (the file holds 9 test functions today).
- Files: `app/platform/channels.py`,
  `app/services/agent_studio_whatsapp.py`.

## Slice 3 — per-owner path setting + automatic failover (Agent 136)

Each owner picks a transport path — Cloud, Baileys, or auto (default) —
stored per owner in `whatsapp_path_settings` (migration
`0104_whatsapp_path`; **0104 collides at merge — renumber**). When the
primary path fails with an eligible error, the other path is tried exactly
once under the same idempotency key; when both fail, the primary's error is
raised so the caller's existing retry / "uncertain" handling applies.
Real bug the tests caught: the Baileys supervisor stores `(bridge, task)`
tuples but `live_baileys_session` read a bare session — a Cloud→Baileys
failover would have raised `AttributeError`; fixed in `dda2988`.

- Tests: `tests/test_whatsapp_failover.py` — **20 passed, 0 failed**
  (8 adapter units + 12 DB/API/worker; re-verified 20/20 in slice 6).
- Files: `app/integrations/whatsapp_failover.py`, migration
  `0104_whatsapp_path`, API `PUT /whatsapp/path`, admin panel card.

## Slice 4 — voice notes (Agent 167)

Voice notes are transcribed locally with Whisper first; the provider API is
used only when local transcription is unavailable, and unintelligible audio
is never sent to the API. Staged audio files are always removed after use.
Cloud downloads are pinned to Meta origins (`graph.facebook.com`,
`.fbcdn.net`), the declared size is checked, and the size cap is 8 MB.
Baileys voice notes only work inside the 5-minute follow-up window after a
text activation (`/agent` or `irfanix`). Any transcription failure becomes
`[NEEDS INPUT]` — the inbound completes, nothing crashes or retries.
Voice replies stay text. **Note for the live push: `bridge.mjs` changed in
this slice — the owner's Windows host bridge needs redeploying.**

- Tests: `tests/test_whatsapp_voice.py` — **20 passed, 0 failed**
  (re-verified 20/20 in slice 6).
- Files: `app/integrations/whatsapp_voice.py`, provider audio-transcription
  capability (`OpenAICompatibleClient.transcribe_audio`, model `whisper-1`,
  never the chat model; DeepSeek builtin and Gemini excluded — no audio
  endpoint), `integrations/whatsapp-baileys/bridge.mjs`.

## Slice 5 — approvals over WhatsApp + "Done (verified)" wording (Agent 182)

When a run is `awaiting_approval`, an approval card is sent over the same
channel — interactive buttons on Cloud (`wa-approve:` /
`wa-reject:<uuid>:<hash12>`), text commands on Baileys
(`APPROVE` / `REJECT <uuid> [hash12]`) — bound to the exact action hash.
The reply is verified as coming from an authorised approver, with
separation of duties (the requester cannot approve their own action);
each decision is single-use and expires after 1 hour. Approving or
rejecting decides the run exactly as the web UI decision does. Reply
wording: `[SUCCESS]` becomes **"Done (verified)" only after the
`execute_once` read-back verify succeeds**; a model utterance of "done"
without a verify result never changes workflow state. Real bug the tests
caught: the wording helper assumed every run has an `id` — fixed with a
defensive guard.

- Tests: `tests/test_whatsapp_approvals.py` — **16 passed, 0 failed**
  (re-verified 16/16 in slice 6).
- Files: `app/integrations/whatsapp_approvals.py` (+ Cloud worker
  `send_interactive` / `send_interactive_once`, failover adapter threading,
  webhook button-id flattening, Baileys command interception).
- No migration (head stays `0104_whatsapp_path` — renumber at merge).

## Slice 6 — 24-hour window templates + hardening (Agent 191)

WhatsApp only allows free-form replies inside a 24-hour customer-service
window. When the window has expired, the software now re-opens it with a
pre-approved **template message** instead of failing silently:

- Template catalogue (`app/integrations/whatsapp_templates.py`):
  `task_ready` (utility, en_US, one parameter — "Your InfraSpine AI task
  is ready. {{1}}"). `validate_template` rejects bad names, languages,
  parameter counts and over-long parameters; `render_template_text`
  substitutes the parameters; `template_summary` caps the preview at 200
  characters and defaults an empty summary to "Reply here to continue.".
  Adding a template needs Meta approval — the catalogue is code on purpose.
- Template sends go through the same idempotent dispatch as normal
  replies (`send_template` / `send_template_once`, failover adapter
  threads `template=(name, language, parameters)`; Cloud sends the
  structured template, Baileys sends the rendered text). The worker's
  24-hour check now calls `_reopen_expired_window` in both places: it sends
  the `task_ready` template once, marks the dispatch accepted and the
  inbound completed, and audits `integration.whatsapp_template_reopen`. If
  the template send fails, the original "window expired" failure handling
  still applies.
- Timestamp hardening (`app/integrations/whatsapp.py` `accept()`): a
  future or unparseable timestamp falls back to the arrival time (it used
  to become NULL, which made the worker always treat the message as
  window-expired); a message more than 7 days old is rejected at intake as
  `stale_message` with audit `integration.whatsapp_stale_message` and no
  dispatch row.
- Secret rotation: `POST /whatsapp/rotate-secret` rotates one connection
  secret at a time (validated, re-sealed, audited as
  `integration.whatsapp_secret_rotated`); the old value stops verifying
  immediately.
- Audit completeness (`app/integrations/whatsapp_audit.py`): every inbound
  is expected to end in a terminal audit group — accepted runs need
  `completed` or `template_reopen`, failed runs need `failed` or
  `stale_message`, queued/processing runs are `pending` while they wait.
  `check_audit_completeness` reads the audit log by inbound id and reports
  `complete` / `pending` / `incomplete`.
- Spoken approvals now work (two slice-5 gaps closed): voice-note
  transcripts have the `[Voice note transcript] ` machine-text prefix
  stripped (`strip_machine_text_prefix` in
  `app/integrations/whatsapp_voice.py`) before the approval command is
  parsed, in both the Cloud and Baileys paths — a spoken "APPROVE …"
  decides the run instead of being ignored. The Baileys audio branch also
  decides a spoken APPROVE/REJECT from the raw transcript before running
  the agent (no acknowledgement, no agent run for a decision).

- Tests: `tests/test_whatsapp_templates.py` — **21 passed, 0 failed**
  (single process, 650.16s): 5 pure (catalogue, validation, summary,
  prefix strip, audit groups) + template worker / idempotency / failover
  (3) + stale/future timestamps (2) + secret rotation (3) + audit
  completeness (5) + spoken approvals (2). Two failures seen during the
  run were bugs in the new test file's own DB helper (a Core connection
  has no `db.get()`; `scalars(select(Entity))` yields only the first
  column) — fixed in the test file only.
- Regressions re-run in slice 6 (one process per chunk, same test DB,
  sequential): unified intake 7 + voice 20 + failover 20 + approvals 16
  = **63 passed**; cloud webhook + webhooks + receipts + studio-unified =
  **16 passed**; governance workflow + workflow engine = **9 passed**.
  **Total: 88 passed, 0 failed.** The 3 failover failures in the first
  chunk were a latent slice-5 test drift: the failover test stub did not
  accept the `interactive` argument slice 5 threads through
  `send_with_failover` (the same stub was fixed the same way in the
  integration merge) — fixed in the test file only (the stub now also
  accepts `template`).
- Alembic: **roundtrip + check clean** on a fresh UTF8 throwaway DB —
  base → head `0104_whatsapp_path`, downgrade −1 → `0103`, upgrade back,
  `alembic check` reports "No new upgrade operations detected". **No new
  migration in this slice.**
- Frontend: `npm test` **22 passed, 0 failed**; `tsc --noEmit` exit 0;
  `vite build` exit 0 (no frontend change in this slice — gates run to
  prove nothing broke).

## Whole step — test totals

New tests per slice: 7 (slice 1) + 8 (slice 2) + 20 (slice 3) + 20
(slice 4) + 16 (slice 5) + 21 (slice 6) = **92 new tests**, all passing
at the time each slice landed; slice 6 re-verified 88 regression tests
green on top. Full backend suite before merge is still owed (integration
step — Step E's verification pass owns the quiet-VM full suite).

## What is still owed (not hidden, not fixed here)

1. **WhatsApp runs execute inline, bypassing `execute_once` dispatch.**
   There is no `PlatformActionExecution` row for a WhatsApp run, so the
   "Done (verified)" stamp only appears for runs that went through the
   engine path. Routing WhatsApp runs through the unified engine is a
   follow-up for the integration / engine owner — it changes how every
   WhatsApp run executes and was not safely fixable inside this slice.
2. **Merge-time renumbering.** The branch Alembic head is
   `0104_whatsapp_path`, which collides with many other branches — renumber
   at merge (the integration map assigns the next free number).
3. **Live go-live (owner steps, after the final deploy).** Baileys: scan the
   QR code in the live app's WhatsApp panel (like WhatsApp Web / Linked
   Devices, about a minute), then test from the owner's phone — send a text
   first (`/agent` or `irfanix`), then a voice note within 5 minutes; the
   phone's WhatsApp must stay running. Meta Cloud: the owner must still
   supply Meta WhatsApp Cloud API credentials before any live Cloud send
   works (app secret, access token, verify token, phone number ID,
   business account ID). The owner's Windows host Baileys bridge
   (`bridge.mjs`, changed in slice 4) needs redeploying at live-push time.
4. **Manual owner checks** (plan §9): link a real phone via Baileys QR,
   send a task, verify the same answer on phone and dashboard; Meta Cloud
   verify-and-subscribe with real credentials; founder Roman Urdu voice
   note end to end (feeds Step H).
5. **Full backend suite** before merge (integration step; Step E pass).
6. **Template catalogue is code** — a new template needs Meta approval
   first, then a code change; there is no admin UI for adding templates.
7. **WhatsApp delivery channels for Step AA** (scheduled reports over
   WhatsApp) wait on this step being live — AA's WhatsApp delivery uses the
   Meta Cloud API only.

## Merge notes

- No migration in slices 5–6; branch head `0104_whatsapp_path` (renumber).
- `app/api/routes/agent_studio.py` is a CRLF file — slice-6 edits were
  byte-preserving; do not reformat.
- `app/cli.py` is mixed CRLF/LF — slice-6 did not touch it.
- `bridge.mjs` changed in slice 4 — redeploy the owner's Windows host
  bridge at live-push time.
- API transcription fallback provider must serve a Whisper-compatible
  model as `whisper-1`; Baileys voice notes need text activation first.
