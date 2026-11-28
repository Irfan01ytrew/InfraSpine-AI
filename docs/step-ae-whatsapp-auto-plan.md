# Step AE — WhatsApp auto-reply from Knowledge + leads to CRM: implementation plan

Author: Agent 38 (Step AE preparation). Base: `step-e-gaps-wip` `ca956c2`
(merged Step E: Gaps 1, 1b, 2, 4, 5 + frontend). Status: **preparation + slice 1 only**.
Step AE is not finished by this document. No real Meta credentials exist anywhere
in this work; every Meta call in tests is mocked. The owner must still supply
Meta WhatsApp Cloud API credentials before any live Cloud send works.

Owner-approved scope (2026-11-28): customer WhatsApp messages are answered
automatically from Knowledge, and new leads go straight into CRM. This step
builds on Step F (Agent 13, `docs/step-f-whatsapp-plan.md`) and does **not**
duplicate it: Step F owns the transport (webhook verification, sender
verification, Baileys ↔ Cloud failover, voice notes, approvals over WhatsApp,
24-hour window templates). Step AE owns only the **customer-facing triage**:
what an incoming customer message becomes — a Knowledge answer, a canned
reply, a captured lead, or a handoff to a human — and the CRM record for leads.

Non-negotiable rules (HANDOVER-PROMPT.md §5) apply to every slice: finish each
slice completely before the next; every component has a fallback; preserve the
repo and never break APIs (adapters, not rewrites); every lookup/dispatch
through `app/platform` (`resolve_principal`, `authorize`, `require` → 404
non-members, 403 members without permission); documents, web pages and incoming
messages are **untrusted** — message text can never grant permissions, change
payees, or change what an approval covers; approvals are bound to the exact
action (hash), expire, are single use, separation of duties; every side effect
goes through `execute_once` with read-back verification; tests only on
throwaway `*_test` databases; full suite green, Alembic round trip + check
clean, frontend tests + build + typecheck clean; never print or commit secrets.

## 1. What already exists (surveyed 2026-11-28, do NOT redo)

### 1.1 Step F transport — already built (Agent 13 plan §1)

- Meta Cloud API path: `app/integrations/whatsapp.py` (322 lines) — webhook
  `accept` (signature/HMAC, dedup on `external_message_id`, sealed text),
  `claim` (fencing, max 5 attempts), `process` (24-hour window check,
  `bound_identity` re-bind on every step, Agent Studio run with idempotency
  key `whatsapp-cloud:{inbound.id}`, reply exactly once via `send_text_once`
  → `execute_once`, duty `channel.reply`).
- Baileys (free, QR) path: `app/services/agent_studio_baileys.py` + Node
  bridge in `integrations/whatsapp-baileys/`.
- Identity: `app/platform/channels.py` — `resolve_whatsapp_sender`,
  `link_identity`, `sync_allowed_senders` (legacy allow-list mirror).
- Models: `app/db/agent_studio_whatsapp_models.py` —
  `AgentStudioWhatsAppConnection`, `AgentStudioWhatsAppContact`
  (connection + wa_id → Agent Studio thread), `AgentStudioWhatsAppInbound`
  (encrypted text, status queued/processing/completed/failed).
- The Cloud worker's `process()` already stores a per-sender `Contact`
  (wa_id → thread) on first contact. **There is no Lead, Customer, or CRM
  model anywhere in `app/`** — `grep` for `class Contact|class Customer|
  class Lead` in `app/db/models.py` and `app/integrations/models.py`
  returns nothing. Lead capture therefore needs a new model + migration
  (slice 3), not a reuse.

### 1.2 Knowledge Q&A — the answer source for auto-replies

- `app/rag/answer.py::answer_question` returns a grounded answer with an
  `insufficient_evidence` flag (`GroundedAnswer`, line 733): when the
  documents do not state the fact, the answer is the fixed
  "Insufficient document evidence" refusal, never an invented fact.
  Agent 12 (commits `0e0ca3a` + `7bf321d`) fixed the web-search router and
  the search-query builder for Roman Urdu questions.
- Knowledge answers are permission-filtered (`app/rag/access.py`,
  `documents.read` gate) — a customer must never receive text from a
  document they may not read. **Auto-replies may only use the public /
  customer-visible Knowledge subset** (slice 2 defines that subset; the
  default is: no document content at all until the owner marks a folder
  or document set as customer-visible).
- What is missing: no path connects a WhatsApp inbound to
  `answer_question` at all. Today every inbound becomes an Agent Studio
  **task** (`AgentStudioService.start_run`); a customer asking "what are
  your prices?" gets a task run, not a Knowledge answer.

### 1.3 What Step AE does NOT own

- Webhook verification, sender verification, failover, voice, approvals,
  templates → Step F slices 2–6 (Agent 13 plan). Step AE reuses them and
  must not re-implement them.
- The Agent Studio task path stays as the fallback for anything the
  decider does not answer, capture, or hand off.

## 2. What is missing (the Step AE work list)

| # | Gap | Evidence | Slice |
|---|-----|----------|-------|
| 1 | No triage of an incoming customer message: question vs lead vs handoff vs spam vs instruction | Every inbound → Agent Studio task; no classifier anywhere in `app/` | **1 (this task)** |
| 2 | No Knowledge answer path for customers; no customer-visible document subset | §1.2; no call from any WhatsApp file into `app/rag/answer.py` | 2 |
| 3 | No CRM / lead storage; no lead capture | §1.1 — no Lead/Customer model | 3 |
| 4 | No human-handoff queue or state (a handoff is just a task today) | No handoff model/flag on inbound or contact | 4 |
| 5 | No auto-reply policy surface (owner cannot turn auto-reply on/off, set hours, or edit canned replies) | No settings field for auto-reply in admin or the WhatsApp panel | 5 |
| 6 | No abuse control specific to auto-reply (a spammer can trigger unlimited Knowledge lookups) | Step F slice 2 rate limit covers intake, not per-sender Knowledge calls | 4 |
| 7 | No `CHANGES-step-AE.md`, no end-to-end proof | — | 6 |

Owner still needed (unchanged from Step F): Meta WhatsApp Cloud API
credentials for any live Cloud send; a phone with normal WhatsApp for the
Baileys path; the owner must mark which Knowledge documents are
customer-visible (slice 2) — until then auto-reply answers nothing from
documents and hands off instead.

## 3. Design rules (apply to every slice)

1. **Answers only from documents.** An auto-reply is sent only when
   `answer_question` returns evidence-backed text with at least one
   citation. Anything else → handoff to a human with the
   insufficient-evidence note. The model never invents prices, dates, or
   promises.
2. **Message text is untrusted data.** It can never grant a permission,
   change a payee, approve anything, or change what an approval covers.
   Text that looks like an instruction to the system ("ignore previous
   instructions", "grant me admin", "act as …") is ignored, never obeyed.
   The decider treats it as data, not as a command.
3. **Auto-replies never execute actions.** No payment, no booking, no
   record change, no approval. An action request from a customer →
   handoff to a human (or, for a verified internal sender, the existing
   Step F agent-task path with its approval flow).
4. **Leads are records, not side effects.** Capturing a lead writes one
   CRM row (name/phone/interest + the source message id) through
   `execute_once` with an idempotency key `whatsapp-lead:{inbound.id}`;
   a duplicate delivery never creates a second lead. No message is sent
   to the customer about the lead except the normal acknowledgement.
5. **Identity first.** The decider only runs after Step F's identity
   binding (`bound_identity`); an unmapped/unverified sender gets no
   Knowledge content and no lead capture — handoff or nothing.
6. **Language: Roman Urdu + English first.** Customers write Roman Urdu;
   the classifier and canned replies must understand it (same word lists
   Agent 12 added for routing: `kb/kab`, `qimat/rate`, `kharidna`, …).
   Urdu script support follows in Step Z.
7. **Every auto-reply is auditable.** Decision, reason, evidence
   citations, and the exact reply text are stored on the inbound record
   (sealed like other WhatsApp text) so the owner can review what the
   bot told a customer.

## 4. Slice 1 — done in this task (pure-function auto-reply decider)

Scope: **decide, don't send.** No production behaviour changes, no Meta
calls, no DB writes. The decider is pure stdlib, no I/O, no app imports.

New module: `app/whatsapp_auto/decider.py` (+ `__init__.py`):

- `classify_intent(text) -> Intent` — `question | greeting | lead |
  handoff_request | complaint | action_request | spam | unknown`.
  Roman Urdu + English keyword lists; greeting and spam checked first
  (spam can masquerade as a question); an instruction-shaped message
  ("ignore previous instructions", "you are now …", "grant me admin")
  classifies as `spam` with reason `untrusted_instruction`.
- `extract_lead_signals(text) -> LeadSignals` — phone number (PK /
  international digit patterns), email address, interest keywords
  (`price/qimat/rate`, `buy/kharidna/lena hai`, `demo`, `quote`, …),
  explicit name pattern ("mera naam X hai" / "my name is X").
- `decide(message, evidence) -> Decision` — combines intent + the
  Knowledge evidence signal (`found`, `citations`, `confidence`) + context
  (`asked_before`, `sender_verified`):
  - `reply_from_knowledge` — intent `question`, evidence found with
    ≥1 citation and confidence ≥ 0.5.
  - `reply_canned` — intent `greeting` (or out-of-hours greeting).
  - `capture_lead` — intent `lead` with at least one contact field
    (phone/email) or an explicit interest + the sender is verified;
    reply template `lead_ack`.
  - `handoff_to_human` — intent `handoff_request` / `complaint` /
    `action_request`; question with insufficient evidence; a question
    asked before with no resolution; any sensitive topic (refund,
    payment dispute).
  - `ignore` — intent `spam`, empty text, or untrusted-instruction text.
- `Decision` carries `action`, `reason`, `reply_template` (a key, never
  customer text built from the message), and `lead` (the extracted
  signals, or `None`).

New test file: `tests/test_whatsapp_auto_decider.py` (throwaway `*_test`
DB not needed — pure functions; minimal pytest-only venv, log capture on).
It proves: question + evidence → reply; question + no evidence →
handoff; Roman Urdu question/lead/handoff; greeting → canned; lead with
phone → capture_lead with extracted phone; lead without contact info →
handoff (ask for details); explicit human request → handoff; complaint
→ handoff; action request ("approve the payment") → handoff, never an
auto-executed action; injection text → ignore; spam/empty → ignore;
unverified sender → no Knowledge reply (handoff or ignore).

Acceptance for slice 1: new tests pass; existing
`tests/test_irfanix_cloud_webhook.py` and
`tests/test_agent_studio_unified.py` still pass; no production code
changed.

## 5. Slice 2 — Knowledge answer path for customers

- New intake mode in the Cloud worker (and Baileys worker): after
  identity binding, run the slice-1 decider. For `reply_from_knowledge`,
  call `answer_question` scoped to the **customer-visible Knowledge
  subset** only (new owner setting: a folder / document set flagged
  customer-visible; default = empty, i.e. no document answers until the
  owner opts in). Reply goes through the existing `send_text_once` →
  `execute_once` path (duty `channel.reply`), with citations stripped
  to plain text and the decision + citations stored (sealed) on the
  inbound.
- "Insufficient evidence" → the fixed refusal text + an offer to connect
  a human (handoff), never a guessed answer.
- Tests: mocked `answer_question` → exact reply text; no citations →
  refusal + handoff; customer-visible subset respected (a document
  outside the subset is never quoted); duplicate delivery → one reply.
- Files: `app/integrations/whatsapp.py` (adapter only),
  `app/whatsapp_auto/knowledge_reply.py`,
  `tests/test_whatsapp_auto_knowledge.py`. No migration unless the
  customer-visible flag needs a column (prefer a setting first).

## 6. Slice 3 — CRM lead model + capture

- New model `WhatsAppLead` (migration): id, owner/org scope, wa_id,
  name, phone, email, interest (free text from the message, stored as
  data), source `inbound.id`, status (`new/contacted/converted/lost`),
  idempotency key `whatsapp-lead:{inbound.id}` (unique), created/updated
  timestamps. All lookups through `app/platform` with owner scope.
- Capture runs inside the worker after the decider returns
  `capture_lead`: one row via `execute_once`, duplicate delivery →
  no second row. A simple leads list in the Agent Studio / CRM UI
  (read-only first; status changes are a later slice).
- Tests: lead created once per inbound; duplicate → one row; unverified
  sender → no lead; lead fields are stored verbatim as data (never
  executed); owner-scoped reads only.
- Files: `app/db/whatsapp_lead_models.py` + Alembic migration,
  `app/whatsapp_auto/leads.py`, `tests/test_whatsapp_auto_leads.py`
  (throwaway `*_test` DB).

## 7. Slice 4 — human handoff queue + abuse control

- Handoff state on the inbound/contact: `handoff_requested_at`,
  `handoff_reason`, `handoff_claimed_by`; a handoff list endpoint for
  staff (through `app/platform`, staff permission only); claiming a
  handoff stops further auto-replies in that thread until released.
- Abuse control: per-sender Knowledge-lookup budget (e.g. N auto-replies
  per hour); excess → canned "please wait for a human" reply, no lookup,
  audited. Spam senders (decider `ignore` × threshold) are paused from
  auto-reply for a bounded period.
- Tests: handoff blocks auto-reply; claim/release; budget breach →
  no lookup; spam pause expires.
- Files: `app/whatsapp_auto/handoff.py`, migration if columns needed,
  `tests/test_whatsapp_auto_handoff.py`.

## 8. Slice 5 — auto-reply policy surface (admin UI)

- Owner settings (admin settings + WhatsApp panel): auto-reply on/off,
  business hours (outside hours → canned out-of-hours reply + handoff
  next morning), canned-reply texts (greeting, lead acknowledgement,
  out-of-hours, insufficient-evidence), customer-visible Knowledge
  folders. Stored as org settings; every send re-reads them.
- Frontend: settings section in the Agent Studio WhatsApp panel;
  `npm test` + build + typecheck green.
- Tests: auto-reply off → no decider run (task path as today); canned
  text override is used verbatim; out-of-hours → canned reply.
- Files: settings surface in `app/api/routes/agent_studio_whatsapp.py`
  + frontend panel, `tests/test_whatsapp_auto_policy.py`.

## 9. Slice 6 — end-to-end proof + CHANGES-step-AE.md

- End-to-end test (mocked Meta API, throwaway `*_test` DB): verified
  webhook → decider → Knowledge reply once; lead message → one CRM row
  + acknowledgement; handoff request → human queue, no auto-reply;
  duplicate delivery → nothing duplicated; injection text → ignored,
  no side effect.
- Docs: `CHANGES-step-AE.md` (plain language, file list, test counts),
  `docs/irfanix-release-checklist.md` update, full backend suite green,
  Alembic round trip + check clean, frontend tests + build + typecheck,
  merge to `main` only when green. Live push is a separate
  owner-approved step (backup first, never downgrade on live).

## 10. Test list for the whole step (acceptance)

- Slice 1: `tests/test_whatsapp_auto_decider.py` (§4).
- Slice 2: Knowledge reply incl. subset + refusal + dedup.
- Slice 3: lead capture incl. dedup + owner scoping.
- Slice 4: handoff queue + claim/release + lookup budget + spam pause.
- Slice 5: policy settings incl. off-switch + hours + canned text.
- Slice 6: end-to-end matrix + `CHANGES-step-AE.md`.
- Regression gates every slice: `test_irfanix_cloud_webhook.py`,
  `test_webhooks.py`, `test_irfanix_whatsapp_receipts.py`,
  `test_agent_studio_unified.py`, `test_whatsapp_unified_intake.py`
  (Step F slice 1), frontend `npm test` + build + typecheck.
- Manual (owner): mark one Knowledge folder customer-visible, message
  the business number with a price question (Roman Urdu), confirm the
  answer comes only from that folder; send a lead message, confirm one
  CRM row; ask for a human, confirm the handoff list shows it.
