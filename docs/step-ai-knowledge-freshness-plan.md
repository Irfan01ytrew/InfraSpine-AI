# Step AI — Knowledge freshness plan

Owner-approved step: flag stale or contradictory policies and documents, and always
show the source/citation. Flagging is advisory — it never deletes or rewrites a
document. A flagged document stays readable until an authorized human acts on it.

## 1. What exists vs what is missing (mapped at `step-e-gaps-wip` `ca956c2`)

| Area | Exists | Missing |
|---|---|---|
| Document identity | `Document` (`app/db/models.py:542`): `id`, `filename`, `version`, `indexed_version`, `created_at`, `updated_at`, `indexed_at`, `status`, `source_uri`, `source_system`, `descriptor` | No `review_date`, `expires_at`/`valid_until`, or `superseded_by` column |
| Old versions | `DocumentVersion` (`app/db/models.py:637`): immutable archived files, `archived_at`, `reason` (`replacement` default) — superseded versions ARE retained | Nothing links a live document to "a newer version exists"; the live row never says it is superseded |
| Citations | Answers cite every factual statement (`app/rag/answer.py` system prompt, `[S1]` tags); `Citation` schema (`app/schemas/rag.py:188`) carries `document_id`, `filename`, `page_number`, `locator`, `snippet` | Citations carry no document age, version, or freshness — a reader cannot tell whether the cited source is stale |
| Contradictions | `app/rag/conflict_detection.py`: `potential_conflict(left, right, semantic_similarity)` — conservative lexical + bounded-alias triage, human-review candidate only; wired into `app/services/knowledge.py` for knowledge *statements* | No contradiction check across document *passages* (the text answers are actually built from); no flag is ever shown to the reader |
| Freshness | **Knowledge revisions DO have it:** `KnowledgeRevision` (`app/db/models.py:2076`) has `last_reviewed_at` / `review_due_at` (365-day cycle), a `/api/governance/knowledge/stale` endpoint, scoped freshness-review decisions, append-only `knowledge_freshness_reviews`, and overdue revisions are withheld from retrieval (`tests/test_knowledge_freshness.py`) | No freshness at all for **documents** (`documents` table): no score, no stale flag, no review date, no expiry, no "needs review" list for uploaded files |
| Contradictions | `app/rag/conflict_detection.py`: `potential_conflict(left, right, semantic_similarity)` — conservative lexical + bounded-alias triage, human-review candidate only; wired into `app/services/knowledge.py` for knowledge *statements* | No contradiction check across document *passages* (the text answers are actually built from); no flag is ever shown to the reader |

## 2. Design rules

1. **Advisory only.** A stale/contradictory flag is a label + reason, never a deletion, rewrite, or retrieval change. Removing or replacing a document stays a human action through the existing document APIs.
2. **Pure first.** Freshness scoring is a pure function of document metadata + policy + `as_of`. No DB, no network, no model call — so it is testable without fixtures and cheap to run on every document.
3. **Policy, not hard-coding.** Thresholds (`review_after_days`, `stale_after_days`, per-kind review periods) live in a `FreshnessPolicy` object; an org setting may override them later. Never hard-code "policies expire after N days" for a specific document kind.
4. **Superseded beats age.** If a newer version of the same document exists, the old one is `superseded` (score 0) regardless of how young it is.
5. **Expiry beats review.** A passed `expires_at`/`valid_until` is `expired` (score 0), even if the document was reviewed yesterday.
6. **Indexed content is what counts.** If `indexed_version != version`, the searchable text is older than the submitted file — the score is penalised and the reason says so. A `failed` document scores 0 with reason "not indexed".
7. **Contradictions need a human.** `potential_conflict` only ever produces a *candidate* with both cited sources. Similarity never decides truth or publication; a reviewer compares the two cited passages.
8. **Every answer shows its sources' freshness.** A citation is displayed with its document's filename, version, indexed date and freshness status — so "this answer comes from a 2-year-old policy" is visible, not hidden.
9. **Document text is untrusted.** A document can never grant permissions, change a policy's review date, or mark itself fresh. Review dates and expiry come from metadata/owner actions, never from document content.
10. **Dates are Asia/Karachi.** `as_of` defaults to the org's today; all age arithmetic is in whole days.

## 3. Freshness model (slice 1)

`app/rag/freshness.py` (stdlib only, no DB/network/app imports except
`app.rag.conflict_detection` for the contradiction helper):

- `FreshnessPolicy(review_after_days=180, stale_after_days=365, unindexed_penalty=25, review_overdue_penalty=40)` — thresholds.
- `DocumentMeta` — the metadata the scorer needs: `document_id`, `filename`,
  `created_at`, `updated_at`, `indexed_at`, `version`, `indexed_version`,
  `status`, `review_date=None`, `expires_at=None`, `superseded=False`.
- `score_freshness(meta, *, as_of, policy=...) -> FreshnessResult`:
  - `score` 0–100, `status` one of `fresh | aging | stale | expired | superseded`,
    `reasons: list[str]` in plain language.
  - Decision order: `superseded` → `expired` (passed `expires_at`) → `failed`
    status → age/review-date bands (`fresh` < review date, `aging` past review
    date but < stale threshold, `stale` beyond it) → `indexed_version` mismatch
    penalty.
- `detect_contradiction(left, right, *, semantic_similarity=None) -> ContradictionFlag | None`
  — wraps `potential_conflict`; each side is a `CitedPassage(document_id, filename, text)`.
  Returns both sources + `needs_human_review=True` when a candidate is found, else `None`.
- `citation_freshness_note(meta, result) -> str` — one display line, e.g.
  `"Source: Leave Policy.pdf v3, indexed 2026-03-02, freshness: aging (review due 2026-09-01)"`.

## 4. Slices 2–5 (what remains after slice 1)

- **Slice 2 — metadata + API:** migration adding `review_date`, `expires_at`,
  `superseded_by` to `documents` (Alembic roundtrip + check clean); freshness
  computed on read from `Document` rows; `GET /api/knowledge/documents` returns
  `freshness` per document through `app/platform` (`documents.read`).
- **Slice 3 — contradiction scan:** nightly worker over newly indexed passages
  (same-document and cross-document candidates via `conflict_search_query`),
  storing `knowledge_conflict_candidates` for human review; never auto-resolving.
- **Slice 4 — answer-time surfacing:** citations in chat responses include
  freshness status; answers citing only stale/expired sources carry a visible
  warning and still answer from the sources (no guessing, no silent downgrade).
- **Slice 5 — review queue UI + docs:** Knowledge page "Needs review" list
  (stale/expired/contradicted, sorted by impact), owner sets review dates,
  `CHANGES-step-AI.md` + full suite/Alembic/frontend gates, then merge to main.

## 5. Test list (slice 1)

`tests/test_knowledge_freshness.py` — pure functions, no DB needed:

- Fresh document (recent, reviewed, indexed) → `fresh`, score 100, no reasons.
- Aging: past `review_date` but under stale threshold → `aging`, reason names the review date.
- Stale: age past `stale_after_days` → `stale`.
- Expired: passed `expires_at` → `expired`, score 0 (even if recently reviewed).
- Superseded → `superseded`, score 0 (even if 1 day old).
- `indexed_version != version` → penalty + "searchable content is older" reason.
- `failed` status → score 0, "not indexed" reason.
- Policy override changes the bands (e.g. 30-day review period).
- Contradiction: opposing policy statements ("must keep" vs "must delete") → flag with both sources; same-statement pair → `None`; unrelated pair → `None`.
- Citation note contains filename, version, indexed date and status.

## 6. Owner inputs needed

- Default review periods per document kind (policies vs contracts vs general docs).
- Whether expired documents should stop being cited at all (v1: no — flag only).
- Who receives the "needs review" list (owner only, or department managers too).
