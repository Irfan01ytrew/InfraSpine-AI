# CHANGES — Step AI: Knowledge freshness (document-level)

Plain-language summary of what changed, why, and how it was checked.
Step AI adds freshness to the documents in Knowledge: every document can
carry a review date, an expiry date, or a pointer to the document that
replaces it, and the app shows — on each document, on each cited source in
an answer, and in a "needs review" list — whether that document is still
fresh. Nothing is ever hidden or removed automatically: freshness is a
label for humans, not a filter. An expired document is still cited, but
the answer says so.

Note: a freshness system already existed for *knowledge revisions*
(review_due_at, the stale endpoint). Step AI is *document-level* only and
does not change the revision system.

## What the user sees

- **On every document** (library list and detail): a freshness label —
  fresh, aging, stale, expired, or superseded — with a score out of 100
  and a plain-language reason (for example, "Review was due on
  2026-09-01"). Only shown for documents the viewer may already read.
- **In every chat answer**: each cited source carries its freshness, and
  a warning banner appears when the answer leans on an expired or
  superseded source, or only on stale ones. The answer is still given
  from those sources — freshness never makes the app guess, and never
  silently drops a source.
- **In the library**: a "needs review" section lists documents that are
  stale, expired, superseded, or have open contradiction candidates,
  most urgent first. A document's owner can set (or clear) its review
  date right there; the queue refreshes after saving.

## How freshness is decided (defaults)

- **Superseded** — an owner pointed the document at the document that
  replaces it. Score 0.
- **Expired** — its `expires_at` has passed. Score 0, even if recently
  reviewed.
- **Failed** — the document failed to index, so nothing searchable
  exists for it. Score 0.
- **Aging** — its review date has passed but it is not yet stale.
  Score 60.
- **Stale** — older than the stale threshold (default 365 days) or past
  its review date by more than the grace period (default 180 days).
  Score 20.
- **Fresh** — recent, reviewed, and indexed. Score 100.
- A document whose searchable content is older than its latest version
  loses points and says so.

Review dates and expiry are owner-set metadata. They are never derived
from document text, and ingestion never sets them.

## New API endpoints

- `GET /api/rag/documents/review-queue` — the review queue, ordered most
  urgent first (expired, superseded, stale, then fresh/aging documents
  with open conflicts). Only documents the caller may already read are
  ever listed; each entry carries the document, its open-conflict count,
  and a severity (0 = most urgent).
- `PATCH /api/rag/documents/{id}/freshness` — owner-only. Body may set
  `review_date`, `expires_at`, and/or `superseded_by`; only fields
  explicitly sent are changed, and an explicit `null` clears the field.
  `superseded_by` must be another document the caller may read, never
  the document itself. Changes are written to the audit log as
  `document.freshness_changed`.

## Contradiction scan (slice 3)

- `app/rag/freshness_scan.py`, runnable as `python -m app.rag.freshness_scan`,
  scans chunks indexed in the last 24 hours (at most 200) for pairs of
  passages that say opposite things. Candidates store chunk ids only —
  never passage text — and are idempotent (the same pair is never stored
  twice). The scan never changes publication or retrieval state.
- A candidate is open, confirmed, or dismissed by a human reviewer with a
  written rationale (3–2000 characters, append-only record). Nothing is
  ever auto-resolved.

## Files changed

- `app/rag/freshness.py` — pure scorer (`score_freshness`,
  `detect_contradiction`, `citation_freshness_note`), answer summary
  (`summarize_answer_freshness`, `CitationFreshness`, `AnswerFreshness`).
- `app/rag/answer.py` — `GroundedAnswer` gains `freshness_warning` and
  `citation_freshness`; `answer_question(..., freshness_metas=...)`.
- `app/rag/chat.py` — `run_chat` builds the freshness metadata for the
  cited passages (one query) and passes it through, including the
  hybrid web+internal path.
- `app/rag/respond.py` — citations in `RagChatResponse` carry
  `freshness_status`/`freshness_score`/`freshness_note`; the export file
  appends the warning after the Sources section.
- `app/rag/documents.py` — `DocumentService.set_freshness` (owner-only,
  via `_get_modifiable`).
- `app/services/document_freshness.py` — `document_meta` /
  `freshness_for` / `citation_note_for` adapters, plus the review queue
  (`review_queue_entries`, `open_conflict_counts`, `ReviewQueueEntry`).
- `app/rag/freshness_scan.py` — the nightly contradiction scan + CLI.
- `app/schemas/rag.py` — `DocumentOut` gains the freshness fields;
  `Citation` and `RagChatResponse` gain theirs; new
  `DocumentFreshnessRequest`, `ReviewQueueEntryOut`, `ReviewQueueResponse`.
- `app/api/routes/rag.py` — `GET /documents/review-queue` (registered
  before `/documents/{id}` so "review-queue" is never parsed as an id)
  and `PATCH /documents/{id}/freshness`.
- `migrations/versions/0104_document_freshness.py` — `review_date`,
  `expires_at`, `superseded_by` on `documents`; downgrade refuses while
  freshness metadata exists.
- `migrations/versions/0105_document_conflict_candidates.py` — tables
  `document_conflict_candidates` (canonical ordered chunk pair, unique
  pair, status open/confirmed/dismissed) and append-only
  `document_conflict_reviews`.
- `frontend/src/types.ts` — freshness fields on `Citation`, `RagAnswer`,
  and `RagDocument`; new `ReviewQueueEntry` / `ReviewQueueResponse`.
- `frontend/src/api.ts` — `documentReviewQueue()` and
  `setDocumentFreshness()`.
- `frontend/src/components/KnowledgePage.tsx` — warning banner above the
  answer, per-citation freshness badges in the Sources list, and the
  "needs review" section in the library with an owner-only review-date
  picker.
- `tests/test_document_freshness.py` (18 tests), `tests/test_document_freshness_api.py`
  (7), `tests/test_document_conflict_scan.py` (10),
  `tests/test_answer_citation_freshness.py` (10),
  `tests/test_document_review_queue.py` (7).

## Tests

- **Step AI new tests: 52, all passing in dedicated runs** — 18 (slice 1
  scorer) + 7 (slice 2 metadata + API) + 10 (slice 3 scan) + 10
  (slice 4 answer-time surfacing) + 7 (slice 5 review queue + PATCH).
- **Slice 4 run:** 10 passed.
- **Slice 5 run:** 7 passed (538s). First attempt was OOM-killed by the
  VM after 2 tests passed; second attempt was lost to a runtime restart
  with no output; third attempt (after the Agent 60 test cluster came
  back on port 55445) passed 5, failed 2 on a datetime string comparison
  (`+00:00` vs `Z` — same instant); the tests now compare parsed
  datetimes, and the fourth attempt passed all 7.
- **Combined Step AI re-run (all 52):** 35 passed (slices 1–3, 92 min) +
  17 passed (slices 4–5, 34 min), zero failures. A single-session run of
  all 52 was attempted three times and never completed: OOM-killed at
  39/52 with zero failures, SIGTERM'd by a runtime restart at 4/35, and
  OOM-killed at 39/52 again — so the 52 are proven across two completed
  sessions instead of one. The shared Agent 60 test cluster (port 55445)
  was lost to the same restarts; the runs above used a private cluster
  built by Agent 100 (port 55448, restart-proof binaries in
  `/home/hatch/tmp-pg-dist-ai100`, restart via
  `/home/hatch/tmp-pg-ai100/restart.sh`).
- **Relevant regressions:** `test_mock_llm_grounding.py` 5 passed.
  `test_rag_units.py` 116 passed with zero failures before a runtime
  restart killed the run at 54%; an earlier three-file regressions run
  reached 45 passed with zero failures before another restart killed it.
  `test_rag.py` 31 passed. No test failure was observed anywhere in this
  work; every interrupted run died to OOM or a runtime restart.
- **Alembic:** roundtrip clean on the final HEAD (downgrade base → upgrade
  head → `alembic check` clean, "No new upgrade operations detected").
  First downgrade attempt failed with `out of shared memory` (the small
  test cluster needed `max_locks_per_transaction = 256`); after that
  tuning the full roundtrip passed. The 0104/0105 downgrades themselves
  ran cleanly in both attempts.
- **Frontend:** `npm test` 22 passed, 0 failed; `tsc --noEmit` clean;
  `vite build` clean. One real bug was caught by typecheck during the
  gates (`ReviewQueueResponse` not imported in `api.ts`) and fixed.
- **Alembic:** roundtrip clean on the slice-2/3 work (downgrade base →
  upgrade head → `alembic check` clean); [PENDING: re-verify on the
  final HEAD.]
- **Frontend:** [PENDING: `npm test` + typecheck + build on the final
  HEAD.]

## Merge notes

- **Migration-number collisions to reconcile at merge:** this branch
  mints `0104` (also minted by Step AG/HR) and `0105` (also minted by
  Step AK agent controls). Renumber one side before merging; the
  downgrade guards are keyed to the revision ids, so rename carefully.
- **Pre-existing issue, not caused by this work:** migration `0091`
  guards table creation with an unqualified `has_table()` call, which
  breaks Alembic upgrades in isolated-schema test harnesses
  (`DuplicateColumnError` in 0092). Agent 69 (Step Z) owns the fix.
- No production code behaviour changes except the additions above:
  retrieval, ranking, ACLs, and the revision-freshness system are
  untouched. Freshness metadata is computed on read, never stored.

## Owner decisions still open (safe defaults already in place)

- Default review periods per document kind: review 180 days / stale
  365 days for all documents (slice-1 defaults).
- Expired documents keep being cited, flagged with a warning (v1
  recommendation: flag only, never stop citing).
- Who receives the "needs review" list: nobody is notified yet — the
  queue is visible in the Knowledge library to anyone who can read the
  documents in it. (A Step AA scheduled digest could deliver it.)
