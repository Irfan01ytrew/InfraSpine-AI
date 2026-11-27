# Document discovery and hierarchical retrieval

Status: implemented behind `RAG_HIERARCHICAL_RETRIEVAL` (on by default). Tunables: `RAG_DISCOVERY_MAX_DOCUMENTS` (3),
`RAG_SMALL_DOCUMENT_CHARS` (4000), `RAG_DATE_ORDER` (`dmy`). No LLM call is added.

## Problem

Retrieval saw documents only through their chunk text (embeddings and `search_vector` were built from chunk
content). Measured on the enterprise workload suite (`evals/enterprise_workloads.json`) and in production traces:

* A month, date or site written only in a filename ("Internet-Bills-July2026.docx", "Board-Minutes-2026-09-15.docx",
  "Q3-2026-Cooling-Audit-Lahore-DC.pdf") could not be found: near-identical documents tied, the wrong twin won.
* A small document split into a header, a table and a signature block was retrieved one fragment at a time: the
  document was found, its answer-bearing table was not (15.6% of cases: "document without evidence").
* A question naming a file ("Internet-Bills-July2026.docx phr yeh file kya hai") was answered from the
  conversation's previous document instead.

## Approach

Everything extends the existing ingestion, `retrieve()`, `run_chat` and PostgreSQL schema. No new service, no
second vector index, and no lists of customer terms, filenames or acronyms.

1. **Document descriptors** (`app/rag/discovery.py`, migration 0016). At ingestion each document gets one
   normalised identity: the filename split at separators, camelCase and letter/digit boundaries; dates in the
   filename and in the first 1,500 characters written out as month names (`RAG_DATE_ORDER` decides 06.07.2026);
   the title (first line), section headings, sheet names, slide titles and table titles. Stored in
   `documents.descriptor` with a generated `tsvector` and GIN index, and a `descriptor_version`. Workers rebuild
   missing or older descriptors at startup from stored chunks (no re-parsing). The descriptor is identity, never
   evidence: it is not sent to the model and not cited.
2. **Stage 1, document resolution.** The question's words, split the same way and without stopwords, are matched
   against descriptors inside the user's ACL scope. Weights are IDF over the descriptors in scope (counted inside the
   scope, so documents the user cannot read never influence anything). Nothing is resolved unless the best document
   matches a distinctive term (in at most a quarter of the documents, or two); documents within 60% of the best are
   kept, up to `RAG_DISCOVERY_MAX_DOCUMENTS`.
3. **Stage 2, documents then chunks.** Documents are ranked by reciprocal-rank fusion of the name match and the
   chunk ranking (their best chunk's place); ties go to the name. Named documents and the two best documents of the
   chunk ranking are expanded: one of up to `RAG_SMALL_DOCUMENT_CHARS` arrives whole; a larger one with the chunks
   next to its hits, or (named, with no hit) its three best chunks with no similarity floor. Added chunks keep their
   own cosine as `score`. The context budget is applied last, in document order.
4. **Named documents first in whole-document questions.** `_overview_target` takes a document the user picked,
   then one the question names, then the conversation's focus.
5. **One configuration.** `retrieval.search_options(settings)` is what chat uses and what the evaluation gate
   measures.

Two answer-path rules ship with it:

* A cited reply that only says the sources do not state the fact (`sufficiency.states_only_absence`) is treated as
  insufficient evidence, exactly like an `INSUFFICIENT_EVIDENCE:` reply, never as a verified, cited answer. A partial
  answer that also notes a gap is kept.
* A general-knowledge reply never sees the text of earlier document-grounded answers (`Turn.grounded`), so facts of
  one document cannot resurface, uncited, after a later question found no evidence.

## ACL

Descriptor matching runs with the same `_scope` predicate as every other stage (`access.readable_by`, current
embedding model, indexed), in the same SQL; term frequencies are counted inside that scope. Expanded chunks are
loaded with the predicate again. A restricted document's name never resolves for a user who cannot read it.

## Trade-offs

* Up to 3 extra queries per question (term frequencies, candidate documents, chunk sizes) plus sibling loads.
* More context per question: a found small document arrives whole. The budget (`RAG_MAX_CONTEXT_CHARS`) still caps
  it, in document order.
* A descriptor term can match an unrelated document with a similar name; the distinctive-term rule, the 60% ratio
  and fusion with the chunk ranking limit its weight, and chunk search still runs.
* Cross-lingual names (an Urdu-script question about an English filename) are not matched: that needs a
  multilingual embedding model (a later phase).
* `RAG_DATE_ORDER` is per deployment; ISO dates and dates with a day above 12 are read unambiguously.

## Evaluation

Same machine, same provider (gpt-6-luna) for answers; retrieval figures are deterministic.

Enterprise workloads (`evals/run_enterprise_workloads.py`, 55 cases; reports `enterprise-workloads-baseline-*` and
`enterprise-workloads-phase2-final`):

"After" includes the two corrections that followed (evidence checks aligned with whole documents, and locale-aware
amounts in `app/rag/numbers.py`; see `tests/test_evidence_alignment.py`).

| Metric | Before | After |
| --- | --- | --- |
| Expected document retrieved | 82.2% | 93.3% |
| Answer-bearing passage retrieved | 66.7% | 93.3% |
| Document retrieved without its answer | 15.6% | 0% |
| History contamination on topic switches | 24% | 2.5% |
| Pass rate | 61.8% | 89.1% |
| History leaks | 3 | 0 |
| False refusals | 4 | 0 |
| Cited non-answers | 11 | 1 |
| Retrieval latency p50 / p95 | 108 / 227 ms | 136 / 413 ms |

Golden gate retrieval, production configuration (`RAG_HIERARCHICAL_RETRIEVAL=false` for "before"): recall@1
0.774 -> 0.786, recall@5 1.0 -> 0.988, nDCG@5 0.923 -> 0.917, MRR 0.905 -> 0.899 (recall@1, nDCG@5 and MRR are under
the gate's thresholds). Golden answers: correctness 0.806 -> 0.857, abstention 0.875 -> 1.0, groundedness 0.827 ->
0.902, citation accuracy 0.872 -> 0.978, false refusals 7 -> 5; every remaining failure also failed before. Open:
follow-ups whose verified evidence falls back to a general answer (not judged a document question), and Urdu-script
questions about English documents.
