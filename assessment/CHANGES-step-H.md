# Step H — Founder scenario + load test + ingestion: what changed

Plain-language changelog for the founder scenario (Step H). The founder can
speak a voice note in Roman Urdu or English — "mera is mahine ka kharcha
chart dikhao" — and the software turns it into a structured request, runs it
on the same workflow engine as everything else, and only executes it after a
manager who is not the founder approves that exact action. Nothing the
founder says can skip approval, change a payee, or grant a permission.

## What was built

### Slice 1 — voice-note parsing (Agent 17, commits 914336d + ed4eb6e)
- `app/qa/founder_voice.py` — Roman Urdu / English voice-note → structured
  command (`FounderVoiceCommand`): intent, sha256 `action_hash` binding the
  approval to that exact action, `requires_approval=True` always,
  `trusted=False` always. Bypass language ("bina approval ke payment kar
  do", "ignore previous instructions") rejects the whole command.
- `app/qa/load_harness.py` — transport-agnostic async load harness
  (virtual users, weighted step mix, p50/p95/p99 + error rate).

### Slice 2 — voice note on the unified engine (Agent 115)
- `app/founder_voice/unified.py` — the adapter: `record_voice_command`
  (idempotent on the action hash — the same words twice mirror to the same
  run, never a second one), `mirror_voice_command` (re-parses and re-hashes
  the sealed transcript; a mismatch refuses), `mirror_pending_voice_commands`
  (tick sweep for records left in `received`), `on_workflow_cancelled`
  (stopping the run cancels the intake record), `FounderVoiceExecuteHandler`
  (`founder_voice.execute`). The workflow is start → approval (risk high,
  digest-bound) → action (risk high, `auto: True`) → end. The handler only
  *reads*: spend chart (monthly series by currency), invoice headline (the
  exact "32 of 50 verified" the finance workflow produced), continuity
  replacement (allowed only if the founder may read that tenant's node —
  approval never grants access), approval request (records the request only,
  never moves money). Finance executions go through `execute_once`
  (key `founder_voice:<action_hash>`, duty `finance.read`, read back by
  re-computing the answer).
- `app/db/founder_voice_models.py` + migration
  `0114_founder_voice_commands` — `founder_voice_commands` table (sealed
  transcript, intent, action hash unique, routing ids, status
  received/mirrored/cancelled/failed); widens the workflow-definition origin
  check to include `founder_voice`. **Renumber at merge: 0104 is also
  claimed by HR, WhatsApp AE, AI freshness, continuity and the Foundry.**
- Wiring: engine tick sweep hook (`app/workflows/engine.py`), handler
  registry (`app/workflows/actions.py`), cancel hook
  (`app/workflows/service.py`), origin check
  (`app/db/workflow_engine_models.py`).

### Slice 3 — founder-only voice-note API (Agent 115)
- `app/api/routes/founder_voice.py` — `POST /api/founder-voice/note`
  (JSON transcript), `POST /api/founder-voice/note-audio` (audio →
  `transcribe_voice_upload`, same 8 MB / 120 s caps, staged audio discarded),
  `GET /api/founder-voice/commands/{action_hash}` (read-only). Founder-only:
  role admin + department membership (`require_scope`). Every intake writes
  an audit event (`founder_voice.note_received`).

### Slices 4–5 — load + ingestion, bounded (Agent 115)
- `app/qa/asgi_load.py` — ASGI transport: drives the real app in-process
  (no network, no server), one session per virtual user, steps
  health / login / documents / founder_note.
- `app/qa/ingestion_projection.py` — deterministic synthetic corpus,
  measured `split_text` throughput, time-to-index projection for 600 GB
  with its assumptions stated. Chunking is the cheapest ingestion stage,
  so the projection is a **lower bound** — parsing, embedding and index
  writes are not in it.
- The 1,000-user qualification and the 600 GB qualification **cannot run on
  this VM** (1.5 shared CPUs, no target-like hardware, HuggingFace
  unreachable). What is proven here is the method plus a small-scale pass;
  the full qualification needs a staging stack on target hardware. The
  measured numbers are recorded in `docs/capacity-benchmark.md`.

## Corrections found during the build
- Slice 1's `REQUIRED_PERMISSIONS` named permissions that **do not exist**
  in the platform catalog (`finance.invoices.verify`,
  `finance.payments.approve`, `finance.payments.transfer`,
  `continuity.replacement.request`, `governance.approve`) — an adapter could
  never authorize against them. Corrected to the real ones (`finance.read`,
  `invoice.verify`, `payment.approve`, `funds.transfer`; continuity has
  none, its tenant scope is the authority).
- The action node and the `execute_once` duty use `finance.read` on purpose:
  asking for a headline is a read. The duty the request is *about*
  (`invoice.verify` / `payment.approve` / `funds.transfer`) belongs to the
  person who later performs that duty through the finance workflow.

## Files changed (vs base ca956c2)
- `app/founder_voice/__init__.py`, `app/founder_voice/unified.py`
- `app/db/founder_voice_models.py`, `app/db/models.py`,
  `app/db/workflow_engine_models.py`
- `migrations/versions/0114_founder_voice_commands.py`
- `app/api/routes/founder_voice.py`, `app/main.py`
- `app/workflows/engine.py`, `app/workflows/actions.py`,
  `app/workflows/service.py`
- `app/qa/founder_voice.py` (permission correction + tests),
  `app/qa/load_harness.py`, `app/qa/asgi_load.py`,
  `app/qa/ingestion_projection.py`
- `tests/test_step_h_founder_load_harness.py`,
  `tests/test_step_h_founder_scenario.py`, `tests/test_step_h_load.py`
- `docs/step-h-founder-plan.md`, `docs/capacity-benchmark.md`,
  `assessment/CHANGES-step-H.md`

## Tests
- Slice 1 pure: [PENDING: fill from run] passed
  (`tests/test_step_h_founder_load_harness.py`, `--noconftest`).
- Slice 2–3 scenario: [PENDING: fill from run] passed
  (`tests/test_step_h_founder_scenario.py`, throwaway `*_test` DB).
- Slices 4–5: [PENDING: fill from run] passed
  (`tests/test_step_h_load.py`: projection pure + fake-app transport +
  small real-app load pass).
- Canonical conftest re-run of Agent 17's slice-1 tests: [PENDING].
- Alembic roundtrip + check: [PENDING].
- Full backend suite: NOT run by this step (the Step E gate pass owns it;
  two passes at once are forbidden).

## Owner decisions still needed
- **Whisper API fallback:** needs an API key decision; local Whisper stays
  first (handover §5 rule 2).
- **Voice-note retention:** transcripts ARE stored sealed in
  `founder_voice_commands` for audit — the owner must confirm how long
  they are kept.
- **Qualification hardware:** 600 GB / 1,000-user qualification needs
  target-like hardware; this VM cannot qualify it.

## Merge readiness
[PENDING: fill after the test runs and the Alembic roundtrip land.]
