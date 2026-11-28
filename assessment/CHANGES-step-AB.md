# Step AB — Knowledge improvements: tags, summaries, expiry alerts, Roman Urdu answers

Plain-language summary of what changed in the Knowledge library (the
documents the assistant answers from), why, and how it was tested.

## What the owner asked for

1. Documents should tag themselves, summarise themselves, and warn before
   they expire — so a contract is never found too late.
2. Answers should come back in Roman Urdu when the question is asked in
   Roman Urdu — same facts, same sources, just the owner's language.
3. The library should show the tags and summaries, flag expiring documents,
   and let the owner filter by tag.

## What was built (slices 1–5, all done)

- **Slice 1 — enrichment core** (`app/rag/auto_enrich.py`, Agent 35).
  Pure functions, no database needed: `extract_tags()` pulls allow-listed
  tags from the text (contract, invoice, finance, hr, meeting, …);
  `summarize_text()` returns up to 3 sentences copied word-for-word from
  the document (never invented); `extract_expiry_dates()` finds expiry
  dates only near expiry words like "valid until" or "expires on" (a date
  far from any expiry word is ignored); `expiry_alerts()` marks each date
  `expired` / `due_soon` (within 30 days) / `ok`; `detect_language()` /
  `answer_language()` recognise English, Roman Urdu and Urdu script.
- **Slice 2 — ingestion hook + backfill** (`app/rag/enrichment.py`,
  migration `0108_knowledge_enrichment`, Agent 89). When a document is
  ingested, the core above runs on its text and the tags, summary and
  expiry dates are stored on the document. The hook is best-effort:
  enrichment can never fail an ingestion. A backfill fills in documents
  that were already ready, reading only their stored text — no files are
  re-opened. Safe to re-run: only documents missing enrichment are touched.
- **Slice 3 — daily expiry-alert check** (`app/rag/expiry_alerts.py`,
  migration `0109_knowledge_expiry_alerts`, Agent 89). Run it with
  `python -m app.rag.expiry_alerts` (default warning window 30 days,
  `--warn-days N` to change it). It records one alert row per document,
  expiry date and checked day — running it twice on the same day records
  nothing twice. Pending alerts feed the Step AA scheduled digest
  (`pending_alerts_for_digest()` / `mark_alerts_delivered()`). Alerts are
  read-only notices: nothing here sends a message or takes any action, and
  delivery must only show a document to someone allowed to read it.
- **Slice 4 — Roman Urdu answers** (`app/rag/answer.py`,
  `tests/test_roman_urdu_answers.py`, Agent 109). The language of the
  question now drives the answer: a Roman Urdu question gets a Roman Urdu
  answer, an Urdu-script question gets an Urdu-script answer, English
  stays English. The facts, citations and the evidence gate never change —
  only the wording mirrors the question. "Insufficient document evidence"
  is still exact when there is no evidence, and the
  `INSUFFICIENT_EVIDENCE` marker the assistant uses internally is
  untouched. A refusal written by the assistant itself (for example its
  one-sentence explanation after the marker) is shown as the assistant
  wrote it — the assistant is already instructed to write it in the
  question's language.
- **Slice 5 — library UI** (Agent 109). The document library now shows
  each document's tags (click a tag to filter by it), its summary (up to
  two lines), and an expiry badge ("Expires in 5 days", "Expires today",
  "Expired 27 Nov 2026"). A tag filter sits next to the status filter,
  and the library search now matches filenames, tags and summaries. The
  new columns are exposed through `DocumentOut` (`tags`, `summary`,
  `expiry_date`) — only for documents the viewer may already read, the
  same boundary as every other field the library shows. Pure helpers in
  `frontend/src/knowledge/documentMeta.mjs` (expiry math in UTC days so
  the count never shifts with the viewer's timezone).

## Files changed

- `app/rag/auto_enrich.py` — slice-1 pure core (tags, summary, expiry, language).
- `app/rag/enrichment.py` — slice-2 hook + backfill over the slice-1 core.
- `app/rag/expiry_alerts.py` — slice-3 daily check + digest helpers.
- `app/rag/answer.py` — slice-4 language wiring (prompt instruction +
  localised canned replies).
- `app/rag/ingestion.py` — slice-2 hook call (best-effort try/except).
- `app/db/models.py` — `Document.tags` / `summary` / `expiry_date` /
  `expiry_dates` / `enriched_at`, `KnowledgeExpiryAlert` table.
- `app/schemas/rag.py` — `DocumentOut.tags` / `summary` / `expiry_date`.
- `migrations/versions/0108_knowledge_enrichment.py` — new document columns.
- `migrations/versions/0109_knowledge_expiry_alerts.py` — alerts table.
- `frontend/src/knowledge/documentMeta.mjs` (+ `.d.mts`) — expiry/tag/search helpers.
- `frontend/src/components/KnowledgePage.tsx` — tags, summary, expiry badge, tag filter.
- `frontend/src/types.ts` — `RagDocument.tags` / `summary` / `expiry_date`.
- `tests/test_knowledge_auto_enrich.py` — 36 slice-1 tests.
- `tests/test_knowledge_enrichment.py` — 14 slice-2/3 tests.
- `tests/test_roman_urdu_answers.py` — 20 slice-4 tests.
- `frontend/test/documentMeta.test.mjs` — 6 slice-5 helper tests.
- `docs/step-ab-knowledge-plan.md` — plan (slices 1–5).

## Tests

- Slice 1 core: **36 passed** (`tests/test_knowledge_auto_enrich.py`).
- Slice 4 Roman Urdu answers: **20 passed**
  (`tests/test_roman_urdu_answers.py`, incl. "pak a ka next match kb hai?"
  → Roman Urdu insufficient-evidence answer, assistant never called).
- Slice 5 frontend helpers: **6 passed**
  (`frontend/test/documentMeta.test.mjs`).
- Full frontend suite: **28 passed, 0 failed** (`npm test`).
- TypeScript: **clean** (`tsc --noEmit`).
- Slice 2/3 pure checks (Agent 89, standalone scripts): **13/13** passed.
- Slice 2/3 DB checks (Agent 89, standalone scripts on real rows): **24/24** passed.
- Slice 2/3 canonical pytest file: **14 tests**
  (`tests/test_knowledge_enrichment.py`) — **NOT green: OOM-killed
  (exit 137) on the 4th consecutive attempt** (Agent 89's 3 + this one).
  This run reached 3 passed, 0 failed (the 3 pure tests) before the
  kernel killed the pytest process during the first DB test; no test
  failed. Cause is VM memory pressure (load 30–50, ~40 agents on
  pytest), not a code failure: Agent 89's standalone DB checks of the
  same production functions passed 24/24 on real rows. Needs a
  quiet-VM re-run before merge — do not claim this gate green.
- Slice 1 regression re-run under conftest
  (`tests/test_knowledge_auto_enrich.py`, 36 tests) and slice 4 under
  conftest (`tests/test_roman_urdu_answers.py`, 20 tests): **still
  running** in the background at report time (job pid 10873, log
  `/tmp/ab109-backend-gates.log`); even the pure slice-1 file had not
  finished collection after ~8 minutes under the same VM load.
  Slice 1 passed 36/36 for Agent 35; slice 4 passed 20/20 standalone
  for this agent.
- Production build: **not green: `npm run build` OOM-killed once
  (exit 137)** during the vite step, under the same shared-VM load
  (tsc `--noEmit` itself passed clean before vite started). Retry in a
  quieter window; do not claim this gate green.
- Alembic: **roundtrip clean** — upgrade 0103 → 0104 → 0105 (head),
  downgrade 0105 → 0104 → 0103, upgrade back to head
  `0109_knowledge_expiry_alerts`, `alembic check` = "No new upgrade
  operations detected" (throwaway DB on a private PostgreSQL 16 cluster,
  127.0.0.1:55457).
- Full backend suite on this branch: not run (branch gate is the focused
  suites above; the full-suite gate is owned by the Step E verification).

## Merge notes

- **Migration-number collisions (renumber at merge):** `0104` and `0105`
  are also used on other branches — Agent 44's `0104_hr`, Agent 60's
  `0104`/`0105` (AI freshness), Agent 56's `0105_agent_controls`. All
  branch off `0103`, so the numbers (not the revision IDs) collide and
  several Alembic heads will need renumbering when the branches land.
- No production data, permissions or secrets were touched. Enrichment and
  alerts are metadata and read-only notices only.

## Owner decisions still open (safe defaults already in place)

- Expiry warning windows: default **30 days** is implemented
  (`--warn-days N` overrides it); the plan also mentions a 7-day window —
  confirm whether both windows should alert, or 30 days alone is enough.
- Which document types matter most for tags: contracts are already the
  strongest tag source; confirm the priority order if others (invoices,
  HR policies) should rank differently.
