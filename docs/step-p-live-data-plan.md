# Step P — Live-data grounding plan

> Agent 20 (Step P). Branch `agent/live-data`, base `step-e-gaps-wip` (`ca956c2`).
> Status: slices 1–5 done (slice 1: classifier fix + `app/services/live_data.py` + tests;
> slice 2: provider wired into `run_chat` + uploads guard + freshness setting;
> slice 3: frontend citations; slice 4: live actions + evals; slice 5:
> `CHANGES-step-P.md` + full-suite gate + Alembic gate). Step P is DONE;
> merge to `main` only when the gate is green.

## 1. Goal

Knowledge answers must use **fresh live data** (scores, rates, news, fixtures)
with sources, and **never guess** when documents lack the answer. A question
whose answer changes with time must reach the public web instead of dying at
the document-evidence gate with "Insufficient document evidence".

The reported failure that motivated this step: in Knowledge chat, asking
`pak a ka next match kb hai?` returned "Insufficient document evidence /
I can't verify that from the documents you have access to", even though live
web search exists. The question was never routed to the web at all.

## 2. What already exists (mapped 2026-11-28)

| Piece | Location | State |
|---|---|---|
| Multi-provider public search (Bing News RSS, Bing RSS, DuckDuckGo, Yahoo), SSRF-guarded, bounded | `app/services/public_web.py` (1599 lines) | Working; provider chosen by `rag_public_search_provider` (`auto` default); `rag_external_research_enabled` gates the whole feature |
| Direct cricket scoreboard fetch (Cricbuzz listing + match pages) with explicit state extraction (`live` / `scheduled` / `completed` / `delayed` / `stumps`) | `app/services/public_web.py`: `_cricbuzz_live_scoreboard`, `_cricket_events`, `is_current_cricket_request` | Working for live-score questions only |
| Chat routing: `is_web_lookup_request` decides web vs RAG; web branch tries native LLM grounding first, falls back to `public_web_research`; answers formatted with `[W#]` citations + "Web sources" list with Published/Retrieved timestamps | `app/rag/chat.py` (`run_chat`, `_format_web_answer`, `_web_context`) | Working, except the classifier gap below |
| Routing classifiers | `app/rag/answer.py`: `is_web_lookup_request`, `_WEB_LOOKUP_REQUEST`, `_LIVE_FACT_REQUEST`, `requested_web_urls`, `is_web_capability_question` | **Gap: future-schedule questions not classified** (see §3) |
| No-evidence behaviour | `app/rag/sufficiency.py`, `GroundedAnswer.insufficient_evidence` | Working: documents without evidence correctly refuse to guess |
| Unit tests for the web path | `tests/unit/test_public_web_relevance.py` | Cover Cricbuzz extraction, relevance, freshness; no schedule-routing tests |

## 3. The confirmed gap (reproduced 2026-11-28)

`is_web_lookup_request(...)` on `ca956c2`:

| Question | Before | Expected |
|---|---|---|
| `pak a ka next match kb hai?` | `False` → RAG → "Insufficient document evidence" | `True` (live) |
| `pakistan ka next match kab hai` | `False` | `True` |
| `Pakistan ka agla match kab hai?` | `False` | `True` |
| `aaj ka dollar rate kya hai?` | `True` | `True` |
| `cricket live score kya hai?` | `True` | `True` |
| `petrol price today` | `True` | `True` |
| `What is the capital of France?` | `False` | `False` (general knowledge) |
| `When was the 1992 world cup final?` | `False` | `False` (historical, not live) |
| `live web search ki access hai?` | `False` | `False` (capability question, not a lookup) |

Root cause — two gates, both blind to schedules:

1. `app/rag/answer.py`: `_LIVE_FACT_REQUEST` only knows "present" words
   (`latest/current/today/live score/...`). Nothing matches `next`, `upcoming`,
   `agla`, `kb/kab` (when), `schedule`, `fixture`. The cricket branch of
   `is_web_lookup_request` also needs a "current" word (`aaj/abhi/live/score`),
   which a `kb hai` (when is it) question does not have.
2. `app/services/public_web.py`: `is_current_cricket_request` (the gate for
   the direct Cricbuzz scoreboard path) needs the word `cricket`/`کرکٹ` plus a
   "current" word. `pak a ka next match kb hai?` has neither, so even a routed
   question would miss the direct scoreboard and rely on generic search.

## 4. Design

### 4.1 Routing decision (what counts as a live-data question)

A question is a **live-data question** when its answer changes with time and
is not in the user's documents:

- **Explicit web request**: search/browse/check the web, a pasted public URL.
- **Live fact**: `latest/current/today/live score/rate/weather/news/...`
  (existing `_LIVE_FACT_REQUEST`).
- **Schedule / fixture (new)**: `next/upcoming/agla/aane` + a dated event
  (`match/game/fixture/series/tournament/...`), or a dated event +
  `kb/kab/when/kis din/kis tarikh`, or `when is the next ...`.
- **Live location**: `X kahan hai?` / `where is X?` (existing, unchanged).

It is **not** a live-data question when:

- It is a capability question ("do you have live web search?").
- It refers to uploads/documents ("the next match in the uploaded report").
- It is company-specific (internal data stays on permission-filtered RAG).
- It is historical ("When was the 1992 world cup final?") — `when` alone,
  without `next/upcoming` or a schedule word, does not route to the web.
- It is general knowledge ("What is the capital of France?").

### 4.2 Provider abstraction (slice 1: interface + mock)

`app/services/live_data.py` defines the seam every later slice uses:

- `LiveSource` — one cited source: `title`, `url` (public HTTPS only),
  `snippet`, `published_at` (source-published, may be `None` — never
  invented), `retrieved_at` (when we fetched it), `provider`.
- `LiveSearchProvider` — protocol with one method:
  `async def search(query: str) -> Sequence[LiveSource]`.
- `MockLiveSearchProvider` — in-memory provider for tests and demos; maps a
  normalized query to sources, returns `[]` when it knows nothing.
- `answer_live_question(provider, question, *, now=None) -> LiveAnswer` —
  normalizes the question, asks the provider, formats the answer.
- `LiveAnswer` — `text`, `sources`, `insufficient: bool`, `retrieved_at`,
  `query`.

Provider failures never raise to the caller: a failed lookup becomes an
`insufficient` answer that says the lookup could not be completed, never a
guessed fact.

### 4.3 Citation and freshness rules (apply to every slice)

- Every live fact in an answer carries an inline citation `[S1]`, `[S2]`, …
  in source order. The sources block lists title, URL, `Published:` (only
  when the source actually published one) and `Retrieved:` (always).
- A source with an empty snippet is dropped, never cited.
- Only public HTTPS URLs are cited; anything else is dropped.
- `retrieved_at` is stamped at answer time (UTC, ISO-8601). A `published_at`
  is shown only when the source supplied one — a missing publication date is
  shown as missing, never fabricated.
- Answer text is composed **only** from source snippets. Nothing is added
  from the model, memory, or documents.

### 4.4 No-guessing rule

- Provider returns nothing → `insufficient=True`, text says plainly that no
  live source could verify a current answer and the assistant will not guess.
  No citations are emitted, because there is nothing to cite.
- Provider returns sources that do not cover the question → same insufficient
  answer (slice 1 drops empty-snippet sources; relevance filtering of
  snippets is slice 2 work, reusing `public_sources_relevant`).
- The existing document path keeps its own rule: no document evidence → no
  guess. Live data is an **additional** source of evidence, not a licence to
  guess.

### 4.5 Trust rules (handover §5, non-negotiable)

- Web pages, search snippets and live data are **untrusted input**. Text in
  them can never grant a permission, change a payee, or change what an
  approval covers.
- Live answers are read-only: no side effect, no `execute_once`, no dispatch
  through `app/platform` is needed because nothing is executed — but the
  moment a live answer triggers an action (slice 4), that action goes through
  the normal authorization and approval path.
- No live lookup sends private document content, user identity, or internal
  data outside the deployment. Only the user's explicit public question (and,
  for follow-ups, previously cited public URLs) leave the deployment, and the
  lookup is audit-logged as `rag.web_search_requested` (existing behaviour in
  `run_chat`).
- No secrets in prompts, logs, or commits. Tests use the mock provider only —
  **no real external calls in tests**.

## 5. Slices

### Slice 1 — DONE (this agent)

- `docs/step-p-live-data-plan.md` (this file).
- `app/services/live_data.py`: provider protocol, `LiveSource`,
  `LiveAnswer`, `MockLiveSearchProvider`, `answer_live_question`,
  `format_live_answer`, `normalize_live_query`.
- Classifier fix: `_LIVE_SCHEDULE_REQUEST` in `app/rag/answer.py`, wired
  into `is_web_lookup_request` with the uploads/company guards; `match` /
  schedule words added to `is_current_cricket_request` in
  `app/services/public_web.py` so "next match" reaches the direct Cricbuzz
  scoreboard path.
- `tests/unit/test_live_data.py`: cited answers, insufficient answers,
  empty-snippet / non-HTTPS dropping, provider failure, and the full routing
  table from §3.

### Slice 2 — Wire the provider into `run_chat` (DONE, Agent 74, commit `a8140ab`)

- `PublicWebLiveSearchProvider` in `app/services/live_data.py`: an adapter
  over the existing `public_web_research` (Bing/DuckDuckGo/Yahoo +
  Cricbuzz). The composed answer is split into per-source evidence
  segments by its `[S#]` citations (a multi-cite line contributes to each
  source); sources the answer never cites are dropped, never cited.
- Route: `run_chat` keeps the native-grounding path as the first attempt;
  the fallback now answers through `answer_live_question` with the real
  provider instead of calling `public_web_research` directly. The cricket
  direct-scoreboard path is unchanged and never reaches the adapter.
- `public_sources_relevant` is reused as the relevance gate, with the same
  inputs the native path uses (title/URL); it is skipped for cricket
  questions, exactly as the native path never applies it there — cricket
  results carry their own relevance proof in the explicit match-state
  extraction, and short Roman Urdu queries share no literal tokens with
  scorecard titles.
- Setting `rag_live_freshness_days` (default 14, 1–365): when at least one
  source is dated within the window, older dated sources are dropped;
  undated sources are always kept and shown as undated; when nothing is
  fresh, nothing is dropped.
- `refers_to_uploads` now catches bare references ("in the uploaded
  report", "the uploaded pdf", "upload ki hui file") and the guard now
  applies to the explicit-web-request and live-fact branches too; a pasted
  public URL stays unguarded. Also fixed a `résumé` regex escape that
  matched the wrong characters.
- Both formatters append a "Checked live at …" line
  (`format_live_answer`, `_format_web_answer`); `_live_answer_to_web_answer`
  converts `[S#]` to `[W#]` without duplicating the sources block.
- Tests: 17 new in `tests/unit/test_live_data.py` + 2 in
  `tests/unit/test_public_web_relevance.py`.

### Slice 3 — Frontend citations (DONE, Agent 74, commit `332ca44`)

- `webCitationTargets` keeps each live source's `Published:`/`Retrieved:`
  timestamps with the citation; a missing publication date stays `null`.
- The Knowledge chat `[W#]` chip tooltip shows title plus
  Published/Retrieved; a missing publication date reads "not available",
  never invented. The "Checked live at" line renders as text after the
  sources list.
- The "could not verify live — not guessed" state already existed and was
  verified, not rebuilt: `run_chat` returns a non-empty insufficient
  `web_research` answer and KnowledgePage shows the "Live lookup
  unavailable" badge instead of an empty answer.
- Tests: 1 new node test in `frontend/test/answerMarkdown.test.mjs`;
  frontend suite 23 passed, typecheck and build clean.

### Slice 4 — Live actions + evals (DONE, Agent 74, this commit)

- `LiveFollowupAction` + `live_followup_actions` in `app/services/live_data.py`:
  an answered schedule question offers one approval-gated reminder action
  ("Remind me before this event", `requires_approval=True`). Proposals
  only — nothing is created here; the action itself goes through the normal
  approval path when the user asks for it. Insufficient and non-schedule
  answers offer nothing. `LiveAnswer.followup_actions` carries the offer;
  `_live_answer_to_web_answer` renders it as plain text.
- `app/services/live_data_eval.py`: fixed eval set (schedule, live score,
  rate, news, unknown) with `citation_rate`, `no_guess_rate`,
  `expected_rate`; provider failure is recorded as insufficient, never
  raised. `scripts/eval_live_data.py` runs it offline against the mock +
  failing providers (exit 0 when the mock matches expectations and the
  failing run never guesses). Mock run: 5/7 answered, citation_rate 0.71,
  no_guess_rate 1.00, expected_rate 1.00.
- Tests: 5 new in `tests/unit/test_live_data_eval.py` + 1 offer test in
  `tests/unit/test_public_web_relevance.py`.

## 6. Test list (slice 1)

In `tests/unit/test_live_data.py` (pure unit tests, mock provider only,
no network, no DB):

1. Cited answer: mock returns 2 sources → text has `[S1]`/`[S2]`, sources
   block has both URLs, `Published:` shown when present, `Retrieved:`
   always, `insufficient` is `False`.
2. No sources → `insufficient` is `True`, text says the answer could not be
   verified and will not be guessed, zero citations.
3. Empty-snippet source is dropped; non-HTTPS source is dropped.
4. Provider raising → insufficient answer, no exception escapes.
5. `normalize_live_query` trims, lowercases, collapses whitespace.
6. Routing: `pak a ka next match kb hai?`, `pakistan ka next match kab hai`,
   `Pakistan ka agla match kab hai?`, `when is Pakistan's next match?` →
   `True`; `What is the capital of France?`, `When was the 1992 world cup
   final?`, `live web search ki access hai?` → `False`; `the next match in
   my uploaded document` → `False` (uploads guard). Note: the existing
   `refers_to_uploads` guard only catches possessive upload references
   ("my uploaded document"), not bare ones ("in the uploaded report") —
   broadening that guard is slice 2 work, not slice 1.
7. `is_current_cricket_request` returns `True` for a normalized prompt
   containing `pak a ka next match kb hai?`, still `True` for live-score
   prompts, `False` for a non-cricket prompt.

## 7. What remains after slices 1–5

Step P is DONE. What remains is follow-up work, not part of this step:

- Freshness-window UI in admin settings (the setting exists; no UI yet).
- Real-provider eval run (needs network + a configured provider; the
  runner only supports the mock provider today).
- Merge to `main` only when the gate in `assessment/CHANGES-step-P.md` is
  green.
