# Step O — Voice-first founder mode: plan

> Owner-approved futuristic step (MASTER-PLAN.md, 2026-11-28). Agent 19 (slice 1).
> Goal: the founder speaks a voice note; InfraSpine transcribes it, understands the
> intent, and carries out the verified action through the unified workflow engine —
> then replies by voice. Typing is the fallback, not the default.

## What exists already (mapped 2026-11-28, HEAD `ca956c2`)
- **Voice question (RAG only):** `POST /api/rag/voice-question`
  (`app/api/routes/rag.py`, `app/rag/voice_questions.py`). A bounded audio upload
  (8 MB / 120 s, WAV/MP3/M4A/FLAC/OGG/OPUS/AAC) is staged in a temp dir, transcribed
  locally with Whisper (`AudioParser.parse_spoken_question`), and the staged audio is
  discarded before the helper returns. The transcript is answered from Knowledge;
  nothing is saved, no trace is written. Tests: `tests/test_voice_questions.py`,
  `tests/test_voice_question_upload_unit.py`, `tests/test_voice_question_route_unit.py`,
  `tests/test_voice_question_api.py`.
- **Approvals (exact-action, expiring, single-use):** `app/platform/approvals.py`
  (`ActionSpec` with canonical SHA-256 hash, `request_approval`, `decide_approval`,
  `consume_approval`, separation of duties). **Workflow approval step:**
  `app/workflows/builtin_actions.py` (`platform.approval` handler). **Governance
  mirror:** `app/services/agent_action_runtime.py` (proposal → workflow run →
  approval → `execute_once` with read-back). **Execution:** `app/platform/execution.py`
  (`execute_once`).
- **Unified workflow engine:** `app/workflows/unified.py` (origins `workflow`,
  `automation`, `system`, `studio`, `governance`; mirror helpers per origin).
- **WhatsApp voice (backlog only):** `docs/irfanix-backlog.json` (`mobile-media`):
  authorized inbound WhatsApp voice + transcription is planned, not built.

## What is missing (the gaps Step O closes)
1. Voice notes cannot **do** anything — today they can only ask a Knowledge question.
2. No intent parsing from a transcript; no mapping from words to a workflow action.
3. No voice-action proposal/approval flow; no voice reply (TTS) back to the founder.
4. No `voice` origin on the unified engine; no voice audit trail.
5. No WhatsApp voice-note intake (Step F dependency for the WhatsApp channel).

## Design (non-negotiable rules)
- **Transcript is untrusted data.** Spoken words (or a doctored audio file) can never
  grant a permission, change a payee, change what an approval covers, or select an
  arbitrary handler. The parser only ever returns one of an allowlisted set of intent
  kinds and fills only whitelisted parameters. Anything else — including
  instruction-like phrases ("ignore previous instructions", "grant me admin",
  "approve all invoices", "pay vendor X") — maps to `unknown`: no proposal, no action.
- **Approval before any side effect.** A voice action is first a *proposal* bound to
  an exact `ActionSpec` (type, scope, params, amount, subject) with its SHA-256 hash.
  Execution requires a consumed platform approval for exactly that hash, unexpired,
  single-use, and decided by someone other than the requester (separation of duties).
  The founder's own voice note can *request* an action; it can never approve its own
  privileged execution.
- **Through the unified engine.** Every voice action runs as a real workflow run
  (`origin='voice'`), with the `platform.approval` step first and `execute_once`
  with read-back verification for the side effect. No parallel engine.
- **Bounded and discardable.** Audio intake reuses the existing bounded upload helper
  (8 MB / 120 s); staged audio is deleted before the helper returns. Transcripts are
  kept only as short excerpts for audit, never full audio.
- **Fallbacks (rule 2 of the handover):** local Whisper ↔ API transcription;
  intent parser is deterministic (no LLM) for slice 1, LLM-assisted intent is a later
  slice and must still map into the same allowlist.

## Slices
1. **(DONE by Agent 19, this slice)** Intake → transcript → proposed (not executed)
   workflow action, approval-gated. Files: `app/voice/intents.py`,
   `app/voice/proposals.py`, `app/voice/service.py`, `tests/test_voice_first_proposals.py`.
   No migration, no route, no real side effect. Dry-run only.
2. Mirror voice proposals as unified workflow runs (`origin='voice'`, migration widens
   the origin check; `app/workflows/unified.py` `ensure_voice_definition` +
   `mirror_voice_proposal`), reusing the `platform.approval` step.
   **(DONE by Agent 87, slices 2-3.)** Migration `0104_voice_origin` widens the
   `ck_wf_definitions_origin_valid` check and adds the `voice_proposals` table.
   `app/voice/unified.py`: `voice_spec()` (start -> `voice.proposal_approval` ->
   `voice.proposal_execute` (`voice.manage`) -> end), `mirror_voice_proposal`,
   `mirror_pending_voice_proposals` (engine tick sweep). Design decision: a
   dedicated approval handler waits on the ONE platform approval the route
   requests (bound to the proposal's exact action hash) instead of the generic
   `platform.approval` step, which would request a *second* approval whose hash
   also covers the run/step ids. Same machinery, one approval, one hash.
   `voice.manage` is granted to `group.manager` and `org.admin` in
   `app/platform/sync.py` (reconciled on use; no data migration).
3. `POST /api/voice/note` route (founder only, `voice.manage` permission): upload →
   transcript → proposal → approval request; `GET /api/voice/proposals/{id}` status.
   **(DONE by Agent 87, slices 2-3.)** `app/api/routes/voice.py` + `submit_voice_note`
   in `app/voice/service.py` (record -> mirror -> approval request, so a mirror
   failure commits nothing). Decisions go through the generic inbox
   (`POST /api/approvals/{id}/decide`); the approval step polls it. Execution
   (`app/voice/unified.py` `execute_voice_proposal` + `app/voice/effects.py`):
   the platform approval is consumed exactly once inside `execute_once`, with
   read-back of the proposal row. Per-intent effects: `status_overview` computes
   real counts (read-only); `invoice_approve` records the second-person approval
   decision against the exact verified invoice and deliberately does NOT change
   the invoice status (that transition belongs to the payment-approval flow —
   flipping it would bypass that flow and break payment grouping); `meeting_summary`
   fails closed (the slice-1 parser captures no meeting reference, so no meeting
   can be identified without guessing). A failed effect spends the approval
   (single use): the founder records a new voice note for a new approval.
   Tests: `tests/test_voice_unified.py` — 25 new tests, all pass (15 confirmed
   under canonical pytest in the final full run — 15 dots, no failures, the
   process was OOM-killed during test 16 — and all 25 confirmed under a
   driver harness running the same test functions with the conftest fixtures
   replicated, DB at head 0104). Two failures found and fixed on the way:
   the sweep test was rewritten (wf_events/wf_decisions are append-only by
   trigger, so a mirrored run cannot be deleted to fake the unmirrored
   state — the test now creates the record directly) and the missing-invoice
   failure reason now names "INV-99" as spoken. Regressions: slice-1 file
   `tests/test_voice_first_proposals.py` 17/17 pass; focused workflow-engine
   2/2 pass; focused platform-core 2/2 pass. **(Agent 156 closed the deferred
   items:** governance-unified focused batch E2 8/8 pass
   (`tests/test_governance_unified_runtime.py`); remaining workflow-engine
   6/6 pass; remaining platform-core 7/7 pass; slice-1 file re-run 17/17 pass
   — all under the same driver harness. **Still owed:** a full canonical
   pytest re-run of `tests/test_voice_unified.py` in one process — Agent 156
   re-ran it canonically and it died at the same place (15 dots, 0 failures,
   OOM-killed during test 16,
   `test_invoice_approve_verified_invoice_records_decision_without_changing_status`;
   the VM cannot hold the session process under current load). The driver
   evidence stands; the canonical re-run needs a quiet VM.)
4. Approval decision + execution: consume the platform approval, dispatch through
   `execute_once`, read-back verification, audit events `voice.proposal_created`,
   `voice.action_executed`. **(DONE by Agent 156.)** Both audit events already
   existed (recorded in `app/voice/service.py` and `app/voice/unified.py`);
   slice 4's remainder was the proof: new test
   `test_audit_events_recorded_for_proposal_and_execution` in
   `tests/test_voice_unified.py` asserts both events land in the append-only
   `audit_logs`, targeted at the proposal id, with outcome `success` — passed
   under the driver harness. No other slice-4 work was outstanding.
5. Voice reply (TTS) back to the founder: spoken confirmation or "needs your approval"
   message; text fallback when TTS is unavailable. **(DONE by Agent 156.)**
   `app/voice/reply.py`: `build_voice_reply()` — deterministic text from the
   proposal's stored state only (executed/proposed/approved/rejected/expired/
   failed; overview counts, invoice number; ASCII, ≤500 chars; the transcript
   excerpt is never included). `app/voice/tts.py`: `synthesize()` — providers
   `none` (default, text fallback), `local` (espeak-ng/espeak subprocess,
   30 s timeout, text fallback when the binary is missing or fails), `api`
   (no TTS API configured, always text fallback). `voice_tts_provider` setting
   in `app/core/config.py` (default `none`). Route
   `GET /api/voice/proposals/{id}/reply` in `app/api/routes/voice.py` — same
   visibility as the proposal (requester or department manager, else 404);
   serves `audio/wav` with `X-Voice-Reply-Text` / `X-Voice-Reply-Status` /
   `X-Voice-Reply-Provider` headers when synthesis succeeds, else JSON
   `{text, status, audio_available: false, reason: "tts_unavailable", provider}`.
   One real bug found and fixed on the way: the stored failure reason carries a
   machine-code prefix (`not_found: No invoice INV-99...`) and the reply spoke
   it aloud — `build_voice_reply` now strips one leading snake_case code prefix
   (`_human_reason`), keeping plain human sentences with colons untouched.
   Tests: `tests/test_voice_reply.py` — 25 tests, all pass under the driver
   harness (10 reply-text pure, 7 TTS pure, 6 route, + 2 pure tests locking the
   code-prefix strip). No migration. Alembic check clean, head stays
   `0104_voice_origin`.
6. WhatsApp voice-note intake (depends on Step F): authorized inbound voice via the
   Step F channel adapter, same proposal/approval path.
7. LLM-assisted intent (optional, later): transcript → intent via the model router,
   still constrained to the allowlist; every new intent kind needs its own tests.

## Intent allowlist (slice 1)
| Kind | Trigger words (EN + Roman Urdu) | ActionSpec type | Params |
| --- | --- | --- | --- |
| `meeting_summary` | meeting + summary/minutes/khulasa | `voice.meeting_summary` | `meeting_ref` (optional) |
| `invoice_approve` | invoice + approve (+ `INV-…` / number) | `voice.invoice_approve` | `invoice_ref` (required-ish) |
| `status_overview` | status / progress / update / "kya chal raha" | `voice.status_overview` | — |
| `unknown` | anything else, or any suspicious phrase | — | no proposal |

## Test list (slice 1 — all in `tests/test_voice_first_proposals.py`)
- Intent parsing: meeting summary (EN + Roman Urdu), invoice approve with reference,
  status overview, chit-chat → `unknown`.
- Injection resistance: "ignore previous instructions", "grant admin", "approve all
  invoices", "pay vendor" → `unknown`, no proposal.
- Proposal: unknown intent raises; hash is stable; changing any param changes the hash.
- Approval gate: no approval → blocked; wrong-hash approval → blocked; expired
  proposal → blocked; matching approval → dry-run result naming the exact action.
- Service: fake audio + fake transcriber → proposal; unknown transcript → no proposal;
  empty transcript → rejected.

## Still needed after slice 1
- Slices 2–7 above. A real TTS provider decision (local ↔ API) — slice 5 ships
  with the safe default `voice_tts_provider=none` (text fallback only); the
  owner picks local espeak or an API when ready. Founder voice
  verification (voiceprint) before privileged requests — out of scope for slice 1,
  listed here so it is not forgotten: a stolen audio clip must not be able to act as
  the founder. Until voiceprint exists, privileged voice actions always need a second
  person's approval, never the requester's own.
