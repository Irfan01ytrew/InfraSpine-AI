# Conversation context and safe fallback

Status: implemented; active whenever a conversation's saved answers carry source state. No setting, no LLM call.

## Problem

Measured on the enterprise workload suite and in the 19 October incident:

* A follow-up borrowed the previous question's words whenever it named no corpus word, so "july ka kitna bill tha"
  after a screenshot question was searched as "what is in this screenshot july ka kitna bill tha".
* The conversation's focus was "whatever the last citing answer cited": after a refused turn it still pointed at the
  screenshot, several turns back.
* A document question whose evidence was not verified fell back to a general-knowledge answer, which restated the
  earlier screenshot answer uncited.

## Approach

1. **Per-turn source state.** Every saved answer stores the documents its question named (`named_document_ids`,
   resolved by document discovery) next to `source_document_ids` and its citations. On reload
   (`ConversationService.context`) each `Turn` carries its `documents`: what it cited, else what it named (re-checked
   for readability). The focus is the latest turn with documents, so a refused "july ka kitna bill tha" that named the
   July bill moves the focus there.
2. **Follow-up resolution through that state** (`run_chat`). Two separate decisions:
   * Which documents: those the question names come first; otherwise those of the turn it refers to (the latest turn
     with documents). A name several documents share ("action items" heads two sets of minutes) is settled by the
     conversation when the question points back ("this file"); a name that is not the conversation's still wins.
     "This file" resolves through the state even when the question has a topic of its own. The chosen documents are
     searched alone, with no similarity floor, and the best of them expand as named documents do (whole when small).
   * Which words: the question's own. Only a follow-up with no topic of its own ("aur?", "net ka bill yr") borrows a
     question, only the referenced turn's, never a chain of earlier ones, and only when it is about that turn's
     documents ("aur August ka?" after the July bill names August and borrows nothing).
   * An independent question (one that names two or more corpus terms) borrows nothing and searches the library.
   * Nothing resolves (no named or tracked documents): the question alone is searched, nothing is borrowed.
   * A history without source state (stateless `/api/rag/chat` clients, answers saved before tracking) behaves as
     before.
3. **Explicit priority** for whole-document, chart and page-image questions: a picked document, then one the question
   names, then the conversation's, then the closest.
4. **Safe fallback.** A question that names documents, or a follow-up resolved to the conversation's documents, is a
   document question (`answer_question(document_hint=True)`): unverified evidence is refused, never replaced by a
   general answer. General answers remain for questions no document is involved in, and never see the text of
   document-grounded turns (`Turn.grounded`).

## ACL

Named and tracked documents are only hints: `searchable_documents` re-authorises them on every question, and
retrieval applies `access.readable_by` again. A document a user can no longer read drops out of the scope; nothing is
borrowed from a withheld answer (withheld answers never reach the history).

## Trade-offs

* One more descriptor lookup per question (the named documents), also used to store the turn's state.
* A follow-up scoped to named or tracked documents does not see other documents; a question that names its own topic
  is not scoped.
* Borrowing only the referenced turn means a long chain ("july?" -> "net?" -> "aur docs?") keeps the documents of the
  chain, not every earlier word.
