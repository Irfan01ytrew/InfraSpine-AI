# Step M — Decision / Organizational Memory: plan

## 1. What it is

Step M is the company's memory of its own decisions: **what** was decided,
**why** (rationale), **by whom**, **when**, **on what evidence**, **what
alternatives were considered**, **what it supersedes**, and **what depends
on it**. It answers, months later, "why did we decide this?" — with the
receipts attached — instead of letting the reason live in someone's head,
a chat thread, or a forgotten meeting.

It is deliberately **not** a log of everything that happened (that is the
audit trail) and **not** a store of documents (that is Knowledge). It is a
small, curated, queryable record of decisions, each one pointing at the
evidence that justified it.

## 2. Overlap check (read 2026-11-28, base `ca956c2` + unmerged branches)

### Already exists — reuse, do not rebuild

- **Personal memory** — `MemoryItem` (`app/db/models.py:1411`),
  `app/services/memory.py`, `/api/memory`. Owner-only, kinds
  `preference` / `terminology` / `fact`, content ≤ 500 chars, edits create
  a new version that supersedes the old one, never embedded in the
  knowledge index, never cited, never used as evidence. It is the
  **pattern** for append-only supersession — and the **boundary**: it is
  personal, one user, no org scope, no evidence, no dependencies.
- **Approval decisions** — `app/governance/models.py`: `ActionRequest`
  (approve/reject with `action_hash`, risk, required approvals) and
  `Decision` (approve/reject + `reason` ≤ 1000 chars, actor, timestamp),
  plus claim/conflict decisions with `rationale` in
  `app/api/routes/governance.py`. These record a verdict on **one
  specific action**. They have no "why we chose X over Y", no evidence
  links, no supersession, no dependencies, no review date, no org-wide
  query. Step M **links to** these (a governance decision can be the
  execution of a decision record), never replaces them.
- **Platform approvals** — `app/platform/approvals.py` (`ActionSpec`
  canonical hash, request/decide/consume, separation of duties). Step M
  reuses this for every privileged step (recording a supersession,
  revoking a decision). Step M never invents its own approval flow.
- **Audit trail** — `app/services/audit.py` `record()`: append-only event
  rows. An audit row says *something happened*; a decision record says
  *why we chose it*. Step M writes one audit row per lifecycle event and
  never reads audit rows as evidence.
- **Continuity journal** — `app/continuity/journal.py` (hash-chained
  event journal, `record`/`verify`) and `GrantRequest`. Tamper-evident
  event log for continuity; decisions taken during a drill are exactly
  the kind of thing Step M records, with the drill run as evidence.
- **Knowledge documents** — documents, chunks, freshness, conflict
  candidates. A decision may **cite** a document (by id); it never
  copies document text into itself, so a reader who may not read the
  document never learns its contents from the decision.

### Exists on unmerged branches — integrate with, do not duplicate

- **Step AN Companion OS** (`agent/personal-companion-os`,
  `app/companion/planner.py`): personal goals/tasks, append-only
  progress records, `where_did_i_leave_off()`. Personal scope, one
  user. Step M is the **org** counterpart: same append-only discipline,
  different subject (the company, not the person).
- **Step AL What-if** (`agent/whatif-simulations`,
  `app/whatif/simulator.py`): simulations of what *could* happen
  (7 scenario kinds, ranked recovery). A simulation result is **evidence
  for** a decision, never a decision itself.
- **Step S Digital Twin** (`agent/digital-twin`, `app/digital_twin/`):
  live snapshot of teams/money/flow + what-if engine. A twin snapshot
  is **evidence for** a decision (e.g. "we approved the hire because
  the twin showed Dept A at risk").
- **Step AH Resilience** (`agent/resilience-ai`, `app/resilience/`):
  risk scoring, watch rules, recovery plans as workflow runs. A recovery
  plan is an **action taken because of** a decision; the decision record
  points at it.
- **Step AB Knowledge** (`agent/knowledge-improvements`): document
  tags/summary/expiry, Roman Urdu answers. About documents, not
  decisions.

### Genuinely new — what Step M adds

1. **A decision as a first-class, org-scoped record**: title, rationale,
   decider, time, kind, status, evidence refs, alternatives considered,
   supersedes, depends-on, review date. Nothing in the codebase has
   this shape.
2. **Append-only supersession at org scope**: a decision is never edited
   or deleted; it is superseded by a new decision, and history stays
   readable. (MemoryItem proves the pattern works at personal scope.)
3. **A dependency graph with validation**: no cycles, no depending on a
   superseded/revoked decision, one superseder per decision, all refs
   resolving inside the same scope.
4. **Evidence visibility at read time**: a decision citing a restricted
   document, a governance proposal, or a simulation never leaks that
   evidence to a reader who may not read it — unreadable evidence is
   dropped from the view (and counted, so the UI can say "2 sources
   hidden").
5. **Queryable memory**: "why did we decide X?", "what depends on Y?",
   "which decisions are stale / need review?", "what did we decide
   about supplier Z last quarter?"
6. **A content fingerprint**: a deterministic SHA-256 over the canonical
   record, so a decision can be referenced exactly (like `ActionSpec`
   hashes) and a later reader can prove the record they see is the one
   that was decided.

## 3. Safety / security rules

- **Decisions are records, never permissions.** Recording, citing, or
  superseding a decision never grants access, never changes an
  approval, never moves money, never changes a payee. Any privileged
  step (supersede, revoke, record a sensitive-kind decision) goes
  through `app/platform` approvals bound to the exact action hash,
  expiring, single-use, with separation of duties.
- **Reads go through `app/platform` scopes**: 404 for non-members, 403
  for members without the permission. A decision record is visible only
  inside its scope.
- **Evidence never leaks.** Evidence is stored as references (kind +
  id + label), never as copied text. At read time, each reference is
  checked against what the reader may read; unreadable evidence is
  dropped. Rationale text is the decider's own words, not quoted
  document content.
- **Rationale is untrusted text.** It may contain instruction-like
  language ("ignore previous instructions", "approve all invoices").
  That language is **flagged** (`needs_review`) and never executed,
  never interpreted as an instruction, never allowed to change what an
  approval covers. The record stays inert data.
- **History is append-only.** No update-in-place, no delete of a
  decision. Supersede and revoke are new records/events, and the old
  record stays readable with its status.
- **Every lifecycle event writes one audit row** (`decisions.recorded`,
  `decisions.superseded`, `decisions.revoked`, `decisions.reviewed`).

## 4. Slices (in order; each is code + tests + docs, small commits)

1. **Pure record model + validation (this task, Agent 120).**
   `app/decision_memory/records.py` (stdlib only): `DecisionRecord`
   dataclass, `validate_decision`, `validate_graph` (cycle + dangling
   ref + status rules), `supersede_record`, `decision_fingerprint`,
   `visible_evidence`, `flag_suspicious`. Tests:
   `tests/unit/test_decision_memory_records.py` (pure, `--noconftest`,
   no DB), including hostile-input cases.
2. **Persistence.** `DecisionRecord` model + migration (one new table
   `decision_records` + append-only `decision_events`; evidence as
   JSONB refs, never copied text), service with org-scoped
   record/supersede/revoke through `app/platform` (`require()` gates,
   approval for supersede/revoke of sensitive kinds), one audit row per
   event. Tests on throwaway `*_test` DBs.
3. **API.** `GET/POST /api/decisions/scopes/{scope_id}/…` through
   `app/platform`: record, list (filter by kind/status/decider/date),
   get one (evidence filtered per reader), "why" query (decisions
   mentioning a subject), dependency view (what depends on this, what
   this depends on). 404 non-member, 403 without permission.
4. **Capture paths.** Link the existing decision events as evidence /
   execution: a governance approval decision can execute a decision
   record; a continuity drill can record the decisions taken during the
   run (drill run id as evidence); a what-if simulation result can be
   cited as evidence. Recording from a proposal is approval-gated and
   never automatic.
5. **Review lifecycle.** `review_at` dates, stale/expired surfacing,
   review action (confirm / supersede / revoke), digest of decisions
   due for review via Step AA scheduled reports. Separation of duties:
   the person who records a supersession cannot be its sole approver.
6. **UI + docs + gates.** Decision timeline + dependency view + "why"
   panel in the relevant workspaces; `assessment/CHANGES-step-M.md`;
   full-suite gate; merge readiness.

## 5. Status

- Slice 1 (Agent 120): done (`58d1c09` plan, `694e044` slice 1).
  `app/decision_memory/records.py` (stdlib only): `DecisionRecord` +
  `EvidenceRef`, `validate_decision`, `validate_graph` (cycle + dangling
  ref + cross-scope + status rules), `supersede_record` (returns a new
  record; input untouched), `decision_fingerprint` (deterministic
  SHA-256 over the canonical record, list-order independent),
  `visible_evidence` (deny by default, hidden count only),
  `flag_suspicious` (advisory only — instruction-like rationale text is
  flagged, never executed). **84 new tests passed, 0 failed**
  (`tests/unit/test_decision_memory_records.py`, `--noconftest`, run
  twice: 8.41 s then 1.29 s). One test bug found and fixed during the
  run (parametrize cases omitted a required keyword argument; product
  code unchanged). No migration, no route, no production code changed.
- Slice 2 (Agent 125): done (`29653d8` persistence, `a76d949` tests).
  `app/db/decision_models.py` (`decision_records` + append-only
  `decision_events`, evidence as JSONB refs only, check constraints for
  kind/status/array shapes/superseded-details) + migration
  `0106_decision_records` (down_revision `0103`; 0104 claimed 4× and
  0105 3× on sibling branches, so 0106 is the next free number —
  renumber at merge if the merge order lands them first) +
  `app/decision_memory/service.py` (org-scoped `record_decision` /
  `supersede_decision` / `revoke_decision` / `get_decision` /
  `list_decisions` / `verify_fingerprint` through `app/platform`
  `require()`; consumed approval for sensitive kinds — finance,
  supplier, people — bound to the exact action, single-use, separation
  of duties; supersede/revoke refuse when active dependents exist;
  one `decisions.*` audit row + one event row per lifecycle event;
  no delete anywhere; `review_at` defaults to decided + 180 days).
  Permissions `decision.record` / `decision.read` /
  `decision.approve` registered in `app/core/permission_catalog.py`.
  **16 new tests passed, 0 failed**
  (`tests/test_decision_memory_service.py`, throwaway
  `infraspine_decision_test` DB, 1003 s under heavy VM load) and
  **84 slice-1 tests re-passed, 0 failed**. Alembic roundtrip on the
  throwaway DB: upgrade head → downgrade base → upgrade head, all
  clean, `alembic check` clean, head `0106_decision_records`. Two real
  bugs found and fixed during the run: the supersede flush emitted the
  old row's UPDATE before the new row's INSERT (FK violation — now the
  new row is flushed first) and one test expectation (1-char rationale
  is valid per slice 1's 1–4000 rule). Environment notes: the VM
  restart wiped the apt Postgres and the `agent125` user mid-run;
  Postgres 16.15 was reinstalled via apt, the cluster rebuilt on port
  55520, binaries stashed at `~/pg16-dist`; the per-test TRUNCATE
  stalled on `DataFileImmediateSync` under load, fixed with
  `fsync=off` + `synchronous_commit=off` on the throwaway cluster
  only. No push (Master Agent pushes).
- Slice 3 (Agent 140): done. `app/api/routes/decisions.py`
  (`GET/POST /api/decisions/scopes/{scope_id}/…` through `app/platform`:
  record, list with kind/status/decider/date filters, get one, "why"
  query, dependency view, fingerprint verify, supersede, revoke, plus
  `POST …/approvals` exposing the approval-request flow) +
  `app/decision_memory/readable.py` (per-kind readability: documents
  via RAG `readable_by`, governance proposals/decisions via the
  `module_gate` legacy+platform membership check, continuity drills via
  `scope_for`, audit events admin-only, external refs visible to
  `decision.read` holders; simulation / twin_snapshot / recovery_plan
  always hidden — their modules live on unmerged sibling branches, so
  readability cannot be verified here; slice 4 wires them when the
  owning step merges) + service additions (`decision_view`,
  `search_decisions` with LIKE-escaped subject, `decision_dependencies`,
  `limit` on `list_decisions`). Roles `decisions.admin` /
  `decisions.recorder` / `decisions.reader` seeded at API startup
  (lifespan, same pattern as Step AA). No migration, no route touches
  live data; one audit row per lifecycle event (the route adds none —
  the service already writes it). **Tests: API suite
  `tests/test_decisions_api.py` (7 tests: record/get/list/filters/why/
  verify, authz 404/403/401, validation, all-kinds evidence filtering,
  LIKE escaping, supersede/revoke/dependencies, sensitive-approval
  flow) — counts in the final report.** Deviations: (1) write responses
  echo the record as submitted (the writer already knows what they
  cited); per-reader filtering applies on GET. (2) `governance_decision`
  refs are request ids (a governance decision is keyed by
  `(tenant_id, request_id, actor_id)`). (3) `external` refs are readable
  by `decision.read` holders — a deliberately cited public reference.
  (4) The supersede/revoke audit rows come from the service only; the
  route writes one audit row only for approval requests
  (`decisions.approval_requested`).
- Slice 4 (Agent 154): done. `app/decision_memory/capture.py`
  (`record_from_governance` / `record_from_drill` /
  `list_decisions_for_drill` / `record_from_simulation`, all
  delegating to slice-2 `record_decision` so scope, consumed approval
  for sensitive kinds, graph validation, one audit row + one event row
  per lifecycle event, and append-only history all apply unchanged;
  evidence is references only, never copied text) +
  `app/api/routes/decisions.py` (`POST …/capture/governance`,
  `…/capture/drill`, `…/capture/simulation`, 201, echo as submitted;
  `GET …/drills/{run_id}/decisions`, evidence filtered per reader) +
  `app/decision_memory/readable.py` docstring (simulation readability
  wiring is now the citation path; the check itself when AL merges).
  Governance capture: request must exist, status `ready`/`executed`,
  at least one `approve` decision row (auto-ready with zero required
  approvals has no decision to capture → refused); actor must be a
  governance-tenant member (404 otherwise); cites
  `governance_proposal` + `governance_decision` (both keyed by request
  id); `decided_at` defaults to the latest approval decision. Drill
  capture: run must exist, `mode == "drill"`, status in
  `active`/`suspended`/`closed`; actor must pass continuity `scope_for`
  on the run's tenant (404 otherwise); cites `continuity_drill`;
  `decided_at` prefers `activated_at`, else `created_at`. Simulation
  capture: id stored exactly as cited (1–512 chars, label ≤200);
  stays hidden from readers (`UNWIRED_KINDS`) until Step AL merges.
  `decision.record` on the scope is required first in every capture
  path, before the source system is touched (a caller without the
  permission gets 403, never a 404 about the source). Nothing is
  automatic — no governance decide/execute or drill flow calls
  capture. **Tests: service `tests/test_decision_memory_capture.py`
  7 passed (176.78 s), API `tests/test_decisions_capture_api.py`
  4 passed (162.49 s); slice-2/3 regressions 23 passed (7 API + 16
  service, 496.23 s); slice-1 pure 86 re-passed, 0 failed
  (`--noconftest`).** Alembic check clean, head
  `0106_decision_records`, no migration. Test bugs found and fixed
  during the run: the fixture used one governance tenant per request
  — `security_members` has a unique `user_id`, one user belongs to
  one governance tenant; drill `decided_at` took the max of
  activated/created, which lied when `activated_at` was backdated —
  now prefers `activated_at`; drill listing order assumed distinct
  `created_at` inside one transaction — Postgres `now()` is the
  transaction start, so the test asserts the set plus a
  later-`decided_at` row first; drill listing needs continuity
  membership as well as `decision.read` — the reader gets 404, same
  rule as slice 3's evidence filtering. Environment notes: the VM
  rebooted ~12:14 PKT (07:14 UTC) mid-run, wiping the `decision125`
  OS user and the apt Postgres share dir; the throwaway cluster on
  port 55520 was restarted with the ai100 dist binaries
  (`/home/hatch/tmp-pg-dist-ai100/usr/lib/postgresql/16/bin/pg_ctl`,
  same 16.15), the `decision125` user recreated (uid 1003, added to
  the `postgres` group), socket dir pointed at
  `/home/hatch/tmp-pg-decision/tmp` via `-k` (`/var/run/postgresql`
  stayed permission-denied even in the group). No push (Master Agent
  pushes).
- Slice 5 (Agent 169): done. `app/decision_memory/review.py`
  (`review_decision` / `list_due_for_review` / `review_digest`):
  outcomes `confirm` (stays active, review date moves to today +
  180 days by default or an explicit future date), `expire` (status
  → `expired`), `supersede` and `revoke` (delegate to the slice-2
  service functions, so the new row / approval / separation-of-duties
  rules apply unchanged). Every outcome needs `decision.record`
  (checked first, 403 before any 404) and a 1–500 char reason; only
  active decisions are reviewable; expiring a sensitive kind
  (finance/supplier/people) needs a consumed platform approval bound
  to the new exact action `decision.expire`. Every review writes one
  `decisions.reviewed` audit row + one append-only event row
  (supersede/revoke reviews write the supersession/revocation rows
  instead — the review itself is the supersession). `review_digest`
  is a frozen dataclass (overdue / due-soon / total-active /
  needs-review counts + due rows) built for Step AA scheduled-report
  consumption; the AA wiring itself lives on the AA branch and is
  owed. Routes: `GET …/reviews/due`, `GET …/reviews/digest`,
  `POST …/{decision_id}/review` (200), plus `GET /api/decisions/scopes`
  (slice 6, the UI's scope picker: scopes where the caller holds
  `decision.read`, org first, empty list when none — never a 404).
  **Tests: service `tests/test_decision_memory_review.py` 12 passed
  (232.07 s); API `tests/test_decisions_review_api.py` 5 passed
  (82.18 s, including the scopes test).** Bugs found and fixed: (1)
  `UTC` was not imported in the routes module, so `GET …/reviews/due`
  raised `NameError` (500) — the first API run caught it (2 failed);
  (2) `PlatformScope` lives in `app.platform.models`, not
  `app.db.models` — the scopes endpoint failed to import until
  corrected; (3) the authz test expected 404 for a child dept scope,
  but org membership covers child scopes through the platform's
  chain inheritance — the test was wrong (200 with an empty due
  list is correct; slice 3's own tests assert the same for the
  dept scope), fixed to assert 404 only for an unknown scope id.
- Slice 6 (Agent 169): done — Step M **code-complete**. UI:
  `frontend/src/decisions/` — `types.ts`, `api.ts` (scopes/list/get/
  why/dependencies/verify/due/digest/review/revoke/requestApproval),
  `reviewState.mjs` + `reviewState.d.mts` (UTC-calendar-day
  overdue/due/ok/none + `sortDue`, most overdue first, undated last),
  `DecisionsPage.tsx` (scope picker, digest stats, review queue,
  timeline, detail panel with rationale/alternatives/evidence +
  hidden count/dependency view/"why" search/review form with
  outcome-dependent fields, compact record form with optional
  approval-id input). Wiring: `AdminConsole.tsx` (lazy `DecisionsPage`,
  `decisions` view available to all signed-in users — backend enforces
  `decision.read`), `AppSidebar.tsx` (Operations group "Decisions"),
  `ModuleWorkspaces.tsx` (GovernanceWorkspace "Decision memory"
  launch card). `assessment/CHANGES-step-M.md` written with no pending
  markers. **Tests: frontend `npm test` 27 passed, 0 failed
  (22 prior + 5 new reviewState); `tsc --noEmit` clean; `vite build`
  clean (DecisionsPage chunk emitted).** Regressions after slices 5–6:
  slices 2–4 re-run **34 passed, 0 failed** (495.27 s, one combined
  run); slice-1 pure **86 passed, 0 failed**; Alembic check clean,
  single head `0106_decision_records`. One UI bug found by the
  typecheck: the page imported the ui primitives from `./ui` instead
  of `../components/ui`, which cascaded into 13 implicit-any errors —
  fixed, tsc clean. No migration in slices 5–6; Alembic head stays
  `0106_decision_records`. Owed: full backend suite on a quiet VM
  (load control); Step AA digest wiring (other branch); simulation /
  twin / recovery readability when AL/S/AH merge (still hidden).
  Merge notes: no migration collision from slices 5–6; no
  ModuleWorkspaces conflict beyond the one launch card.

## 6. Owner decisions needed

1. **Who may record decisions per scope?** Proposed: members holding a
   new `decision.record` permission; sensitive kinds (finance,
   supplier, people) additionally need an owner approval before the
   record becomes active.
2. **May rationale quote document text?** Proposed: **no** — cite by id
   only; text is fetched at read time through access checks (slice 3
   enforces this; slice 1's evidence model already stores refs only).
3. **Review cadence default?** Proposed: 180 days (`review_at` =
   decided date + 180 days unless the decider sets another date).
4. **Retention?** Proposed: decision records are kept indefinitely
   (they are the company's memory); only superseded records may be
   archived from list views, never deleted.
