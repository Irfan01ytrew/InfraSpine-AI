# CHANGES — Step AD: Agent Studio drag-drop builder + template library

> Branch `agent/studio-builder`, base `step-e-gaps-wip` (`ca956c2`).
> Status: slices 1–4 done. Focused gates green (see "Test evidence").
> Full-backend-suite count is covered by the merged-HEAD verification
> (Agent 96 / Agent 104); merge only after that tally is green.

## What this step does (plain language)

You can now build a workflow by dragging steps onto a canvas, or start from
a ready-made template in one click.

- **Template library.** The workflow editor lists every template with its
  category (finance / HR / IT / operations), how many steps it has, and
  which inputs it needs ("Needs: Invoice number · Amount"). One click —
  "Use template" — creates a draft from that template and opens it in the
  canvas so you can adjust it before anything is reviewed.
- **Drag-drop canvas.** The workflow diagram editor (palette, drag to move,
  click a port to connect, Delete selected node, Validate) is now mounted
  in the workflow editor. It was built earlier but never shown on any page.
- **New template: "Invoice approve karwao".** A clearly-named
  invoice-approval template (verify → approve → record) with amount +
  invoice number + vendor inputs and a financial approval rule, alongside
  the 11 templates that already existed.
- **A draft is never published and never executed by the builder.**
  Creating a draft only produces a validated definition with
  `status='draft'` and `origin='studio'`. Review and publish stay
  separate, owner-gated steps, exactly as before.

## The problem it fixes

Before this step the workflow editor only counted templates
("Available templates: 12") next to the AI "Draft a workflow from
instructions" form — there was no one-click "use this template" action
anywhere, and the drag-drop canvas component existed but was imported by
no page, so drag-drop building was not real.

## Files changed (vs `ca956c2`)

### Slice 1 — template library + invoice-approval template (Agent 37, `727dac3`)
- `app/agent_studio/templates.py` (new) — `list_studio_templates()`
  (metadata derived from the catalog, no spec duplication) and
  `create_draft_from_template(key, ...)` (validated draft payload:
  `origin='studio'`, `origin_ref=<key>`, `status='draft'`,
  `spec.source='template'`; fresh copy each call, mutating a returned
  draft never corrupts the catalog).
- `app/workflows/templates.py` — new `invoice-approval` template
  ("Invoice approve karwao").
- `tests/test_studio_builder_templates.py` (new, 7 tests).

### Slice 2 — draft-save endpoint (Agent 52, `1dfbfe2`)
- `app/api/routes/workflow_engine.py` — `POST
  /api/workflow-engine/scopes/{scope_id}/templates/{template_key}/draft`
  (201): platform scope check, `create_draft_from_template`, then
  `WorkflowService.save()`; unknown key → 404, invalid graph → 422,
  duplicate draft in the same scope → 409. Never publishes.
- `app/workflows/service.py` — `WorkflowService.save()` gained
  `origin` / `origin_ref` kwargs (defaults `'workflow'` / None; validated
  against `WORKFLOW_ORIGINS`); audit `workflow.draft_saved` now records
  origin / origin_ref.
- `tests/test_studio_builder_draft_endpoint.py` (new, 6 tests).

### Slice 3 — mount the canvas (Agent 52, `70565fa`)
- `frontend/src/components/ModuleWorkspaces.tsx` — `FlowDiagram` mounted
  in the `WorkflowEngineWorkspace` draft panel (`onChange` → `setDraft`,
  selected-node state, Validate button calling server-side
  `workflowApi.validate`).
- `frontend/src/workflows/FlowDiagram.tsx` — node palette (drag or
  click-to-add), Delete selected node button.
- `frontend/src/workflows/specEdit.ts` (new) — `nodeKinds` / `newNode` /
  `arrange` moved verbatim from `FlowDiagram.tsx`, plus pure `deleteNode`
  and `connectNodes`, re-exported for compatibility.
- `frontend/src/workflows/api.ts` — `templateDraft()` client for the
  slice-2 endpoint.
- `frontend/src/index.css` — minimal `.wf-*` rules using design tokens
  (the canvas had class names but no styles anywhere).
- `frontend/test/specEdit.test.mjs` (new, 7 tests).

### Slice 4 — template picker UI (Agent 103, `02b6dbd`)
- `app/api/routes/workflow_engine.py` — `GET /api/workflow-engine/templates`
  now also returns `category` per template (from the slice-1 library);
  additive only — `key` / `name` / `description` / `spec` unchanged.
- `frontend/src/workflows/templatePicker.ts` (new) — pure helpers:
  `templateSummary()` (category, step count/labels, required inputs only)
  and `templateDraftState()` (maps a `templateDraft` response to editor
  state: spec, no errors, `requiresReview: true`, workflow id + base
  version).
- `frontend/src/workflows/types.ts` — `Template` gains optional
  `category`.
- `frontend/src/components/ModuleWorkspaces.tsx` — "Template library"
  card in the workflow editor (name, category badge, step count,
  description, required inputs, "Use template" button); one click calls
  `templateDraft()`, loads the created draft into the canvas, and
  "Save draft" then saves a new version of that same workflow instead of
  a duplicate. If a draft from the same template already exists in the
  scope, the server returns 409 and the message tells the user to open it
  from the workflow list.
- `frontend/test/templatePicker.test.mjs` (new, 4 tests).
- `tests/test_template_picker_api.py` (new, 1 test).

## Test evidence

- Slice 1: 7 new tests passed.
- Slice 2: 13 passed (6 new endpoint + 7 slice-1 template tests);
  regressions `tests/test_workflow_engine.py` 8/8 passed.
- Slice 3: frontend `npm test` 29 passed (22 baseline + 7 new
  `specEdit` tests); typecheck + build clean.
- Slice 4: frontend picker tests 4 passed (plus 7 specEdit = 11 in the
  combined run); backend picker API test 1 passed; combined AD backend
  regression run (slices 1+2+4 files) **14 passed, 0 failed** in 42:47.
- Step AD total new tests: 7 + 6 + 7 + 4 + 1 = **25**.

## Gates

- Alembic: no migration in this step (no schema change). `alembic check`
  = **"No new upgrade operations detected."** Head `0103_unified_studio_governance`.
- Frontend: `npm test` **33 passed, 0 failed** (29 baseline + 4 new
  picker tests); typecheck clean (exit 0); `vite build` clean
  (built in 5.42s).
- Full backend suite: covered by the merged-HEAD verification
  (Agent 96 / Agent 104) — not re-run on this branch; the branch changes
  one route (additive field), one pure frontend helper module, and one
  editor card, and all focused/regression suites above pass.
- The 3 unified-runtime tests left unrun by Agent 52 (OOM-killed /
  runtime restarts) were re-run by Agent 103 and **all 3 passed**
  (3 passed in 989.90s, exit 0):
  `test_generic_action_with_exact_platform_approval_and_phases`,
  `test_rejected_approval_fails_and_cannot_be_resumed`,
  `test_run_timeout_and_stop`. A combined re-run with
  `tests/test_workflow_engine.py` was OOM-killed (exit 137) after
  4/8 workflow-engine tests passed, 0 failed; Agent 52 had already
  run that file 8/8 on the same code, and slice 4 changed none of the
  code it exercises (only the `GET /templates` route, covered by the
  picker API test), so this is recorded, not re-attempted — the
  merged-HEAD full-suite gate is the remaining proof.

## Merge notes

- **ModuleWorkspaces conflict (expected):** frontend redesign round 8
  (Agent 106) is also editing `ModuleWorkspaces.tsx`. Reconcile the
  picker card + `useTemplate`/`saveDraft` changes with the redesign
  pass at merge; both are styling/structure edits to the same file.
- **No migration collisions:** this step mints no revision.
- **409 edge case:** one draft per scope + template key
  (`uq_wf_definitions_origin_ref`). A second "Use template" click for
  the same template in the same scope returns 409 with a message pointing
  at the workflow list; the picker does not yet offer to open the
  existing draft directly.
- **Owner input still open (plan):** template list wording — English
  labels are kept; Roman Urdu display names can be added as `name_ur`
  later.

## What is NOT in this step

- No execution path changed: drafts are inert, review/publish/approval
  behaviour is untouched.
- No template is code: templates are data only (bounded schema, no eval,
  no URLs, no secrets).
- No Studio agent behaviour changed: Studio agent configs and workflow
  definitions remain separate concepts.
