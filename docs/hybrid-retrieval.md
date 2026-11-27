# Query understanding and hybrid retrieval

Status: implemented behind `RAG_HYBRID_RETRIEVAL` and `RAG_QUERY_ANALYSIS` (both on by default). Tunables:
`RAG_MAX_CHUNKS_PER_DOCUMENT` (3), `RAG_LEXICAL_CANDIDATES` (50). No LLM call is added by either switch.

## Problem

Retrieval was embedding-only (MiniLM + FAISS, global top 5). Measured on the development library:

* A typo ("what is IT polciy") dropped every similarity below the 0.2 floor; "policy" scored 0.40.
* An acronym ("IT") was read as the pronoun "it", so an independent question was merged with the previous one.
* Short, broad questions ("major issue in my company") matched weakly everywhere; chunks that literally say
  "issues requiring attention" ranked 541st.
* One repetitive document (93% of the chunks, near-identical log rows) filled most of every top 5.

## Approach

Everything extends the existing `retrieve()`, `run_chat` routing, evidence checks and PostgreSQL schema. No new
service or framework, and no word lists for acronyms, typos or company terms.

1. **Lexical index in PostgreSQL** (migration 0015). `document_chunks.search_vector`: a generated `tsvector`
   (`simple` configuration, so English, Roman Urdu and Urdu tokens are kept as written), GIN-indexed.
   `rag_terms(term, chunk_count)`: the corpus vocabulary, kept by statement-level triggers (insert, delete,
   update, truncate), with a `pg_trgm` index.
2. **Query analysis** (`app/rag/query_analysis.py`):
   * Terms: the question's words without generic function words (a standard English and Roman Urdu stopword
     set; nothing domain-specific). An all-capitals token in a mixed-case question is an acronym: it is kept as
     a term and is never read as a pronoun.
   * Weights: BM25-style IDF over **documents**, counted at query time for the question's few terms (terms in
     over 20,000 chunks count as common). Chunk counts would let one repetitive document make a word look
     common. A term in at most a fifth of the documents is *distinctive*.
   * Typo tolerance: a word of five or more letters that no chunk contains is matched to the vocabulary by
     trigram similarity and accepted within edit distance 1 (up to seven letters) or 2 (longer), preferring
     frequent terms. The original word stays in the query; the corrected wording is also searched
     semantically and is what the evidence checks read.
3. **Hybrid ranking** in `retrieve()`:
   * Candidates: FAISS (ACL-authorized, as before, four times `top_k`) and PostgreSQL full-text matches (ACL
     predicate in the same SQL; at most three chunks per document, each further chunk of a document weighted
     0.7 times the previous in the lexical order).
   * Fusion: reciprocal rank (k = 60). The semantic rank is taken over every candidate by cosine (lexical ones
     included), so a neighbour pool filled by one document cannot deny a lexical match its semantic credit.
     The lexical contribution is weighted by the share of the question's known words the chunk contains.
   * Eligibility: cosine at least `min_similarity` (as before), or lexical support (half or more of the known
     words, one of them distinctive) with a positive cosine. Otherwise nothing is returned: the existing
     refusal path answers.
   * Diversification: at most `RAG_MAX_CHUNKS_PER_DOCUMENT` chunks of one document while other documents have
     eligible chunks; free places are refilled from the capped documents.
   * `score` stays the cosine similarity, so traces, displays and thresholds keep their meaning.
   * Any lexical failure (for example, an unmigrated schema) runs in a savepoint and falls back to semantic
     search for that request.
4. **Context only when justified** (`run_chat`):
   * Follow-up detection runs on the question with acronyms masked.
   * A detected follow-up that names two or more corpus terms of its own is independent and searched alone.
   * A question with no corpus term at all ("where are most of them?") takes the previous question as context.
   * "it" or "this report" with no cited document to point at (the last answer was refused) is focused on the
     document the previous question retrieves first.
5. **Grounding**: the evidence checks (`assess_support`, the verifier) read the corrected wording, and match a
   question word of four or more letters against longer forms in the source ("issue" in "issues"). The model
   still sees the user's own question. The answer must cite retrieved passages, as before.

## Evaluation

`evals/run_query_understanding.py` seeds the synthetic golden corpus plus an IT policy, an inspection note, a
payment memo, a risk register, a CRM table and a 3,000-row door log that repeats "policy" and "issue". All 33
cases of `evals/query_understanding.json` run through production `run_chat` with both switches off (baseline)
and on. Retrieval is judged on the passages handed to the model, without a provider. `--answers` scores the
answers with the existing rubric (`app.rag.evaluation.answers`) and a configured provider (synthetic data
only). Reports are written to `evals/reports/query-understanding-*.json`.

## ACL

Every stage filters with the same `_scope` predicate (`access.readable_by`): FAISS candidates are authorized
before use, the lexical SQL carries the predicate, lexical-only candidates' embeddings are loaded with it, and
content is loaded with it again. Document frequencies and the vocabulary are corpus-wide (see trade-offs).

## Trade-offs

* `simple` text search does not stem. Words of four or more letters match as prefixes, which also matches some
  unrelated longer words ("issue" in "issued"); IDF, coverage and fusion keep their weight small.
* The vocabulary and document frequencies cover every document, so a correction or a weight can come from a
  document the user cannot read. Corrections are never shown and results are always ACL-filtered: only
  ranking is affected.
* A correction can be wrong (a rare real word close to a common one). The original is always searched too;
  a wrong correction can make the evidence check accept a passage about the corrected word. The answer still
  has to cite it.
* Generated column and triggers add write cost at ingestion (one vocabulary upsert per statement and term)
  and roughly the text's size again in index. Adding the column rewrites `document_chunks` once.
* Two extra small queries per question (term frequencies, document frequencies), and one more retrieval for a
  refused-then-"it" follow-up: p50 latency went from about 35 ms to about 100 ms on the evaluation corpus.
* Diversification can hide a fourth relevant chunk of one document (a cap of 2 measured slightly better on the
  evaluation set; 3 is kept for long single documents). Table analysis and whole-document questions are not
  diversified.
