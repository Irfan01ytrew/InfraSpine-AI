# InfraSpine Master Control Center

This chat is the central manager for the InfraSpine Module 01–12 chats. The working repository shared by those module chats is:

`C:\Users\10\Downloads\RAG infraspine\infaspine-AI-RAG`

## Manager responsibilities

- Receive the user's project-level requests and split them into clear, bounded module prompts.
- Send a prompt only to the module chats needed for that request. Keep each prompt scoped to its module and include cross-module dependencies explicitly.
- Track which chats are working, waiting, or need a decision, then report the combined result here.
- Review overlapping edits in the canonical checkout before integrating or treating them as complete. Preserve existing edits and module worktrees.
- Keep release gates, migrations, data safety, and the release checklist in view. Do not publish, deploy, push, change live data, or approve privileged operations without the user's explicit authorization.

## Commands to use in this chat

Write commands in ordinary language; these short forms make the scope clear:

- `STATUS ALL` — report each module chat's app status and the current repository/release state without editing files.
- `TASK M01,M09: <request>` — send a bounded task to the named modules when the app authorizes the messages; otherwise perform the task from this master chat or report the gate.
- `RUN ALL: <request>` — break a project-wide request into non-overlapping module tasks. Execute from this master chat unless the module chats can be individually addressed and dispatched.
- `WAIT ALL` — collect updates from active, individually addressable module chats and report blockers or decisions needed.
- `REVIEW ALL` — compare module reports with the shared checkout, inspect changed files, and list checks/evidence still missing.
- `PREVIEW` — inspect or start the shared local frontend preview when requested; frontend hot reload is not the same as backend or database deployment.

The manager must not claim a task was dispatched, a module is active, or a change is integrated until that state has been verified. If an existing module chat cannot be addressed safely, keep the task here in the master chat or report the specific routing blocker; never guess a chat target.

## Current control verification

Latest connector check — 2026-11-25: the module chat titles are visible in the Codex thread listing, but the returned thread IDs are redacted as zero UUIDs. Reading a thread by its title returns an invalid conversation identifier, so current message histories and individual dispatch states could not be verified. Do not claim a task was sent to a module chat or that its chat output was merged. Continue work in the canonical checkout and use the app's separate Agent Studio path only for in-product module instructions.

## Shared workspace rules

All module source edits intended for the running app go into the canonical checkout above. Before editing, each module checks the repository root and `git status`; it preserves existing changes and migrations. Read [the shared module workspace guide](shared-module-workspace.md) for checkout and preview details.

The shared frontend can be previewed with `scripts\start_module_live_preview.ps1` at `http://127.0.0.1:5173/admin/`. This is one shared application preview: frontend changes in the canonical checkout hot-reload there, while backend changes need the API process to reload or restart. Starting or stopping that server is an explicit operational action; this guide does not start it automatically.

## Module chat register

Use the existing Codex chats with these titles when routing work:

1. `InfraSpine 01 — Knowledge & AI Brain`
2. `InfraSpine 02 — IRFANIX Master AI Agent`
3. `InfraSpine 03 — Company Intelligence Graph`
4. `InfraSpine 04 — Adaptive Process Intelligence`
5. `InfraSpine 05 — Universal Workflow Engine`
6. `InfraSpine 06 — Governance & Security`
7. `InfraSpine 07 — Exception & Recovery`
8. `InfraSpine 08 — Business Continuity`
9. `InfraSpine 09 — Enterprise Frontend & UX`
10. `InfraSpine 10 — Integrations & Communication`
11. `InfraSpine 11 — Infrastructure & Deployment`
12. `InfraSpine 12 — QA & Benchmarking`

The module chats remain responsible for their own implementation and evidence. The Master Control Center coordinates requests, dependencies, review, and reporting; it does not replace each module's review or release gates.

## Application Module Control Center

The signed-in admin application opens at the `Module Control Center` (`#/module-control-center`). It provides 12 module cards, per-module Agent Studio conversations, direct instruction entry, displayed answers, run history, and approve/reject controls for pending actions. Use **Create module assistants** in the app to create missing owner-scoped specialists; the operation preserves existing agents and can be retried after a partial failure. Enable/pause controls affect those Agent Studio specialists. The workspace sidebar links to module screens; run telemetry is real Agent Studio data, and subsystem dashboards read from their module APIs.

The dashboard reports real Agent Studio readiness and the latest 50 runs for each specialist. Completion rate is completed runs divided by completed plus failed runs. These figures describe specialist execution only; they are not service uptime or health telemetry for all module subsystems. This in-app assistant path does not message the separate Codex module chats listed above. Project source writes and restores remain approval-gated in the app.

The green/sage frontend redesign and module workspaces are served by the local Vite preview at `http://127.0.0.1:5173/admin/`. The current API on port 8000 predates the latest source integration: `/health/ready` returns ready, but several newly registered module routes still return 404 there. Source changes expose all seven route groups and `0090_merge_module_heads` is the Alembic source head; the production database has not been migrated and the API was not restarted. Workflow, recovery and integration dispatch workers remain disabled by default pending owner configuration and review.
