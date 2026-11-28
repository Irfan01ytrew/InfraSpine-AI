# Step AM — Self-Evolving Software Foundry: plan

Owner request (2026-11-28 ~00:38 PKT): InfraSpine ke andar ek naya, advanced
product direction — **Self-Evolving Software Foundry**. Yeh AI platform user ki
zaroorat samajh kar **working software applications banaye — interface, backend
aur database ke saath — phir unhein test karke preview de. User ki approval ke
baad app ko deploy kare aur baad mein feedback ke mutabiq improve kare.**

Status: **all 6 slices done** (Agent 53 slice 1, Agent 62 slices 2–3,
Agent 71 slices 4–6). Remaining: final gates (full backend suite,
platform/approvals regressions, clean typecheck + build) and merge to main.
Owner decisions in §5 still open (deploy targets, sandbox limits,
generated-app data rules, cost guard).

## 1. What exists vs what is missing

Reuse, don't redo. Checked against `step-e-gaps-wip` HEAD `ca956c2`:

| Capability the Foundry needs | What already exists in InfraSpine | Gap |
|---|---|---|
| Workflow execution with pause/stop/resume + approvals | `app/workflows/unified.py`, `app/workflows/engine.py`, `execute_once` ledger; Agent Studio adapter `app/agent_studio/unified.py` (Gap 1, Step E) | Foundry build jobs must run as unified-engine workflow runs, not a new runner |
| Workflow templates (start → work → approval → verify → end) | `app/workflows/templates.py` — 12 validated templates (onboarding, procurement, invoice, …) | No template for "build an app"; a foundry template is slice 3 work |
| Agent Studio: agent runs, approvals, tools | `app/agent_studio/service.py` (998 lines), `app/agent_studio/tools.py`, `project_tools.py` | Studio builds *workflows inside* InfraSpine; Foundry builds *standalone apps outside* it — new product surface, shared approval machinery |
| Drag-drop workflow canvas | `frontend/src/workflows/FlowDiagram.tsx` — exists but orphaned (imported by no page; Agent 37 finding, Step AD) | Foundry preview/spec editor can mount this canvas instead of building a new one |
| Disposable test environments | `app/qa/sandbox.py` — disposable loopback PostgreSQL (`disposable_postgres`), clean env, never derives a target from app config | Reuse for generated-app test DBs; no new sandbox mechanism needed |
| Platform auth, approvals, audit | `app/platform/` (`access.py`, `approvals.py`, `side_effects.py`, `outbox.py`) — every lookup/dispatch through `app/platform`; approvals bound to exact action, expiring, single-use | Foundry must route every build/deploy decision through these; no bypass |
| Knowledge / RAG (spec grounding) | `app/rag/` — permission-filtered retrieval; document text is untrusted | Generated specs may cite Knowledge docs, never trust their text for permissions/payees |
| Finance / invoice flow | `app/finance/` — invoice intake, connector, approvals before payment | A generated app that touches money must go through Finance approvals, never direct payment |
| Frontend shell, pages, API client | `frontend/src/` — React 19 + Vite 8 + Tailwind 4, served from `frontend/dist` at `/admin` | Foundry needs one new page (spec → preview → approve → deploy) + API client calls; no new shell |
| Self-healing / QA | Step W detector (`app/self_healing/detector.py`, Agent 29); `app/qa/` harness | Generated-app test failures feed the same incident classification, not a second system |

**Missing entirely (this step builds it):**
- An **app spec** model: name, purpose, interface pages, backend endpoints,
  database tables — validated before anything is generated (slice 1, done).
- A **generator** that turns a validated spec into interface + backend + database
  code (slice 2).
- An **automated test + preview** stage: generated tests run in the QA sandbox,
  preview is a disposable, non-production URL (slice 3).
- A **deploy gate**: deploy only after the user's explicit approval of the exact
  build (hash-bound, single-use, expiring); deploy + rollback (slice 4).
- A **feedback → improve loop**: user feedback becomes a new spec revision,
  re-validated, re-tested, re-approved — never silently patched live (slice 5).
- A **registry + API/UI**: list generated apps, their spec versions, test results,
  preview links, deploy state, feedback (slice 6).

## 2. Design: the Foundry pipeline

```
user need (text / voice / Knowledge docs)
  → 1. SPEC: structured app spec (name, purpose, pages, endpoints, tables)
  → 2. VALIDATE: slice 1 — reject unsafe specs, produce build plan + test checklist
  → 3. GENERATE: interface + backend + database code from the validated spec only
  → 4. TEST: generated tests run in app/qa sandbox (disposable Postgres, clean env)
  → 5. PREVIEW: disposable preview (no production data, no public URL, expiring)
  → 6. APPROVAL GATE: user approves the exact build hash — or rejects / asks for changes
  → 7. DEPLOY: only after approval; deploy target per owner decision (see §5)
  → 8. FEEDBACK: user feedback → new spec revision → back to step 2 (never live-patch)
  → 9. IMPROVE: each revision re-validated, re-tested, re-approved, rollback kept
```

Every stage is a step on the **unified workflow engine** (`app/workflows/unified.py`):
the build job is a workflow run; pause/stop/resume work the same as Agent Studio
runs; every side effect (generate, test, deploy) goes through `execute_once` with
read-back verification. No second runner, no background thread that bypasses the
engine.

## 3. Safety rules (non-negotiable)

1. **Generated code never runs unreviewed.** Generation output is data until the
   spec is validated (slice 1), tests pass in the sandbox (slice 3), and the user
   approves the exact build hash (slice 4). No auto-run, no auto-deploy.
2. **Never touches production data.** Generated apps test against disposable
   sandbox databases only (`app/qa/sandbox.py`). Preview databases are empty
   seed data. The live InfraSpine DB, uploads, and `.env` are never inputs.
3. **Deploy only after explicit user approval.** `requires_approval=True` on
   every build plan (slice 1 enforces this in code — it cannot be set to False
   by a spec). Approval is bound to the exact build hash, expires, single-use,
   separation of duties (the requester cannot approve their own deploy).
4. **Spec text is untrusted.** User text, Knowledge documents, and feedback can
   never grant permissions, change payees, add secrets, or widen what an
   approval covers. Anything that looks like a permission/secret/payment
   instruction inside a spec is rejected, not obeyed (slice 1 reject rules).
5. **Sandboxed execution.** Generated apps run in an isolated sandbox with
   resource limits (owner decision: exact CPU/memory/time limits, §5). No
   network access except allow-listed hosts; no host filesystem; no secrets.
6. **Rollback plan.** Every deploy keeps the previous build; a failed health
   check after deploy rolls back automatically and alerts the owner. A deploy
   is not "done" until its health check passes.
7. **Audit everything.** Every spec version, validation result, test run,
   preview link, approval, deploy, rollback, and feedback item is written to
   the audit log through `app/platform` — same as every other approval in
   InfraSpine.
8. **No secrets in specs.** A spec containing anything that looks like a
   password, API key, token, or private key is rejected outright (slice 1).
   Generated apps receive configuration through the platform secret store,
   never through spec text.

## 4. Slice list (plan order)

1. **Spec validation (DONE, Agent 53)** — `app/foundry/spec.py`: validate an app
   spec (name, purpose, interface pages, backend endpoints, database tables)
   into a build plan + test checklist + preview checklist; reject unsafe specs
   (destructive actions, secrets in spec, direct DB/payment without approval);
   always `requires_approval=True`. Pure functions, no code execution, no
   deployment, no network calls. Tests: `tests/test_foundry_spec.py` (28 tests).
2. **Generator** — turn a *validated* spec into interface (React page stubs
   reusing the frontend shell), backend (FastAPI router stubs on the platform),
   and database (Alembic migration draft, never auto-applied). Output is code
   as data; nothing is executed in this slice.
3. **Test + preview** — generated tests run in the QA sandbox; preview is a
   disposable URL backed by a disposable Postgres; preview checklist from
   slice 1 is executed and reported. Failure blocks the approval gate.
4. **Approval gate + deploy** — approval bound to exact build hash via
   `app/platform/approvals.py`; deploy to the owner-chosen target; health
   check; automatic rollback on failure; deploy recorded in audit log.
5. **Feedback → improve loop** — feedback items attach to a spec version and
   produce a new spec revision; revisions re-enter at slice 1 (re-validate,
   re-test, re-approve). Live apps are never patched in place.
6. **Registry + API/UI** — `GET /api/foundry/apps` list, per-app detail (spec
   versions, test results, preview links, deploy state, feedback), and a
   frontend Foundry page mounting `FlowDiagram.tsx` for the build pipeline
   view. All reads through `app/platform` with `foundry.read`.

## 5. Owner decisions needed

1. **Deploy targets:** where may a generated app be deployed? (Options: same
   free host as InfraSpine Codespaces/Render; a separate free tier; local-only
   preview with no public deploy.) No deploy code until the owner picks.
2. **Sandbox limits:** CPU / memory / runtime limits for generated-app test and
   preview runs, and whether generated apps may call the network at all.
3. **Generated-app data rules:** may a generated app store real user data, or
   is it demo/seed data only until a second, explicit approval?
4. **Cost guard:** generated apps use LLM/model calls during build — per-app
   build budget / spend limit before the build pauses for approval.

## 6. What slice 1 proves

- A well-formed spec (pages + endpoints + tables, all names valid, references
  consistent) produces a deterministic build plan, a test checklist (one test
  per endpoint, one per table, one per page), and a preview checklist.
- Unsafe specs are rejected with a named reason: destructive actions
  (`DROP`, `DELETE FROM`, `TRUNCATE`, …), secrets in any text field,
  direct database/payment access without an approval step, invalid names,
  dangling references (endpoint → missing table, page → missing endpoint),
  empty specs.
- `requires_approval=True` is structural: no input can turn it off.
- Nothing in slice 1 executes code, touches a database, or makes a network
  call — it is pure validation, so it is safe to run anywhere.
