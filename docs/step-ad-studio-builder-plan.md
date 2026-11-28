# Step AD — Agent Studio drag-drop builder + template library: plan

Owner-approved 2026-11-28 (module improvement: "Agent Studio: drag-drop builder —
workflow mouse se kheench kar banao, aur tayyar templates ki library (jaise
'invoice approve karwao' ek click mein)").

## What exists (mapped, not rewritten)

| Piece | Location | State |
|---|---|---|
| Workflow graph schema + validation | `app/workflows/definition.py` (`Definition`, `Node`, `Edge`, `validate_graph`) | Done. Bounded schema (max 100 nodes, no eval/code/URLs, no embedded secrets), graph checks (one start, reachable, no cycles, decision branches, financial policy, critical quorum). |
| Template catalog (11 templates) | `app/workflows/templates.py` (`template_catalog()`) | Done. Code-defined, validated at build via `Definition.model_validate`. Served by `GET /api/.../templates` (`app/api/routes/workflow_engine.py:90`). Templates: onboarding, resignation, procurement, offboarding, it-incident, invoice, document-review, asset, customer-service, project, custom. |
| Draft save / review / publish | `app/workflows/service.py` (`WorkflowService.save` → version `status='draft'`, `review`, `publish`) | Done. `save()` never publishes; review + publish are separate, owner-gated steps. |
| `wf_definitions` origin + status | `app/db/workflow_engine_models.py` | Done. `origin IN ('workflow','automation','system','studio','governance')`, `status IN ('draft','review','published','rejected')`, default `draft`. Studio-origin runs are already mirrored (`app/agent_studio/unified.py`). |
| Drag-drop canvas component | `frontend/src/workflows/FlowDiagram.tsx` | **Exists but orphaned**: draggable nodes, click-to-connect edges, node palette, auto-arrange. It is imported by no page (`grep` finds only itself). The workflow editor in `ModuleWorkspaces.tsx` never mounts it. |
| Template use in UI | `frontend/src/components/ModuleWorkspaces.tsx:101-145` | Templates are fetched (`workflowApi.templates()`) but only shown as a count ("Available templates: N") next to the AI "Draft a workflow from instructions" form. There is **no one-click "use this template"** action anywhere. |
| Agent Studio service | `app/agent_studio/service.py` | Agents/threads/runs with pause/stop/resume on the unified engine (Step E Gap 1). Studio definitions are agent configs, not workflow definitions — the builder must not confuse the two. |

## What is missing (the real gaps)

1. **No one-click template → draft path.** Nothing turns a template key into a
   saved draft `wf_definition` (`origin='studio'`, `origin_ref=<template key>`,
   `status='draft'`). The API serves the catalog; the UI only counts it.
2. **No Studio-side template library.** No module that lists templates with
   friendly metadata (category, steps, required inputs) and produces a
   *validated* draft payload. Each caller would re-implement validation.
3. **Canvas is dead code.** `FlowDiagram.tsx` must be mounted in the workflow
   editor (slice 3) before drag-drop building is real.
4. **No dedicated "invoice approve karwao" template.** The generic `invoice`
   template exists ("Invoice review and payment authorization": verify →
   review → end), but there is no explicit, clearly-named invoice-approval
   template with amount + invoice number + vendor inputs and a record step.

## Design rules (from handover §5 + repo AGENTS.md)

- A template is **data, never code**: only node kinds, labels, positions and
  configs from the bounded schema. No eval, no URLs, no secrets in specs
  (`bounded_data` already rejects secret-looking keys).
- A draft is **never published and never executed** by the builder. Creating a
  draft only produces a validated `Definition` + `status='draft'` payload;
  review and publish stay separate, owner-gated, and go through
  `WorkflowService` (`check_definition` re-validates scope/contracts).
- Builder output is a **normal workflow definition**: same `Definition` schema,
  same `validate_graph` rules, same approval/`execute_once` behaviour at run
  time as a hand-written definition. The canvas only edits nodes/edges/positions.
- Every lookup/dispatch through `app/platform`; template text is untrusted
  input and can never grant permissions, change payees, or change what an
  approval covers. Approval nodes keep their financial policy
  (amount variable + high/critical risk) and critical-quorum rules.
- No breaking changes: existing catalog templates, `template_catalog()`, the
  `/templates` API shape, and `FlowDiagram` props are preserved; this step only
  adds.

## Slices

1. **(DONE in this prep) Template library** — `app/agent_studio/templates.py`:
   `list_studio_templates()` (metadata derived from the catalog, no spec
   duplication) + `create_draft_from_template(key, ...)` (validated draft
   payload: `origin='studio'`, `origin_ref=key`, `status='draft'`,
   `spec.source='template'`); plus a new `invoice-approval` template
   ("Invoice approve karwao") in `app/workflows/templates.py`. Pure functions,
   no DB/IO. Tests: `tests/test_studio_builder_templates.py`.
2. **Draft-save endpoint** — `POST /api/studio/templates/{key}/draft` (or the
   workflow route): `app/platform` scope check, `create_draft_from_template`,
   then `WorkflowService.save()`; returns the draft id. Rejects unknown keys
   (404) and invalid graphs (422). Never publishes.
3. **Mount the canvas** — use `FlowDiagram` in the workflow editor page with
   the node palette, drag, connect, delete, and `arrange`; editing produces a
   `Definition` that is validated client-side and saved as a draft via slice 2.
4. **Template picker UI** — list templates with category/steps/required inputs
   in Studio; one click creates the draft and opens it in the canvas.
   The draft opens in `review` state only after the owner submits it.
5. **Docs + gates** — `CHANGES-step-AD.md`, full backend suite green,
   Alembic check clean (no migration expected), frontend tests + build +
   typecheck clean, then merge to main.

## Test list

- Slice 1 (this prep): library lists all templates with metadata, no duplicate
  keys; every template produces a draft with empty `validate_graph` errors;
  `invoice-approval` draft has an approval node with financial policy
  (expense, `input.amount`, high risk); unknown key raises; custom
  name/description override works; mutating a returned draft never corrupts
  the catalog (fresh copy each call).
- Slice 2: endpoint creates a draft row (`origin='studio'`, `status='draft'`)
  for a known key, 404 for unknown, 422 for a tampered spec; non-members get
  404, members without permission get 403.
- Slice 3: canvas renders a template draft, drag moves a node, connect adds an
  edge, invalid edits show validation errors, save produces a draft.
- Slice 4: picker lists templates, one click creates a draft and opens the
  canvas with the draft loaded.

## Owner inputs needed

- None for slice 1. For slice 4: confirm the template list wording (English
  labels are kept; Roman Urdu display names can be added as `name_ur` later).
