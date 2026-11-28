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
  `0104_founder_voice_commands` — `founder_voice_commands` table (sealed
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
- **Spend-chart query rejected by Postgres (real bug, fixed):**
  `to_char(...)` was rendered separately in SELECT / GROUP BY / ORDER BY,
  giving it different bind parameters, so Postgres refused the query
  ("column must appear in the GROUP BY clause"). The month expression is
  now labelled once and reused.
- **Handler used the wrong `execute_once` duty (real bug, fixed):** it
  passed `command.required_permissions[0]` (`invoice.verify`), which a
  founder with no finance role does not hold, so every invoice-headline
  run failed `forbidden`. The duty is now `INTENT_EXECUTION_PERMISSION`
  (`finance.read`, the same permission the workflow node carries); the
  about-duty stays on the record for the approval binding.
- **`execute_once` could wedge an execution `claimed` forever (real bug,
  fixed in `app/platform/execution.py`):** a dispatch database error aborts
  the transaction, so the failure-marking writes (publish / audit / commit)
  could never land and a replay waited forever instead of failing. It now
  rolls back and re-fetches the row before marking it `failed`.
  Regression test
  `test_execute_once_dispatch_database_error_is_recorded_failed_not_wedged_claimed`
  (dispatch runs invalid SQL on the same session) passes.
- **Blocklist gap (real bug, fixed):** "ignore previous instructions"
  was not blocked — it parsed as an `approval_request` and mirrored. The
  `ignore-instructions` pattern now covers ignore/disregard + previous /
  prior / above / all / any + instructions/directions (+2 hostile cases).
- **Engine fairness observation (NOT fixed — recorded for the engine
  owner):** `claim_next` claims one workflow per tick by earliest
  `updated_at`, and a run waiting on a human approval stays due (wake +2 s)
  forever, so it can starve every other workflow's runs indefinitely. The
  scenario tests work around it by waking only the founder run
  (`founder_pump`); production runs of a second workflow behind a
  human-waiting run would hit the same starvation.
- Test-only fixes (no product change): the approve step starts `blocked`
  (its normal waiting state — tests now wait for `approval`); run detail is
  visible only to the requester/managers, so tests read as the founder;
  decisions must be posted as the decider (reading the step re-logs in as
  the founder); rejected transcripts return 422 and unsupported audio 415
  by the app's own conventions; `bc_tenants` seed rows need
  `revision`/`event_head`; `ActionContext` lives in
  `app.workflows.actions` and async work must run on the portal loop.

## Files changed (vs base ca956c2)
- `app/founder_voice/__init__.py`, `app/founder_voice/unified.py`
- `app/db/founder_voice_models.py`, `app/db/models.py`,
  `app/db/workflow_engine_models.py`
- `migrations/versions/0104_founder_voice_commands.py`
- `app/api/routes/founder_voice.py`, `app/main.py`
- `app/workflows/engine.py`, `app/workflows/actions.py`,
  `app/workflows/service.py`
- `app/qa/founder_voice.py` (permission correction + tests),
  `app/qa/load_harness.py`, `app/qa/asgi_load.py`,
  `app/qa/ingestion_projection.py`
- `app/platform/execution.py` — `execute_once` rolls back before marking a
  dispatch failure (see Corrections), `tests/test_platform_core.py` (one
  new regression test)
- `tests/test_step_h_founder_load_harness.py`,
  `tests/test_step_h_founder_scenario.py`, `tests/test_step_h_load.py`
- `docs/step-h-founder-plan.md`, `docs/capacity-benchmark.md`,
  `assessment/CHANGES-step-H.md`

## Tests
All on throwaway `*_test` databases (cluster port 55438, role `agent115`),
via the verify-merged venv. Each scenario test was run one at a time
(full-file runs OOM-kill on this VM); each single test takes 3–11 minutes
here (conftest rebuilds the schema per session + the finance fixture).
- Slice 1 pure: **32 passed, 0 failed**
  (`tests/test_step_h_founder_load_harness.py`, canonical conftest run —
  the 27 original + 3 spec tests + 2 new hostile-transcript cases;
  also 30 passed `--noconftest` before the 2 new cases).
- Slice 2–3 scenario: **8 passed, 0 failed**
  (`tests/test_step_h_founder_scenario.py`, one at a time: spend chart
  616 s, invoice headline 555 s, continuity 534 s, rejected transcripts
  484 s, audio rejection 464 s, tampered/other-owner 394 s, pending sweep
  250 s, stop cancels 196 s).
- Slices 4–5: **8 passed, 0 failed** (`tests/test_step_h_load.py`,
  canonical single-file run, 596 s: 6 projection + 1 fake-app transport +
  1 small real-app load pass — 5 users × 6 iterations = 30 requests,
  0 errors).
- `execute_once` wedge regression:
  **1 passed** (`test_execute_once_dispatch_database_error_is_recorded_failed_not_wedged_claimed`,
  in the same canonical session as slice 1: 33 passed total, 1660 s).
- Canonical conftest re-run of Agent 17's slice-1 tests: **DONE — 32
  passed** (see above; the re-run includes the 3 spec tests).
- Alembic roundtrip + check: **CLEAN** (2026-11-28 ~04:50 PKT, throwaway
  DB `infraspine_h115_alembic`: upgrade head RC=0 → downgrade base RC=0 →
  upgrade head RC=0 → `alembic check` RC=0, head
  `0104_founder_voice_commands`).
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
**Ready to merge after renumbering migration `0104_founder_voice_commands`**
(it collides with HR, WhatsApp AE, AI freshness, continuity and the
Foundry — all minted 0104; 0105 is minted by AI, AK and AB). All 5 slices
done; every test above green; Alembic roundtrip clean; no `[PENDING]`
markers left in this file. The full-backend-suite gate is owned by the
Step E verification pass (Agent 117) and stays owed for the main merge.
Two findings travel with the merge: (1) the engine claim-starvation
observation above — a second workflow's runs can starve behind a
human-waiting run; (2) the `execute_once` rollback fix touches shared
platform code and is covered by one new regression test plus the 8
scenario tests.
