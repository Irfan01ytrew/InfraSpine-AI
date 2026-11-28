# Step AN — Universal Personal AI Companion OS: plan

Owner request (2026-11-28 ~00:39 PKT): a personal AI platform that understands
one user's goals, projects and preferences, plans tasks, coordinates different
AI tools, remembers progress, and advances work through authorized integrations.
First job: check overlap with the existing codebase, then propose unique value,
MVP, architecture, privacy/security controls and a phased roadmap.

Status: plan + slice 1 (pure-function goal/task planner) by Agent 54.
Base: `step-e-gaps-wip` `ca956c2`. Branch: `agent/personal-companion-os`.

## 0. The one-line distinction

InfraSpine is the **company's brain**: org-scoped, role-based, approvals over
company money, documents and workflows. The Companion OS is **one person's
brain**: user-scoped, goal-based, remembering what *this* user wants, what they
already did, and what is next — across tools. Same platform rules (platform
auth, approvals, audit), opposite scope. Nothing company-scoped becomes
personal data, and nothing personal leaks into a company scope.

## 1. Overlap check (checked against base `ca956c2` + sibling prep branches)

| Area | What already exists | Reuse | Genuinely NEW for Companion OS |
|---|---|---|---|
| Personal tasks | `app/db/models.py: PersonalTask` + `app/api/routes/personal_tasks.py` (`/api/personal-tasks`, CRUD/complete/cancel/reminders, per-user) | Task storage, completion, reminders | No goals/projects above tasks; no planning, priorities, deadlines across projects; no progress memory |
| Personal memory | `app/db/models.py: MemoryItem` + `app/api/routes/memory.py` (`/api/memory`, per-user, others get 404, supersede, max items) + `app/services/memory.py` | Per-user memory store + isolation pattern | Memory items are free-form notes; nothing links memory to a goal/project/task, nothing tracks progress over time, nothing recalls "where did I leave off" |
| Knowledge RAG | `app/rag/` (answer, chat, retrieval, voice_questions, permission-filtered) | Answering from the user's readable documents; voice questions | RAG answers company documents; Companion needs a *personal* layer: goals, preferences, project notes the user wrote — not org documents |
| Voice-first (O) | `app/qa/founder_voice.py` (Agent 17): parse voice note → proposed command, `requires_approval=True` always, rejects bypass language | Voice → structured, approval-gated command pattern | Founder voice commands are company workflow actions; Companion needs personal commands ("remind me", "plan my week", "what's next on X") |
| Live-data (P) | Mocked search provider + citations/freshness (Agent 20) | Freshness + "insufficient evidence" behaviour | Nothing personal: no per-user watchlists, no "tell me when X changes for my project" |
| Control room (Q) | `app/control_room/service.py`: read-only overview per scope (Agent 22) | Status-overview shape | Q shows agents/company status; Companion shows *one user's* goals, progress and next actions |
| Digital twin (S) | `app/digital_twin/impact.py` (Agent 24, DONE): `simulate_absence` pure function, company dependency impact | Impact-simulation pattern (pure functions, no I/O) | S models the company; Companion models one person's workload, capacity and commitments |
| Model router (T) | Pure-function router: task class → model from config, fallback + cost estimate (Agent 25) | Route Companion sub-tasks (plan vs summarise vs draft) to the cheapest capable model | No personal routing: no per-user model preference, no per-user spend cap |
| Integrations (V) | `app/integrations/` (catalog, adapters, oauth, security, service, WhatsApp) + Step V generic registry + mocked Slack (Agent 26) | Connector registry, OAuth, per-connector security | No *personal* consent per integration, no per-user token scoping, no "which tools may act for me" |
| Multilingual (Z) | Language detection/normalisation helper, Roman Urdu ↔ Urdu ↔ English (Agent 32) | Understand goals/tasks written in Roman Urdu | Nothing goal-aware: dates like "kal" / "pichla mahina" are parsed for reports (Step X), not for personal deadlines |
| Scheduled reports (AA) | `app/reports/scheduled.py` (Agent 33): `ReportSchedule`, `next_due`, `should_send`, quiet hours, daily cash summary | Scheduling + quiet-hours + no-stale-send rules | AA reports company finance on a schedule; Companion needs a *personal* daily briefing ("your day, your open goals, what's overdue") |
| Resilience (AH) | Dependency map + risk scoring, company-level (Agent 43) | Risk/impact vocabulary | AH protects the company; Companion protects one person's commitments (missed deadline, overloaded week) |
| Self-healing (W) | `app/self_healing/detector.py` (Agent 29): allow-list remedies, escalation | Escalation + cooldown pattern | Nothing personal |
| Finance / HR | Invoice flow, leave/attendance (Agents 36/40) | Domain modules the Companion can *coordinate* (e.g. "your leave request is pending") | Companion never owns finance/HR data; it only surfaces status the user may already read |
| Agent Studio (AD) | Workflow templates + drag-drop canvas (Agent 37) | A Companion task plan can become a Studio draft (never auto-published) | Studio builds company workflows; Companion builds personal plans |
| Software Foundry (AM) | Step AM (Agent 53, in progress): builds working apps from a need, preview → approval → deploy | **Tool coordination target**: "build me a small app for X" is a Companion task routed to the Foundry — Foundry builds software, Companion manages the person asking | Foundry has no memory of the user's goals or follow-up; Companion supplies the brief and tracks the outcome |
| Platform core | `app/platform/`: `resolve_principal`, `authorize`/`require`, approvals (`ActionSpec`, `request_approval`, `decide_approval`, `consume_approval` — exact-action hash, expiring, single-use, separation of duties), `execute_once` | **Everything**: identity, permissions, approvals, audit | Companion adds a *per-user* scope: a user may only ever see/act on their own goals, tasks, preferences and memories |

**Overlap verdict:** roughly 60% of the plumbing already exists (platform,
personal tasks, personal memory, integrations, router, scheduler). What does
**not** exist anywhere: a goal/project model, a planner that turns goals +
preferences + progress into an ordered plan, progress memory tied to goals,
a tool coordinator, per-integration personal consent, or a personal briefing.
Companion OS is a new product layer on top of existing platform pieces — not
a rewrite, and not a second copy of InfraSpine.

## 2. Unique value proposition

- **One place for "what am I doing and what's next":** goals → projects →
  tasks → progress, remembered across sessions and tools, in the user's own
  language (English / Roman Urdu / Urdu script).
- **It coordinates, it doesn't replace:** Knowledge answers, Finance tracks
  money, Studio builds workflows, Foundry builds apps, WhatsApp talks to
  customers — the Companion decides *which* tool a task needs and hands it
  over, with the user's approval where anything leaves the system.
- **It remembers honestly:** progress is only what the user (or an
  authorized tool, with evidence) reported. It never invents completion.
- **It stays personal:** per-user isolation by construction; company data
  stays company data. The Companion is the only surface that is *yours*.

## 3. MVP scope (Phase 1)

1. Goals + projects + preferences per user (create/edit/archive; one active
   goal list; preferences: language, working hours, quiet hours, weekly
   capacity).
2. Task planning: goal + preferences + recorded progress → ordered task plan
   with priority and due date (slice 1, this agent).
3. Progress memory: record/update progress per goal/task with evidence note;
   "where did I leave off" summary; never auto-complete.
4. ONE authorized integration, approval-gated: in-app + email digest of the
   daily plan (no external send without explicit consent + approval).
5. Personal daily briefing page: today's tasks, overdue items, next action
   per goal. Read-only over data the user may already read.

Explicitly OUT of MVP: auto-sending messages, auto-booking, payments,
multi-tool execution, proactive suggestions, cross-user sharing.

## 4. Architecture

```
User (web / mobile / voice)
  → Companion API (app/companion/, per-user routes, get_current_user)
  → app/platform (resolve_principal, authorize/require, approvals, execute_once)
  → Companion core (pure functions first):
      planner.py      goals + preferences + progress → ordered plan   [slice 1]
      memory.py       goal-linked progress records (extends MemoryItem)
      coordinator.py  task → tool route (Knowledge/Finance/Studio/Foundry/V)
      briefing.py     daily briefing builder (reuses AA scheduling rules)
      consent.py      per-integration consent + retention records
  → Existing modules (read-only unless approval consumed):
      PersonalTask / MemoryItem / RAG / integrations / model router (T)
  → Persistence (slice 2): companion_goals, companion_projects,
      companion_preferences, companion_progress (+ Alembic migration)
```

Rules: every route resolves the principal and checks `companion.*`
permissions; every external action is an `ActionSpec` with an exact hash,
expiry and single use, consumed through `execute_once`; the planner and
briefing builders stay pure (no I/O) so they are testable without a DB.

## 5. Privacy / security controls

- **Per-user isolation:** every query is filtered by `user_id`; another
  user's goals/tasks/preferences/memories return 404, never 403-with-existence
  (same pattern as `/api/memory`). No cross-user leakage in planner, briefing
  or coordinator — a plan input from another user is rejected, not merged.
- **Consent per integration:** each integration needs a separate, revocable
  consent record (tool, scope, granted_at, expires_at). No consent → no call,
  no data sent. Revoking consent stops future calls and deletes stored tokens.
- **Approval before any external action:** sending, booking, paying, sharing
  or changing anything outside the Companion always needs an exact-action
  approval (platform approvals: hash-bound, expiring, single-use, separation
  of duties). The planner only *flags* `needs_approval`; it never executes.
- **Untrusted text:** goal/task/preference text, document text and tool
  outputs are data, never instructions — they cannot grant permissions, change
  a payee, or change what an approval covers.
- **Audit record:** every plan generated, progress recorded, consent granted/
  revoked and approval requested/consumed is audit-logged with user, time and
  action hash.
- **Retention / deletion:** progress and briefing history kept per a per-user
  retention setting (default 90 days, owner decision pending); "delete my
  Companion data" erases goals, projects, preferences, progress and consents
  for that user only; exports are per-user.
- **Model routing:** personal text goes only to the configured router (T);
  never logged with secrets; no personal data in training or telemetry.

## 6. Phased roadmap

- **Phase 1 — MVP (slices 1–2):** planner (slice 1, done here) + persistence
  and per-user API + daily briefing (slice 2). One integration (email digest).
- **Phase 2 — Multi-tool coordination (slices 3–4):** coordinator routes
  tasks to Knowledge / Finance / Studio / Foundry / WhatsApp with approval
  gates; per-integration consent records; per-user model routing + spend cap.
- **Phase 3 — Proactive Companion (slice 5):** suggestions ("your week is
  overloaded", "goal X has stalled 14 days"), quiet-hours-aware nudges via
  AA scheduling rules, weekly review; every suggestion is advisory and
  dismissible — the Companion never acts on a suggestion by itself.

## 7. Slices (in plan order)

1. **Planner (pure functions)** — `app/companion/planner.py` + tests.
   Goals + preferences + progress → ordered plan, priorities, overdue
   detection, progress recording, `needs_approval` flag. DONE (Agent 54).
2. **Persistence + per-user API + briefing** — models + Alembic migration
   (`companion_goals`, `companion_projects`, `companion_preferences`,
   `companion_progress`), `GET/POST /api/companion/*` through `app/platform`,
   daily briefing builder. Needs: owner decision on retention default.
3. **Consent + ONE integration (email digest)** — consent records, digest
   send via approval-gated outbox, revoke stops sends.
4. **Tool coordinator** — route a planned task to Knowledge/Finance/Studio/
   Foundry/WhatsApp; approval gate per route; per-user router + spend cap.
5. **Proactive suggestions + weekly review** — stall/overload detection,
   quiet-hours nudges, weekly review summary; advisory only.

## 8. Owner decisions needed

1. **First integration:** email digest (proposed) vs WhatsApp daily briefing?
2. **Retention default:** 90 days proposed — confirm or change.
3. **Weekly capacity:** should the planner warn when planned hours exceed a
   per-user weekly capacity (default 40h proposed)?
4. **Voice commands:** should Companion accept the same voice-note intake as
   Step O, or web/mobile text first?
