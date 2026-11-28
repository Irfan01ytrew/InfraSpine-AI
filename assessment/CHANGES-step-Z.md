# CHANGES — Step Z: Multilingual / Roman Urdu

Plain-language summary of what changed, why, and what is still owed before
merge. Branch `agent/multilingual`, base `ca956c2` (step-e-gaps-wip).
HEAD at writing: `8c5c675`. Diff vs base: **39 files, +2689 / −53**,
10 commits. Work by Agents 32, 69 and 112.

## What this step does (in plain words)

The software now understands the user in English, Roman Urdu (Urdu written
in Latin letters, e.g. "pak a ka next match kb hai?") and Urdu script. It can
detect which language a message is in, convert between scripts, understand
Roman Urdu / Urdu number, date and command words, remember each user's
preferred reply language, show the interface in that language (with
right-to-left layout for Urdu script), format money and dates the way the
user's locale expects, and tag voice-note transcripts with the language that
was heard.

**Safety rule that never changed:** language is only about understanding and
showing text. A detected language or a preference never changes
authorization, permissions, approvals, or what a user may read. Document,
web and message text stays untrusted — it can never grant anything or change
what an approval covers.

## Slices

### Slice 1 — Shared language helper (Agent 32, commits `9e56148`, `0651b8c`)
New pure module `app/language/`: `detect_language` (`en` / `roman_urdu` /
`urdu` / `mixed` / `unknown`), Roman Urdu ↔ Urdu script conversion,
digit/number/date/command normalization (`do sau` → 200, `paanch hazaar` →
5000, `dikhao` → `show`, `kb` → `when`), `normalize_for_matching`.
Deliberate: `kal` is kept as-is (it means both yesterday and tomorrow —
the caller resolves it from verb tense, never by guessing here); `saath`
is never mapped to 60. **27 new tests passed.**

### Slice 2 — Consolidate the three ad-hoc copies (Agent 69, commit `5d850f5`)
The three separate Roman Urdu regex copies in `app/rag/chat.py` (×2 sites),
`app/rag/answer.py` (`is_web_lookup_request`) and `app/services/public_web.py`
(stopwords/noise) now live once in `app/language/words.py` +
`contains_roman_urdu_words()`. Behaviour unchanged by construction **and**
by proof: routing regexes compile byte-identical, stopword/noise unions
identical, and a **60-question differential corpus vs the pre-change code
showed 0 differences**. Pinned by
`tests/unit/test_language_consolidation.py`.

### Pre-existing blocker fixed along the way (Agent 69, commit `a88a2fd`)
Migration `0091_agent_studio_durable_queue.py` checked `has_table(...)`
without a schema, so in an isolated-schema run it saw the public table and
skipped creating the table — then 0092's `ALTER TABLE` failed with
`DuplicateColumnError` and the whole Alembic upgrade rolled back. 0091 now
passes the current schema explicitly. Proven with a reproduction that
**failed before the fix and passed after**, plus new test
`tests/test_migration_0091_isolated_schema.py` (passed in pytest twice).
This bug was pre-existing, not caused by Step Z.

### Slice 3 — Per-user language preference (Agent 69, commit `1e4dbd3`)
New `UserLanguagePreference` model + migration
`0106_user_language_preference.py` (one row per user or none; unset =
detection, default English), service `app/language/preference.py`
(`get` / `set` / `resolve_reply_language` / `prefer_roman_urdu`),
`GET/PUT /api/language/preference` (caller-only rows — no user id in the
path, so one user can neither see nor change another's), wired into
`run_chat`'s four Roman-Urdu reply decisions. **Verification: 115 unit
tests passed in 47.48 s (`--noconftest`, final slice 2–3 tree); pure
resolve/prefer checks 14 passed; API round-trip passed in pytest;
handler-level route check 8/8 passed; direct production `run_chat`
wiring check 4/4 passed.**

Two real bugs found and fixed in this slice: (1) PUT returned 500 —
`set_language_preference` used generic `sqlalchemy.insert` without
`on_conflict_do_update`, fixed with the PostgreSQL dialect insert;
(2) a test question never matched the library-inventory check, so the
English wiring question was corrected (production behaviour unchanged).

### Slice 4 — Frontend switcher + translated strings + RTL (Agent 112, commit `c1151d0`)
`frontend/src/i18n.mjs` (nav translation table EN / Roman Urdu / Urdu
script for every label in `buildNav`, pinned by a test that reads the
labels out of `AppSidebar.tsx`; `tNav`, `isRtlLanguage`,
`localeForLanguage`, `translateNavGroups`, language store with
`restoreLanguage` respecting the user chip's manual direction override),
Settings → Language tab (`LanguageSection`, GET/PUT
`/api/language/preference`), preference loaded at sign-in in `App.tsx`,
sidebar translated in `AdminConsole`; Urdu script sets `dir="rtl"`.
**8 new frontend tests; full frontend suite 31 passed, `tsc` + build
clean.**

### Slice 5 — Locale-aware reports (Agent 112, commits `dc8a4f8`, `77275db`)
Backend pure module `app/language/reporting.py`: `parse_report_amount`
(Roman Urdu / Urdu number words, `2.5 lakh`, PKR / Urdu-digit amounts),
`parse_report_date_range` (`pichle mahine`, `is hafte`, `aaj`; `kal`
deliberately unparsed — ambiguous), `format_report_number` /
`format_report_money` / `format_report_date` (western grouping for
English, South Asian lakh/crore grouping for Roman Urdu / Urdu,
Urdu-script digits for Urdu). **11 new unit tests.** Frontend:
`formatMoneyForLanguage` / `formatDayForLanguage` (`format.ts`, Intl
with `localeForLanguage`), `spendMonthLabel` (`financeView.mjs`:
`2026-10` → `Oct 26` / `Aktobar 26` / `اکتوبر ۲۶`), wired into the
finance Spend tab. **1 new frontend test.** One real bug found and fixed
by the tests: Urdu money formatting left the decimal fraction in Latin
digits.

Note: no live backend call site for the slice-5 parsers exists on this
branch (there is no NL-query module here) — they ship as a tested library
for the Step X NL-reporting consumer, which wires them when it lands.
The formatting side is live in the finance Spend tab.

### Slice 6 — Voice language tagging (Agent 112, commit `351f48a`)
`transcribe_voice_upload` now returns `TranscribedVoiceQuestion` (text +
detected language from the shared detector typed chat uses);
`/api/rag/voice-question` exposes `detected_language` and records it in
the audit event (never the transcript text); the Knowledge page shows
"heard in Roman Urdu / Urdu / …" on temporary voice turns. The per-user
preference already feeds the voice reply path via `run_chat` (slice 3
wiring); the tag is metadata only and never changes authorization or
what the asker may read. **9 new unit tests passed (`--noconftest`).**

**Slice 4–6 backend unit total: 20 passed, 0 failed** (11 reporting +
9 voice tagging). **Frontend: 31 passed, 0 failed** (incl. 8 new i18n
tests + 1 new spendMonthLabel test); `tsc` rc=0, `vite build` rc=0
(20.16 s, backgrounded with `NODE_OPTIONS` heap caps). `node_modules`
was absent in this worktree — `npm ci` was run (224 packages); the
`answerMarkdown` failure seen before install was environmental
(`Cannot find package 'react'` at import), not Step Z code.

## Files changed (vs `ca956c2`, 39 files)

- Backend: `app/language/` (new: `__init__.py`, `detect.py`,
  `normalize.py`, `words.py`, `preference.py`, `reporting.py`),
  `app/api/routes/language.py` (new), `app/api/routes/rag.py`,
  `app/db/models.py`, `app/main.py`, `app/rag/answer.py`,
  `app/rag/chat.py`, `app/rag/voice_questions.py`, `app/schemas/rag.py`,
  `app/services/public_web.py`, `migrations/versions/0091_*` (fix),
  `migrations/versions/0106_user_language_preference.py` (new).
- Frontend: `frontend/src/i18n.mjs` + `.d.mts` (new),
  `frontend/src/components/LanguageSection.tsx` (new),
  `frontend/src/App.tsx`, `frontend/src/api.ts`,
  `frontend/src/components/AdminConsole.tsx`,
  `frontend/src/components/KnowledgePage.tsx`,
  `frontend/src/components/SettingsPage.tsx`,
  `frontend/src/finance/FinancePage.tsx`,
  `frontend/src/finance/financeView.mjs` + `.d.mts`,
  `frontend/src/format.ts`, `frontend/test/i18n.test.mjs` (new),
  `frontend/test/financeView.test.mjs`.
- Tests: `tests/test_language_preference.py`,
  `tests/test_migration_0091_isolated_schema.py`,
  `tests/unit/test_language_detection.py`,
  `tests/unit/test_language_normalize.py`,
  `tests/unit/test_language_consolidation.py`,
  `tests/unit/test_language_reporting.py`,
  `tests/unit/test_voice_language_tagging.py`.
- Docs: `docs/step-z-multilingual-plan.md` (slices 4–6 marked done),
  `assessment/CHANGES-step-Z.md` (this file).

## Test counts (exact, verified this session)

| Suite | Result |
|---|---|
| Slice 1 unit tests | 27 passed |
| Slices 2–3 unit tests (final tree) | 115 passed |
| Slice 3 pure resolve/prefer + API round-trip + route 8/8 + `run_chat` wiring 4/4 | all passed |
| 0091 isolated-schema migration test | passed twice in pytest |
| 60-question differential (old vs new code) | 0 differences |
| Slices 4–6 backend unit (`--noconftest`) | 20 passed, 0 failed |
| Frontend `node --test` | 31 passed, 0 failed |
| `tsc --noEmit`, `vite build` | clean |

## Still owed before merge (environmental, not code failures)

1. **DB-backed re-runs — NOT complete.** The combined 4-file attempt
   (`test_migration_0091_isolated_schema.py` +
   `test_language_preference.py` +
   `test_knowledge_general_routing.py` + `test_voice_question_api.py`)
   was OOM-killed (rc=137, zero pytest output, died in conftest setup).
   The single-file re-run of `test_voice_question_api.py` (slice 6 route
   regression) was **also OOM-killed (rc=137)** — checked
   2026-11-28 ~07:45 PKT; `/home/hatch/tmp-agent112/db-voice.log`
   contains only `pytest rc=137`, no test output. Agent 69's caveat
   therefore still stands: **the routing regressions
   (`tests/test_knowledge_general_routing.py`, 2 tests) have still never
   executed in pytest**. A quiet-VM re-run of the preference + routing +
   voice API tests is owed before merge. No test failed anywhere; every
   failure observed was the VM running out of memory.
2. **Full backend suite gate — not done.** Same VM-wide condition as
   every other step; the Step E verification (Agent 117) owns the merged
   HEAD gate.

## Merge notes

- **Migration `0104` renumber at merge.** Number 0104 is claimed on
  other branches by HR leave/attendance (Agent 44), Step AI freshness
  (Agent 60), finance autopilot (Agent 76), Step AB knowledge enrichment
  (Agent 89), Step G continuity (Agent 34) and Step AA scheduled reports
  (Agent 42), in addition to this branch's
  `0106_user_language_preference`. Number 0105 is claimed by Step AK
  (Agent 56) and Step AI (Agent 60). The language-preference migration
  is self-contained (one new table, no changes to existing tables), so it
  renumbers cleanly.
- **Consolidation NOTE markers:** Agent 12 (knowledge-fix) and Agent 20
  (live-data) branches change `is_web_lookup_request` and the
  `"pak next kb"` search query — the consolidation test carries NOTE
  markers to update those expectations when those branches merge.
- Step Z adds no new migration in slices 4–6. No production code outside
  `app/language/`, the RAG wiring, the language-preference API and the
  frontend was changed.

## Owner decisions needed

None new. Open ones from slice 1 remain: voice-note retention policy and
the Whisper fallback for voice questions (see
`docs/step-z-multilingual-plan.md` §7).
