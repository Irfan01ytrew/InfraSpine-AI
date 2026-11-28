# Step G — Company Continuity Engine: implementation plan

Status: **preparation + first safe slice** (branch `agent/stepg-continuity`, base
`step-e-gaps-wip` `ca956c2`). Step G comes after F in plan order; this document
maps what already exists, what is missing, and the order the rest is built in.
It does not change any live behaviour by itself.

Sources: `assessment/master-plan-v2.md` §1 (Step G: 20–28 h) and §3 (G may add
"graph merge start, access-checked handover, five disruptions, restore drill";
it must NOT pull in horizon predictions, the full what-if engine, or structured
leaver packs — those belong to K/L), `HANDOVER-PROMPT.md` §4–§5, and the code
survey below (all paths relative to the repo root).

## 1. What Step G must deliver (acceptance)

1. **Impact of unavailability.** For a person, system, or supplier being
   unavailable, show which processes/owners are hit, on the merged company
   graph, with an `effective_permissions` gap list (a recorded `backup_for`
   link is not enough — the replacement must actually hold the access).
2. **Access-checked handover.** A replacement is granted only after an
   evidenced backup relationship AND a recorded required-skills check; the
   grant is time-limited, and the replacement resumes pending work from the
   last verified step, not from zero. After revoke, the leaver/replaced
   person cannot open the pack or resume the work.
3. **Five disruption drills.** Five seeded disruption kinds run as drills
   (resignation, absence, server outage, database failure, supplier
   disruption) with a recovery plan that can be compared against a
   simulation; differences are listed.
4. **Backup → restore with pending jobs.** A verified backup can be
   restored and pending workflow jobs resume; restore is a drill first,
   never a silent production change. Rollback on live is file-restore
   only — never `alembic downgrade` on live.
5. **First live merge of the two company graphs.** `app/company_graph/*`
   and `app/continuity/*` stop being two tenant tables for the same
   entities: one id, one scope, seeded from live memberships.
6. **All continuity actions run through the unified workflow engine**
   (`app/workflows/unified.py`) and `app/platform` authorization
   (`resolve_principal`, `authorize`, `require` → 404 non-members, 403
   members without permission). Every side effect goes through
   `execute_once` with read-back verification. Approvals are bound to the
   exact action (hash), expire, single use, separation of duties.

## 2. What already exists (do not rewrite)

| Area | Existing code | Notes |
|---|---|---|
| Continuity graph + BIA | `app/continuity/models.py` (`bc_nodes`, `bc_links`, `bc_scenarios`), `intelligence.py` (`bia_score`, `analyze`, `handover_draft`, `impact_score`/`risk_score`), `schemas.py` (12 scenario kinds, `backup_for` links) | Impact is computed on a static, separately maintained graph, not on live work. `backup_for` is a recorded link only — no effective-permission check. |
| Continuity plans/runs | `app/continuity/service.py` (547 lines: tenants, grants, nodes, links, BIA, scenarios, plans `bcp`/`drp_reference`/`handover`, activation, tasks, evidence, `verify_task`, dashboard), `routes.py`, `access.py`, `journal.py` | Activation dispatch is its own code path — **not** the unified engine, no `execute_once`. Handover plan kind exists but pending work is not auto-listed from workflows/approvals/agent runs. |
| Recovery | `app/recovery/*` (`models.py`, `service.py`, `worker.py`, `policy.py`, `adapters.py`, `evidence.py`): incidents, playbooks, approved commands, worker that runs an approved command with its own `_execute_once` | Not hooked to live workflow/agent/integration failures. Playbooks do not auto-start. No allow-list of self-heal-permitted incident types (that is step M, not G). |
| Company graph | `app/company_graph/*` (`service.py`, `intelligence.py` forward/reverse impact depth 6, `discovery.py`, `schemas.py` with `Simulation`) | Second graph, second tenant table, mostly manual/API-ingested, not fed live by HR, workflow, agent activity or connectors. |
| Health / readiness | `app/api/routes/health.py` (`/health` liveness, `/health/ready` DB + ingestion workers), `app/services/operational_health.py`, `app/rag/ingestion.py` (`workers_online`, heartbeat, 3 missed beats = stopped) | No API/worker heartbeat monitoring, no backup-freshness check, no continuity dashboard health roll-up. |
| Watchdog (PC) | `scripts/ops/windows/watchdog.ps1` (every 5 min scheduled task `InfraSpine-IRFANIX-Watchdog`), `run-api.ps1`, `run-worker.ps1` | Restarts a stopped API; does not restart a healthy one. Windows-only; no equivalent health loop in the cloud/Docker stack beyond compose healthchecks. |
| Backups (live) | Live-push method: timestamped file backup + `pg_dump` before any migration; backups in `C:\Users\10\Downloads\RAG infraspine\infraspine-live-backups\` | Manual procedure in docs only. **No backup/restore code in `app/` at all** — no manifest, no verification, no restore drill. `pg_dump` appears nowhere in `app/`. |
| Platform core | `app/platform/*` (`resolve_principal`, `authorize`, `require`, approvals with SHA-256 hash, `execute_once` with read-back, outbox) | Continuity and recovery services do not consistently use it (see §3). |
| Frontend | `frontend/src/continuity/` (`api.ts`, `types.ts`, `common.tsx`) | Graph/plan UI exists; no health roll-up, no drill comparison, no restore UI. |
| Tests (thin) | `tests/test_continuity.py` (4 tests), `tests/test_recovery.py` (15), `tests/test_company_graph.py` (12), `tests/test_health_readiness_unit.py`, `tests/test_worker_healthcheck.py` | Continuity coverage is thin; no drill, handover-resume, or backup tests exist. |

## 3. What is missing (the Step G work list)

1. **Graph merge (foundation for K).** One entity id + one scope across
   `company_graph_records` and `bc_nodes`; seed from live memberships;
   outbox event → graph update; stale sources marked stale. No
   cross-tenant/out-of-scope node visible to a restricted user.
2. **Effective-permission check on handover.** `handover_draft` must
   verify the replacement actually holds the required scopes/permissions
   (via `app/platform`), and list the gap when it does not. A
   time-limited grant is issued only after the check passes.
3. **Pending work at handover.** Auto-list pending items from unified
   workflow runs (last verified step), open approvals, agent runs, and
   tickets — not prose notes (`app/services/handoffs.py` is prose-only).
4. **Unified-engine activation.** Continuity activation plans become real
   workflow runs on `app/workflows/unified.py` (pause/stop/resume from
   last verified step, exact-action approvals, `execute_once`), with the
   existing continuity APIs kept working through adapters (same pattern
   Step E used for Agent Studio/governance).
5. **Recovery hooked to failures.** Recovery incidents open from
   workflow/agent/integration failures (human proposes/approves in G;
   auto-start allow-lists are step M, not G).
6. **Five disruption drills.** Seeded drills for resignation, absence,
   server outage, database failure, supplier disruption; each produces a
   recovery plan; drill result is comparable to the simulation and
   differences are listed. Drills are side-effect free (no production
   dispatch).
7. **Backup → restore.** Backup manifest (id, time, Alembic head, file
   list with SHA-256 + sizes), manifest verification (missing/tampered/
   stale/wrong-head), restore drill on a throwaway database, pending-job
   resume after restore. **First slice implemented now — see §5.**
8. **Health roll-up.** Component health (API, database, workflow worker,
   ingestion worker, vector index, backup freshness) aggregated to one
   continuity status for the dashboard. **First slice implemented now —
   see §5.**
9. **Runbooks + docs.** `CHANGES-step-G.md` (plain language, file list,
   test counts), operator runbook for drill + restore, frontend
   health/drill views.

## 4. Build order (each slice finished + tested before the next)

Note (2026-11-28, Agent 34): the Master Agent re-scoped slices 2–4 to
deliver the backup/restore/unified-engine core first, ahead of the
handover check and graph merge. The order below is the one actually
built; the original order (health endpoints → handover check →
unified activation) is superseded for slices 2–4, and the handover
check + graph merge move to slices 5–6.

1. ✅ **Slice 1 — health-check + backup-manifest pure module** (this
   branch): `app/continuity/health.py` + `tests/test_continuity_health_manifest.py`.
   No I/O, no live data, no secrets.
2. ✅ **Slice 2 — pg_dump backup + freshness check:** `app/continuity/backup.py`
   (`create_backup`, `latest_manifest`, `check_backup`, `current_alembic_head`)
   + `tests/test_continuity_backup.py`.
3. ✅ **Slice 3 — restore verification (restore drill):** `app/continuity/restore.py`
   (`verify_restore`: manifest check → restore into a throwaway `*_test`
   DB → Alembic-head + table read-back → drop) + `tests/test_continuity_restore.py`.
4. ✅ **Slice 4 — unified-engine activation:** `app/continuity/unified.py`
   (`continuity.execute` handler + `mirror_continuity_run` + engine sweep
   + cancel hook) + `tests/test_continuity_unified.py`. Existing APIs kept
   through adapters.
5. ✅ **Slice 5 — effective-permission handover check** on `handover_draft`
   (`app/continuity/handover.py`: `required_permissions`, `check_candidate`,
   `handover_check`, `propose/decide/revoke_handover_grant`,
   `handover_access_active`) + time-limited grants (`HandoverGrant`,
   migration `0105_continuity_handover_grants`) + routes + tests
   (`tests/test_continuity_handover.py`, `tests/test_continuity_handover_pure.py`).
6. ✅ **Slice 6 — graph merge start** (one id/scope, seed from memberships)
   (`app/continuity/graph_merge.py`: `seed_graph_merge`,
   `merge_status`; `GraphLink`, migration `0106_continuity_graph_links`;
   pending `GraphChange` proposals only, never direct writes) + tests
   (`tests/test_continuity_graph_merge.py`,
   `tests/test_continuity_graph_merge_pure.py`).
7. ✅ **Slice 7 — five disruption drills + drill/simulation comparison**
   (`app/continuity/drills.py`: `seed_drills`, `drill_comparison`,
   `compare_drill_to_simulation`; kinds resignation, absence,
   server_outage, database_failure, supplier_disruption) + tests
   (`tests/test_continuity_drills.py`, `tests/test_continuity_drills_pure.py`).
8. ✅ **Slice 8 — frontend health/drill views, `CHANGES-step-G.md`, full
   suite + Alembic round-trip + frontend clean, merge to main.**
   Frontend: `continuity/types.ts`, `continuity/api.ts`,
   `continuity/drillSummary.mjs` (+ node test), Continuity workspace cards
   in `ModuleWorkspaces.tsx`. Docs: `assessment/CHANGES-step-G.md` (this
   file's §4). Gates: `alembic upgrade head` to 0106 + `alembic check`
   clean; DB test counts in `CHANGES-step-G.md` (Agent 114).

Note (2026-11-28, Agent 114): slices 5–8 implemented on branch
`agent/stepg-continuity`. Merge notes: migration `0104` is claimed 4×
(continuity vs HR vs WhatsApp AE vs AI freshness) and `0105` is shared
by AK, AI, AB and Step G — renumber at merge; the Step G chain is
`0104 → 0105 → 0106` internally. The slice 2–4 canonical DB re-run
(`tests/test_continuity_unified.py` + `tests/test_continuity.py`) is
still owed a quiet-VM attempt before merge.

## 5. Slice 1 — what was implemented (this branch)

`app/continuity/health.py` — pure functions, standard library only, no I/O,
no database, no network, no secrets:

- `evaluate_probe()` — normalises one component probe (api, database,
  workflow_worker, ingestion_worker, vector_index, backup) into a
  `Probe`; rejects unknown components and invalid statuses.
- `aggregate_health()` — rolls probes up to one overall status
  (`ok` / `degraded` / `down`); a stale probe (older than the freshness
  window, default 5 min) counts as `unknown` and degrades the roll-up;
  `down` beats `degraded` beats `unknown` beats `ok`.
- `build_backup_manifest()` — builds a manifest dict (backup id, kind,
  created time, Alembic head, database name, file entries with path,
  size, SHA-256).
- `verify_backup_manifest()` — checks a manifest against the files
  actually present: missing file, size mismatch, SHA-256 mismatch,
  duplicate path, empty file list, stale backup (default max age 24 h),
  Alembic head mismatch. Returns `{"valid": bool, "problems": [...]}`.
- `backup_freshness()` — `fresh` / `stale` / `missing` for a manifest.
- `summarize_health()` — one-line roll-up for the dashboard.

Tests: `tests/test_continuity_health_manifest.py` — 18 tests, database-free
(runs in a minimal pytest venv with `--noconftest`; the file imports only
the standard library and `app.continuity.health`, so it also runs under the
full suite). Test counts are in the final report.

## 6. Data-integrity and safety guarantees (whole step)

- Backups are verified by SHA-256 before any restore is attempted; a
  failed manifest blocks the restore.
- Restore is always a drill on a throwaway `*_test` database first; a
  production restore needs the owner's explicit OK and a verified backup
  taken just before it.
- Pending jobs resume from the last verified step via the unified engine
  ledger — never re-dispatched, never restarted from zero.
- A disruption drill writes no `platform_action_executions` in the
  dispatched state and changes no production records.
- Every continuity lookup/dispatch goes through `app/platform`; document
  and message text is untrusted and can never grant permissions, change a
  payee, or change what an approval covers.
- Live DB: never touched by tests; tests use only `*_test` databases via
  `scripts/irfanix_postgres_tests.py`, with pytest log capture on.

## 7. What Step G still needs from the owner

- Nothing new for the preparation slice. For the later slices: which five
  disruptions to drill first (default: resignation, absence, server
  outage, database failure, supplier disruption), and confirmation that
  the PC backup folder (`infraspine-live-backups`) is the canonical
  backup location for the restore drill.
- Step F (WhatsApp) must finish before Step G starts in full, per plan
  order. Step H (founder scenario) then proves D–G end to end.
