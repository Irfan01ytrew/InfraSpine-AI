# Evidence validation

`app/rag/sufficiency.py` decides whether a cited answer is supported by the passages it cites. Values (numbers,
codes, amounts, dates) must come from the cited text; topic words decide whether a passage is about the question;
the LLM verifier is called only when the rules cannot decide (`inconclusive`). This page records the alignment work
after document discovery delivered whole documents (`docs/document-discovery.md`) and conversations were resolved
through source state (`docs/conversation-context.md`).

## Identity is not evidence

The cited document's filename, section and sheet tell what a passage is about ("the July bill" relates to the July
memo's table; "And DataServe?" to DataServe's termination clause). They count for topic relatedness only. They never
supply a value: an answer giving the August total for a July question is unsupported even though the filename says
"July".

## Numbers read as people write them

* Grouping: Western (1,234,567) and South Asian (12,34,567); malformed groupings ("1,2,3") are not numbers
  (`app/rag/numbers.py`, shared with table calculations: "Rs. 18,450/-", "PKR 1,25,000", "(1,200)", "12%").
* A number glued to a unit or code is that number ("200mbps", "Q2", "July2026"), in answers as in sources.
* Dates are day, month and year ("06.07.2026" is stated as "6 July 2026").
* Number words: cardinals in English, Roman Urdu and Urdu; compounds ("twenty-one", "twenty-first" = 21); ordinals
  ("second", "doosri", "دوسری" = 2) in cited text only, because an answer's "First, ..." is not a number to check.
* A number the answer computes from two different cited numbers ("60 days longer" from 90 and 30) is not accepted
  outright: it goes to the verifier, which sees the passages.

## What goes to the verifier instead of being refused

* An answer whose question words are a synonym or an exclusion ("allowance" for "working days"; "not the withdrawn
  draft") when the passage shares at least half of the question's words.
* A translation or re-wording follow-up ("urdu mein batao"): its values come from the source or it is in another
  script.
* A derived number (above).

## What is still refused

* A number, code or amount the cited text does not state (identity never fills it).
* A code or an amount from a passage about something else (an asset register's code given as "the acquisition target
  code"): the asked attribute must relate to the cited passage.
* A passage that shares little with the question.
* A cited reply that only says the sources do not state the fact: insufficient evidence, not a verified answer.

## Follow-ups

The checks read the question as retrieval searched it: spelling corrected and, for a follow-up with no topic of its
own ("aur docs ma deko"), together with the turn it was resolved to.

## Credentials

Passwords, door and access codes, PINs, keys and tokens are replaced with "[credential withheld]" before any text
reaches the model, and the prompts tell the model never to share one even if a source shows it. The prompts also tell
it not to write that placeholder for a business value a source shows (a document, ticket, rate or reference code): the
model had withheld an IT memo's domain and drive codes that were never credentials.

## Evaluation

`tests/test_evidence_alignment.py` holds the regression cases, each from a reply the active model gave. Answer suites
vary between runs: `evals/summarize_runs.py` reports the median and range across repeated runs and how often each case
failed.

Phase 4, 3 runs each, gpt-6-luna (median, range); Phase 3 reference from 2 runs:

| Suite | Metric | Phase 3 | Phase 4 |
| --- | --- | --- | --- |
| Golden | Answer correctness (min 0.90) | 0.833 | 0.943 (0.917-0.943) |
| Golden | Groundedness (min 0.90) | 0.885 | 0.941 (0.941-0.942) |
| Golden | False refusals (max 3) | 6 | 2 (2-3); door-code cases withheld by policy |
| Golden | Unsupported claim rate / permission leaks | 0 / 0 | 0 / 0 |
| Enterprise | Pass rate | 0.918 | 0.927 (0.909-0.945) |
| Enterprise | False refusals | 1.5 | 0 (0-1) |
| Enterprise | Unsupported claims / ACL leaks / history leaks | 0.5 / 0 / 0 | 0 / 0 / 0 |

Open: a cited reply that says the fact is absent in words the rules cannot tell from a stated fact ("the register does
not list X" vs "the policy does not include contractors") is left to the verifier, which accepted it in 2 of 3 runs
of one golden case.
