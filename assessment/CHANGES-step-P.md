# CHANGES — Step P: Live-data grounding

> Branch `agent/live-data`, base `step-e-gaps-wip` (`ca956c2`).
> Status: slices 1–5 done. Focused gates green, Alembic clean. The
> full-backend-suite count is still being collected in chunks (driver
> running — see "Full-suite gate"); merge only after that tally is green.

## What this step does (plain language)

Knowledge chat can now answer questions whose answer changes with time —
the next match, a live score, the dollar rate, today's news — using fresh
live web data with sources, instead of saying "Insufficient document
evidence".

- **Every live fact carries a citation** (`[S1]`, `[S2]`, …) with the
  source title, URL, `Published:` and `Retrieved:` timestamps. A missing
  publication date is shown as missing — never invented.
- **The assistant never guesses.** If no live source can verify a current
  answer, it says so plainly and offers no citations, because there is
  nothing to cite.
- **Questions about your own documents stay on your documents.** "The next
  match in the uploaded report" is never sent to the web.
- **Freshness window.** Sources dated within the last 14 days (setting
  `rag_live_freshness_days`, 1–365) are preferred; older dated sources are
  dropped when at least one fresh source exists. Undated sources are always
  kept and shown as undated. When nothing is fresh, nothing is dropped.
- **Follow-up offers are proposals only.** An answered schedule question
  can offer one approval-gated reminder ("Remind me before this event").
  Nothing is created without an approval.
- **"Checked live at …"** is stamped on every live answer, so you can see
  when it was verified.

## The bug it fixes

Asking Knowledge chat `pak a ka next match kb hai?` ("when is Pakistan's
next match?") used to answer "Insufficient document evidence", even though
live web search exists. The question was never routed to the web at all:
two routing gates were blind to schedules — the live-fact classifier only
knew present-tense words (`latest`, `today`, `live score`), and the cricket
gate needed the word `cricket` plus a "current" word. A `kb hai` ("when
is it") question has neither. Both gates now understand schedules
(`next`, `upcoming`, `agla`, `kb`/`kab`, `when`, `schedule`, `fixture`,
`match`).

## Files changed (16 files, +1988/−15 vs `ca956c2` — includes this
changelog and the slice-5 plan-doc update below)

### Slice 1 — routing fix + provider seam (Agent 20, `a16106c`)
- `app/services/live_data.py` (new) — `LiveSearchProvider` protocol,
  `LiveSource` / `LiveAnswer`, `MockLiveSearchProvider`,
  `answer_live_question`, `format_live_answer`, `normalize_live_query`.
- `app/rag/answer.py` — new `_LIVE_SCHEDULE_REQUEST` classifier, wired
  into `is_web_lookup_request` behind the uploads/company guards.
- `app/services/public_web.py` — `match` / schedule words added to
  `is_current_cricket_request` so "next match" reaches the direct Cricbuzz
  scoreboard path.
- `tests/unit/test_live_data.py` (new).

### Slice 2 — provider wired into `run_chat` (Agent 74, `a8140ab`)
- `app/services/live_data.py` — `PublicWebLiveSearchProvider`, the
  real-provider adapter over the existing `public_web_research`. The
  composed answer is split into per-source evidence segments by its
  `[S#]` citations; sources the answer never cites are dropped, never
  cited. The relevance gate reuses `public_sources_relevant` (skipped for
  cricket, exactly as the native path does — cricket results carry their
  own relevance proof in explicit match-state extraction).
- `app/rag/chat.py` — native grounded search stays first; the
  public-search fallback now answers through `answer_live_question` with
  the real provider. The cricket direct-scoreboard path is unchanged and
  never reaches the adapter. `_live_answer_to_web_answer` converts
  `[S#]` to `[W#]` without duplicating the sources block.
- `app/rag/answer.py` — `refers_to_uploads` now catches bare references
  ("in the uploaded report", "the uploaded pdf", "upload ki hui file")
  and the guard now applies to the explicit-web-request and live-fact
  branches too; a pasted public URL stays unguarded. Also fixed a
  `résumé` regex escape that matched the wrong characters, and a bug
  where "aaj ka dollar rate in the uploaded report" still routed to the
  web.
- `app/core/config.py` — new setting `rag_live_freshness_days` (default
  14, 1–365).
- Both formatters append a "Checked live at …" line.

### Slice 3 — frontend citations (Agent 74, `332ca44`)
- `frontend/src/lib/answerMarkdown.mjs` (+ `.d.mts`) — `webCitationTargets`
  keeps each live source's `Published:` / `Retrieved:` timestamps.
- `frontend/src/components/Markdown.tsx` — the Knowledge chat `[W#]` chip
  tooltip shows title plus Published/Retrieved; a missing publication
  date reads "not available", never invented.
- The "could not verify live — not guessed" state already existed and was
  verified, not rebuilt: `run_chat` returns a non-empty insufficient
  `web_research` answer and KnowledgePage shows the "Live lookup
  unavailable" badge instead of an empty answer.

### Slice 4 — live actions + evals (Agent 74, `f3fc16d`)
- `app/services/live_data.py` — `LiveFollowupAction` +
  `live_followup_actions`: one approval-gated reminder offer for answered
  schedule questions (`requires_approval=True`); insufficient and
  non-schedule answers offer nothing.
- `app/services/live_data_eval.py` (new) + `scripts/eval_live_data.py`
  (new) — fixed offline eval set (schedule, live score, rate, news,
  unknown) measuring `citation_rate`, `no_guess_rate`, `expected_rate`;
  provider failure is recorded as insufficient, never raised.

### Slice 5 — changelog + gates (Agent 90, this retry)
- `assessment/CHANGES-step-P.md` (this file).
- `docs/step-p-live-data-plan.md` — §7 updated, Step P marked done.
  (Agent 85 started this slice and errored before delivering anything;
  Agent 90 verified its draft claims against the committed diff and
  finished the gates below.)

### No migration
Step P adds **no database migration** and changes no schema. Alembic head
stays `0103_unified_studio_governance`. The roundtrip below is a
verification that the head is unchanged and clean.

## Tests

### New tests: 47, all passing
- `tests/unit/test_live_data.py` — **38** (21 slice 1: cited/insufficient
  answers, empty-snippet / non-HTTPS dropping, provider failure, routing
  table; 17 slice 2: adapter, freshness window, bare-uploads routing,
  "Checked live at").
- `tests/unit/test_live_data_eval.py` — **5** (slice 4).
- `tests/unit/test_public_web_relevance.py` — **+3** (2 slice 2:
  "Checked live at", findings conversion; 1 slice 4: follow-up offer).
- `frontend/test/answerMarkdown.test.mjs` — **+1** (slice 3: timestamps,
  missing date never invented).

### Focused suites (re-verified by Agent 90, 2026-11-28 ~04:30 PKT and ~00:22 UTC)
- Backend focused suites: **99 passed, 0 failed**
  (`test_live_data.py` + `test_live_data_eval.py` +
  `test_public_web_relevance.py` + `test_followup_pronoun_routing.py`,
  `--noconftest`, log capture on).
- Frontend: **23 passed, 0 failed** (`npm test`), typecheck clean,
  `vite build` clean.
- Eval script (offline, mock provider, re-run 2026-11-28 ~00:22 UTC):
  **5/7 answered, citation_rate 0.71, no_guess_rate 1.00,
  expected_rate 1.00**; a failing-provider run answers 0/7 with
  no_guess_rate 1.00; script exits 0.

### Full-suite gate (Agent 90, this slice) — IN PROGRESS, not yet green
- The whole-suite run in one process does **not** complete on this VM:
  the attempt died silently after ~5 minutes with zero pytest output
  (load average ~63, ~12 agents' pytest processes running at once; the
  most likely cause is the OOM killer at startup — the same failure mode
  Agent 11/96 hit repeatedly on the merged head: 4 full-suite + 3 chunk
  attempts, `SUITE_RC=247` / temp Postgres OOM-killed).
- The suite is therefore being run in **chunks**, one pytest process at
  a time, against Agent 90's own throwaway cluster
  (`127.0.0.1:55490`, DB `infraspine_p_test`, `DATABASE_URL` pointed at
  the cluster's empty `infraspine_ai` DB — the runner never sets
  `DATABASE_URL`, but `tests/conftest.py` requires it):
  chunk 0 = the 4 Step P files **with** conftest (real DB fixture),
  chunks 1–8 = the remaining 192 test files (24 each, heavy files
  spread out). Driver: `/home/hatch/tmp-agent90/run_chunks.sh`
  (pid 44920, launched 2026-11-28 ~00:21 UTC); per-chunk logs
  `/home/hatch/tmp-agent90/chunkN.log`; final tally
  `/home/hatch/tmp-agent90/CHUNKS_SUMMARY.txt`.
- Chunk 0 was still running when this file was committed (it had
  finished migrating the test DB and started executing tests; under the
  current load a single chunk crawls at ~1% CPU share). **Do not merge
  until the chunk tally is collected and shows the full suite green**
  (bar: ≥1347 passed, 0 failed, same as main).
- No Step P test has failed anywhere it was run: 99/99 focused
  (`--noconftest`), and chunk 0's with-conftest run had produced no
  failures at commit time.

### Alembic gate (Agent 90, this slice) — CLEAN
- On Agent 90's own throwaway cluster (port 55490, DB
  `infraspine_p_alembic`): `alembic upgrade head` → exit 0, head
  `0103_unified_studio_governance`; `alembic downgrade base` → exit 0;
  `alembic upgrade head` again → exit 0, head 0103;
  `alembic check` → exit 0, "No new upgrade operations detected."
  (A pre-existing `SAWarning` about unresolvable FK cycles among
  `folder_sync_runs` / `folder_sync_sources` / `meeting_artifacts` /
  `meeting_revisions` / `positions` / `users` appears in the check
  output; exit 0, not an error.)
- Step P adds no migration; head stays 0103.

## Safety rules kept

- Web pages, search snippets and live data are **untrusted input**. Text in
  them can never grant a permission, change a payee, or change what an
  approval covers.
- Live answers are read-only: no side effect, no `execute_once`, no
  dispatch. The moment a live answer triggers an action, that action goes
  through the normal authorization and approval path.
- No live lookup sends private document content, user identity, or internal
  data outside the deployment. Only the user's explicit public question
  (and, for follow-ups, previously cited public URLs) leaves the
  deployment, and the lookup is audit-logged as `rag.web_search_requested`.
- No secrets in prompts, logs, or commits. Tests use the mock provider
  only — **no real external calls in tests**.
- Provider failure never raises to the caller: a failed lookup becomes an
  insufficient answer, never a guessed fact.

## Known limitations (not blockers for merge)

- The `run_chat` fallback wiring is covered at unit level (adapter +
  conversion helper), not by an end-to-end `run_chat` test with a live DB.
- Freshness-window UI in admin settings is not yet built — the setting
  `rag_live_freshness_days` exists; there is no UI to change it yet.
- The real-provider eval run needs network + a configured provider; the
  eval runner only supports the mock provider today.
- When nothing is fresh, nothing is dropped (all sources are kept rather
  than answering with nothing).

## Owner decisions needed

None. The remaining items above are work items, not choices awaiting the
owner.
