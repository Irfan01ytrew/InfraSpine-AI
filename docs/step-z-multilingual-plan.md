# Step Z — Multilingual + Roman Urdu voice plan

Owner language first: the whole software speaks the owner's language — Roman Urdu
and Urdu script everywhere, plus English. The owner writes Roman Urdu; the
founder scenario (Step H) is a Roman Urdu voice note. Language must be a shared
capability, not copy-pasted regexes in every module.

## 1. What exists today (verified 2026-11-28, branch `step-e-gaps-wip` @ `ca956c2`)

- **UI is English-only.** All frontend strings are hardcoded English
  (`frontend/src/**/*.tsx`); no i18n library in `frontend/package.json`, no
  language switcher, no RTL layout. One piece of plumbing exists:
  `frontend/src/format.ts` `RegionalSettings` (locale / timeZone / currency,
  from Company setup) — used for number/date formatting only, never for
  language.
- **Roman Urdu is understood ad-hoc, in at least three places, with duplicated
  word lists:**
  - `app/rag/chat.py` ~line 342 and ~line 558 — two inline regexes deciding
    whether to answer in Roman Urdu (`kya|kia|btao|batao|...|hain|hai`,
    `kitna|kitni|kitne|...|mai|ab|tk`).
  - `app/rag/answer.py` `is_web_lookup_request` (Agent 12, 2026-11-28) —
    Roman Urdu future words (`kb|kab|next|agla|...`) + Urdu script, routing
    live questions to web search.
  - `app/services/public_web.py` — Roman Urdu stopwords for search queries
    (`kb, kab, kahan, kidhar, kis, kon, ...`).
  - Same words, three copies: they will drift. There is no shared helper.
- **Voice exists, language is untagged.** `app/rag/voice_questions.py` —
  Whisper transcription (local model, API fallback), max 120 s / 8 MB; the
  transcript is plain text with no detected-language tag, so downstream code
  cannot tell a Roman Urdu voice note from an English one.
- **Urdu script documents ingest.** `tests/unit/test_urdu_ocr.py` — Urdu OCR
  is covered; documents in Urdu script are searchable.
- **Handover rule §5.8:** the owner writes Roman Urdu; default timezone
  Asia/Karachi. No language preference is stored anywhere (no user/org
  setting, no `locale` column).

## 2. Gaps

1. No shared language detection / normalization module.
2. No per-user (or per-org) language preference; replies guess from the
   question each time.
3. No translated UI strings, no language switcher, no RTL for Urdu script.
4. Dates / numbers / currency in reports are formatted for one locale only;
   Roman Urdu number words (`do sau`, `paanch hazaar`) and Urdu-script digits
   (`۵`, `۳۲`) are not understood by any parser.
5. Voice transcripts carry no language tag; Roman Urdu voice commands are not
   a first-class input.

## 3. Slices (in order; each is code + tests + docs, small commits)

1. **Shared helper (this task, Agent 32).** Pure functions, no DB, no network:
   `app/language/` — `detect_language`, `contains_urdu_script`,
   `is_roman_urdu`, Roman Urdu ↔ Urdu script transliteration of key phrases,
   Urdu-script/Arabic digit → ASCII, Roman Urdu/Urdu number words → digits
   (incl. `do sau` → 200, `paanch hazaar` → 5000), date/command words →
   canonical English tokens (`aaj` → `today`, `pichle mahine` → `last month`,
   `dikhao` → `show`, `kb` → `when`), and `normalize_for_matching` combining
   them. Tests: `tests/unit/test_language_detection.py`,
   `tests/unit/test_language_normalize.py` (pure unit tests, no DB).
2. **Consolidate the three ad-hoc copies.** Replace the inline regexes in
   `app/rag/chat.py`, `app/rag/answer.py`, `app/services/public_web.py` with
   calls into `app/language/` — behaviour unchanged, proven by the existing
   suites (Agent 12's routing tests, chat tests). Do not change reply wording
   in this slice.
3. **Language preference.** Per-user setting (`en` / `roman_urdu` / `urdu`),
   stored on the user/org record via migration, exposed in settings API;
   reply language follows preference first, detected language second.
   Preference never affects authorization or what a user may see.
4. **Frontend switcher + translated core strings + RTL.** Language switcher
   in settings; core navigation/labels translated (EN / Roman Urdu / Urdu
   script); `dir="rtl"` layout for Urdu script; dates/numbers via the
   existing `RegionalSettings` locale.
5. **Locale-aware reports.** Finance/NL-reporting dates, amounts and currency
   formatted per locale; parsers accept Roman Urdu/Urdu number and date words
   (built on slice 1).
6. **Voice language tagging.** Tag Whisper transcripts with
   `detect_language`; Roman Urdu voice commands route through the same
   normalization as typed chat.

## 4. Rules

- Pure helpers stay pure: no imports from `app.platform`, no DB, no network.
  Language detection must never change authorization, permissions, approvals,
  or what a user may read — it only changes how text is understood and shown.
- Document/web/message text stays untrusted: a detected language never grants
  anything and never changes what an approval covers.
- `kal` (کل) means both yesterday and tomorrow — the helper keeps the token
  `kal` and documents the ambiguity; callers resolve it from verb tense,
  never by guessing here.
- Tests only on throwaway `*_test` databases; unit tests for this module need
  no DB at all. Keep pytest log capture on.

## 5. Status

- Slice 1 (Agent 32): plan + `app/language/` helper + tests — done
  (`9e56148`, `0651b8c`; 27 tests).
- Slice 2 (Agent 69): consolidation of the three ad-hoc Roman Urdu copies
  into `app/language/words.py` + `contains_roman_urdu_words` — done.
  Behaviour verified identical by a differential run of the old code vs the
  new code (60-question corpus, 0 differences) and pinned by
  `tests/unit/test_language_consolidation.py`. The routing regexes in
  `app/rag/answer.py` compile to byte-identical patterns; the stopword sets
  in `app/services/public_web.py` are unchanged unions.
- Slice 3 (Agent 69): per-user language preference — done.
  `user_language_preferences` table (migration `0104`), service
  `app/language/preference.py`, `GET/PUT /api/language/preference`
  (caller-only rows), `resolve_reply_language` / `prefer_roman_urdu`,
  wired into `run_chat`'s Roman-Urdu reply decisions. Merge note:
  migration number `0104` is also claimed by Agent 44's HR branch and by
  Agent 60's Step AI freshness branch, and `0105` is claimed by both Step AK
  (Agent 56) and Step AI (Agent 60) — renumber at merge; the language
  preference migration is self-contained (one new table, no changes to
  existing tables), so it renumbers cleanly.
- Blocker fixed (Agent 69, pre-existing): migration 0091's schema-less
  `has_table()` saw public's table in isolated-schema runs and broke the
  upgrade at 0092 (`DuplicateColumnError`); it now passes the current
  schema explicitly. Proven before/after on throwaway schemas and by
  `tests/test_migration_0091_isolated_schema.py`.
- Slices 4–6: not started.
- Slice 4 (Agent 112): frontend switcher + translated core strings + RTL
  — done (`c1151d0`). `frontend/src/i18n.mjs` (nav translation table EN /
  Roman Urdu / Urdu script for every label in `buildNav`, pinned by a
  test that reads the labels out of `AppSidebar.tsx`; `tNav`,
  `isRtlLanguage`, `localeForLanguage`, `translateNavGroups`, language
  store with `restoreLanguage` respecting the user chip's manual
  direction override), Settings > Language tab (`LanguageSection`,
  GET/PUT `/api/language/preference`), preference loaded at sign-in in
  `App.tsx`, sidebar translated in `AdminConsole`. Urdu script sets
  `dir="rtl"`. 8 new frontend tests; full suite 31 passed, tsc + build
  clean.
- Slice 5 (Agent 112): locale-aware reports — done (`dc8a4f8` backend,
  `77275db` frontend). Backend pure module `app/language/reporting.py`:
  `parse_report_amount` (Roman Urdu / Urdu number words, `2.5 lakh`,
  PKR / Urdu-digit amounts), `parse_report_date_range` (`pichle
  mahine`, `is hafte`, `aaj`; `kal` deliberately unparsed — ambiguous),
  `format_report_number` / `format_report_money` / `format_report_date`
  (western grouping for English, South Asian lakh/crore grouping for
  Roman Urdu / Urdu, Urdu-script digits for Urdu). 11 new unit tests.
  Frontend: `formatMoneyForLanguage` / `formatDayForLanguage`
  (`format.ts`, Intl with `localeForLanguage`), `spendMonthLabel`
  (`financeView.mjs`), wired into the finance Spend tab. Note: no live
  backend call site for the parsers exists on this branch (no NL-query
  module here) — they ship as a tested library for the Step X
  NL-reporting consumer, which wires them when it lands.
- Slice 6 (Agent 112): voice language tagging — done (`351f48a`).
  `transcribe_voice_upload` returns `TranscribedVoiceQuestion` (text +
  detected language from the shared detector); `/api/rag/voice-question`
  exposes `detected_language` and records it in the audit event (never
  the transcript text); the Knowledge page shows "heard in Roman Urdu /
  Urdu / ..." on temporary voice turns. 9 new unit tests pass
  (`--noconftest`). The per-user preference already feeds the voice
  reply path via `run_chat` (slice 3 wiring); the tag is metadata only
  and never changes authorization or what the asker may read.
- Merge notes (updated): migration `0104` is now also claimed by Step G
  continuity (Agent 34) and Step AA scheduled reports (Agent 42), in
  addition to HR (Agent 44), Step AI freshness (Agent 60), finance
  autopilot (Agent 76) and Step AB (Agent 89) — renumber at merge; the
  language-preference migration stays self-contained (one new table).
  Step Z adds no new migration in slices 4–6.
- Owed before merge (environmental, not code): the DB-backed re-run of
  `tests/test_language_preference.py` + `tests/test_knowledge_general_routing.py`
  (Agent 69's caveat) plus `tests/test_voice_question_api.py` (slice 6
  route regression) — Agent 112's combined 4-file attempt was OOM-killed
  (rc 137, zero pytest output, died in conftest setup); a single-file
  voice-test re-run was in progress at report time.
