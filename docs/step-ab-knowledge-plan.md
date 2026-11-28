# Step AB — Knowledge AI improvements plan

Owner-approved 2026-11-28. Goal: make Knowledge smarter without touching its
trust rules — documents auto-tagged and summarized on upload, contract/deadline
expiry alerts before the date passes, and answers in Roman Urdu when the owner
asks in Roman Urdu. Agent 35 (prep + slice 1).

## What exists (mapped, not rewritten)

- **Ingestion** (`app/rag/ingestion.py`, `app/rag/parsers/`, `app/rag/chunking.py`,
  `app/rag/embeddings.py`): upload → parse → chunk → embed → vector store, with a
  worker, retries and failed-status handling. No enrichment step: nothing derives
  tags, a summary, or dates from the text.
- **Document model** (`app/db/models.py`, `Document`, `documents` table): filename,
  file_format, status (`pending/processing/ready/failed`), visibility, ACL
  (`acl_status`, quarantine), owner, source. **No tags, no summary, no expiry /
  deadline column, no metadata JSONB for derived info.** Derived data has nowhere
  to live yet — slice 3 adds a migration.
- **Chat / answers** (`app/rag/chat.py`, `app/rag/answer.py`): Roman Urdu is
  handled only in spots — a media-evidence intro (chat.py ~342) and the library
  inventory intro (chat.py ~558) switch to Roman Urdu when Roman Urdu words are
  detected, and `answer.py` (~572) understands "urdu mein batao" style follow-ups.
  The main grounded-answer path has **no general "answer in the language asked"
  rule**: a Roman Urdu question about a document can still be answered in English.
  Agent 12 (commits `0e0ca3a`, `7bf321d`) fixed web-search *routing* for Roman
  Urdu questions; it did not change answer language.
- **Dates / expiry**: no expiry or deadline extraction exists anywhere in
  `app/rag/` (only token/session `expires_at` columns and one "deadline" word in
  a sufficiency pattern). Nothing alerts the owner before a contract ends.
- **Voice questions** (`app/rag/voice_questions.py`): spoken questions are
  transcribed to text, then flow through the same chat path.

## What is missing

1. Auto-tagging: no document-type/topic tags (contract, invoice, HR, meeting…).
2. Summary: no short summary per document for the library list / chat context.
3. Expiry extraction + alerts: no dates pulled from contract text, no warning
   before a deadline.
4. Roman Urdu answers: no general language-mirroring for grounded answers.

## Design rules (non-negotiable)

- **Document text is untrusted input.** Tags, summaries and dates are *derived
  metadata about* the text — they can never grant permissions, change payees, or
  change what an approval covers. They are stored as metadata only.
- **Never change the document.** Enrichment writes metadata columns only; the
  stored file and indexed chunks are untouched.
- **Extractive only, no invention.** Summaries quote sentences from the document
  itself; the tagger uses an allow-listed keyword taxonomy; dates must be found
  in the text next to an expiry keyword. Nothing is guessed — no date found means
  no expiry, never a made-up one.
- **Permission-filtered, always.** Tags/summaries/expiry are visible only to
  users who may read the document (`documents.read` via `app/platform`); the
  enrichment step itself runs inside ingestion, after the document is stored.
- **Deterministic, stdlib-only core.** The tagger/summarizer/date extractor are
  pure functions (no LLM, no network, no DB) so they are cheap, testable and
  cannot hallucinate. An LLM pass may be added later as an *optional* refinement,
  behind the same extractive guardrails — not in this step.
- **Language mirroring is presentation only.** It changes the wording of the
  answer, never the facts, sources, or evidence gate. "Insufficient document
  evidence" stays exact when there is no evidence.
- Tests only on throwaway `*_test` databases; pytest log capture stays on.

## Slices

1. **(DONE — this agent)** Pure-function enrichment core:
   `app/rag/auto_enrich.py` — `extract_tags()`, `summarize_text()`,
   `extract_expiry_dates()`, `expiry_alerts()`, `detect_language()` /
   `answer_language()` — + `tests/test_knowledge_auto_enrich.py`.
2. **(DONE — Agent 89, resuming Agent 51's uncommitted work)** Ingestion hook:
   run the core on the parsed text during ingestion, store tags/summary/expiry
   as document metadata (migration `0108_knowledge_enrichment` adding the
   columns), backfill for already-ready documents, tests on a throwaway DB.
   Hook is best-effort: enrichment failure never fails ingestion.
3. **(DONE — Agent 89)** Expiry alerts: a daily check (`python -m
   app.rag.expiry_alerts`, migration `0109_knowledge_expiry_alerts`) that
   records one alert row per (document, expiry date, checked day) for expired
   / due-soon (30-day, `--warn-days`) documents; idempotent per day; pending
   rows feed the Step AA digest (`pending_alerts_for_digest`,
   `mark_alerts_delivered`). Alerts are read-only notices, never actions.
   Verified: pure checks 13/13, DB checks 24/24, Alembic roundtrip + check
   clean on a throwaway DB. Canonical pytest file still needs a quiet-VM
   re-run (OOM-killed twice under shared-VM load).
4. Roman Urdu answers: wire `answer_language()` into the grounded-answer path
   so a Roman Urdu question gets a Roman Urdu answer (same facts, same
   citations); tests incl. the "pak a ka next match" style questions.
5. UI: tags + summary shown in the document library, expiry badge on contracts,
   filter by tag. `CHANGES-step-AB.md` + full suite / Alembic / frontend gates,
   then merge to main.

## Test list (slice 1)

- Tags: contract text → `contract`; invoice text → `finance`/`invoice`;
  HR policy → `hr`; meeting notes → `meeting`; mixed text → several tags;
  empty/gibberish text → no tags; tag count capped.
- Summary: returns ≤ 3 sentences, every sentence copied verbatim from the
  source text; empty text → empty summary; short text → the text itself.
- Expiry dates: "valid until 31 December 2026", "expiry: 2026-03-15",
  "expires on 12/03/2026" all found; a date far from any expiry keyword is
  ignored; no date → empty list; ambiguous dates never invented.
- Alerts: past date → `expired`; within 30 days → `due_soon`; later → `ok`;
  days-left counts correct.
- Language: Roman Urdu questions → `roman_urdu`; Urdu script → `urdu`;
  English → `english`; mixed Roman Urdu + English → `roman_urdu`.

## Owner inputs needed

- Which expiry warning windows (default 30 and 7 days)?
- Which document types matter most for tags (contracts first)?
