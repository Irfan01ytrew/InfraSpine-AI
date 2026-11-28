# Step M — Decision / Organizational Memory: what changed

Step M is the company's memory of its own decisions: what was decided,
why, by whom, when, on what evidence, what alternatives were
considered, what it supersedes, and what depends on it. It answers,
months later, "why did we decide this?" — with the receipts attached.

## What a decision is

- **Title, rationale, decider, time, kind, status.** Kinds: policy,
  finance, operations, supplier, people, technology, other. Statuses:
  active, superseded, revoked, expired.
- **Evidence is references only, never copied text.** A decision cites
  a document, a governance proposal, a drill run, or a simulation by
  id. At read time each reference is checked against what the reader
  may read; unreadable evidence is dropped from the view (and counted,
  so the page can say "2 sources hidden").
- **Rationale is the decider's own words.** It must not quote document
  text — cite by id only. Instruction-like wording in a rationale is
  flagged (`needs_review`); it is never executed, never interpreted
  as an instruction.
- **History is append-only.** A decision is never edited or deleted.
  It is superseded by a new decision, or revoked, or expired — and the
  old record stays readable with its status.
- **Every lifecycle event writes one audit row** (`decisions.recorded`,
  `decisions.superseded`, `decisions.revoked`, `decisions.reviewed`)
  and one row in the append-only `decision_events` table.
- **A content fingerprint** (deterministic SHA-256 over the canonical
  record) lets a later reader prove the record they see is the one
  that was decided.
- **Review date.** Every decision carries a `review_at`, defaulting to
  the decision date + 180 days. Overdue decisions surface in the
  review queue; the review digest is built for Step AA scheduled
  reports.

## What was built, slice by slice

### Slice 1 — Pure record model (Agent 120, `694e044`)

`app/decision_memory/records.py` (stdlib only): `DecisionRecord`,
`validate_decision`, `validate_graph` (cycle + dangling ref + status
rules), `supersede_record`, `decision_fingerprint`, `visible_evidence`,
`flag_suspicious`. Tests: `tests/unit/test_decision_memory_records.py`
— **84 passed, 0 failed**.

### Slice 2 — Persistence (Agent 125, `29653d8` + `a76d949`)

`app/db/decision_models.py` (`decision_records` + append-only
`decision_events`) + migration `0106_decision_records`
(down_revision `0103`) + `app/decision_memory/service.py`
(org-scoped `record_decision` / `supersede_decision` /
`revoke_decision` / `get_decision` / `list_decisions` /
`verify_fingerprint` through `app/platform` `require()`; consumed
approval for sensitive kinds — finance, supplier, people — bound to
the exact action, single-use, separation of duties; supersede/revoke
refuse when active dependents exist). Permissions `decision.record` /
`decision.read` / `decision.approve` registered in the permission
catalog. Tests: **16 passed, 0 failed**; Alembic roundtrip clean,
head `0106_decision_records`.

### Slice 3 — API (Agent 140)

`app/api/routes/decisions.py` (`GET/POST
/api/decisions/scopes/{scope_id}/…`: record, list with kind/status/
decider/date filters, get one, "why" query, dependency view,
fingerprint verify, supersede, revoke, plus `POST …/approvals`) +
`app/decision_memory/readable.py` (per-kind readability; simulation /
twin_snapshot / recovery_plan always hidden until AL/S/AH merge).
Roles `decisions.admin` / `decisions.recorder` / `decisions.reader`
seeded at API startup. Tests: API **7 passed** + pure **86 passed** +
service regressions **16 passed** = 109 total, 0 failed.

### Slice 4 — Capture paths (Agent 154, `454f493`)

`app/decision_memory/capture.py` (`record_from_governance` /
`record_from_drill` / `record_from_simulation`, all delegating to
slice-2 `record_decision`) + `POST …/capture/governance|drill|
simulation` (201) + `GET …/drills/{run_id}/decisions`. Recording from
a proposal is approval-gated and **never automatic** — no governance
decide/execute or drill flow calls capture. Tests: service **7 passed**
+ API **4 passed** + slice-2/3 regressions **23 passed** + slice-1
pure **86 passed** = 120 total, 0 failed. Alembic check clean, head
`0106_decision_records`, no migration.

### Slice 5 — Review lifecycle (Agent 169)

`app/decision_memory/review.py` (`review_decision` /
`list_due_for_review` / `review_digest`):

- **Confirm** — the decision still stands; its review date moves to
  today + 180 days by default, or an explicit future date.
- **Expire** — status becomes `expired`. Expiring a sensitive kind
  needs a consumed platform approval bound to the exact action
  `decision.expire`.
- **Supersede / revoke** — delegate to the slice-2 service functions,
  so the new row, approval, and separation-of-duties rules apply
  unchanged.
- Every outcome needs `decision.record` (checked first — 403 before
  any 404) and a 1–500 character reason. Only active decisions are
  reviewable.
- `GET …/reviews/due` (active decisions whose review date falls
  within the window, most overdue first) and `GET …/reviews/digest`
  (overdue / due-soon / total-active / needs-review counts + due
  rows) — the digest is built for Step AA scheduled-report
  consumption.

Tests: service `tests/test_decision_memory_review.py` — **12 passed**;
API `tests/test_decisions_review_api.py` — **5 passed** (including the
scopes endpoint test). 0 failed in both runs.

Bugs found and fixed during slice 5: (1) `UTC` was not imported in
the routes module, so `GET …/reviews/due` raised `NameError` (500) —
the first API run caught it (2 failed); (2) `PlatformScope` lives in
`app.platform.models`, not `app.db.models` — the scopes endpoint
failed to import until corrected; (3) the authz test expected 404 for
a child department scope, but org membership covers child scopes
through the platform's chain inheritance — the test was wrong (200
with an empty due list is correct; slice 3's own tests assert the
same for the dept scope), fixed to assert 404 only for an unknown
scope id.

### Slice 6 — UI + docs + gates (Agent 169)

`frontend/src/decisions/`:

- `types.ts` — decision, scope, digest, dependency, review/record
  input types.
- `api.ts` — scopes, list, get, why, dependencies, verify, due,
  digest, review, revoke, requestApproval.
- `reviewState.mjs` + `reviewState.d.mts` — UTC-calendar-day review
  state (overdue / due / ok / none) and `sortDue` (most overdue
  first, undated last).
- `DecisionsPage.tsx` — scope picker, digest stats, review queue,
  decision timeline, detail panel (rationale, alternatives,
  evidence + hidden count, dependency view, "why" search, review
  form with outcome-dependent fields), compact record form with an
  optional approval-id input for sensitive kinds.

Wiring: `AdminConsole.tsx` (lazy-loaded `DecisionsPage`, view
`decisions`, available to all signed-in users — the backend enforces
`decision.read`), `AppSidebar.tsx` (Operations group, "Decisions"),
`ModuleWorkspaces.tsx` (GovernanceWorkspace "Decision memory" launch
card). New route: `GET /api/decisions/scopes` (scopes where the
caller holds `decision.read`, org first, empty list when none —
never a 404).

Tests: frontend `npm test` — **27 passed, 0 failed** (22 prior + 5
new review-state tests); `tsc --noEmit` clean; `vite build` clean
(`DecisionsPage` chunk emitted). One UI bug found by the typecheck:
the page imported the ui primitives from `./ui` instead of
`../components/ui`, which cascaded into 13 implicit-any errors —
fixed, tsc clean.

## Gates

- Backend (slice 5 + 6): service review **12 passed**, API review
  **5 passed**, frontend review-state **5 passed** — all 0 failed.
- Regressions (slices 2–4 re-run after slices 5–6): **34 passed, 0
  failed** in 495.27 s (service 16 + API 7 + capture 7 + capture API
  4, one combined run); slice-1 pure tests **86 passed, 0 failed**
  (`--noconftest`, 11.32 s).
- Frontend: `npm test` **27 passed, 0 failed**; `tsc --noEmit`
  clean; `vite build` clean.
- Alembic: no migration in slices 5–6; head stays
  `0106_decision_records`; `alembic check` clean ("No new upgrade
  operations detected"), single head `0106_decision_records`.
- Full backend suite: **owed on a quiet VM** (load control — the Step
  E verification pass was running on this VM).

## What is still owed

- **Full backend suite on a quiet VM** before merge (load control).
- **Step AA digest wiring**: `review_digest` is built for scheduled
  reports; the AA branch consumes it (other branch, not this one).
- **Simulation / twin / recovery readability**: `simulation`,
  `twin_snapshot`, and `recovery_plan` evidence stays hidden until
  Steps AL / S / AH merge.
- **Simulation / twin / recovery capture readability**: capture from
  a simulation stays hidden from readers until Step AL merges.

## Merge notes

- No migration from slices 5–6; `0106_decision_records` stays Step
  M's. Migration `0106` is also claimed by Step K (twin snapshot
  versions), Step G (graph links), and Step L (forecast records) —
  renumber at merge.
- `ModuleWorkspaces.tsx` touch: one launch card in the
  GovernanceWorkspace only.
- No live data touched; tests only on throwaway `*_test` DBs.

## Owner decisions (from the plan, still open)

- Who holds `decision.record` per scope — proposed: members holding
  the permission; sensitive kinds additionally need an owner approval
  before the record becomes active.
- Rationale must not quote document text — cite by id only (enforced
  since slice 1).
- Review cadence default 180 days (implemented).
- Retention indefinite; superseded records may be archived from list
  views, never deleted.
- Whether executed governance decisions should ever auto-record a
  decision — currently refused by design; needs an explicit owner
  call (capture is always a human act).
