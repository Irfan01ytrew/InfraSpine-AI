# Step W — Self-Healing + Auto-Tests: what changed

Step W is code-complete: slices 1–8 are done. The software watches its own
health, classifies faults, applies only safe, verified, reversible remedies
on its own, runs its own test suite on a schedule, and escalates to the
owner in plain language whenever a fault is unsafe to fix automatically,
keeps failing, or is unknown.

Hard rule, kept throughout: self-healing never changes production data,
grants permissions, changes payees, or moves money. A remedy that touches
data, permissions, approvals or payments is not a remedy — it is an
escalation.

## What was built, slice by slice

### Slice 1 — Fault detector (Agent 29)
Pure functions in `app/self_healing/detector.py`: signals become incidents
(`api_down`, `database_unreachable`, `worker_stalled`, `error_rate_spike`,
`stale_lock`, `ingestion_backlog`, `transient_failure`, `unknown`), the
decision is remedy vs escalate with a 3-attempt budget and a 5-minute
cooldown, and the escalation report is scrubbed of secrets before it
reaches the owner. 27 tests passed.

### Slice 2 — Signal collection (Agent 149)
`app/self_healing/collector.py` reads liveness, `SELECT 1`, worker
heartbeats, ingestion queue depth, expired leases and caller-supplied
error counts into probes, and stores probe snapshots plus one incident row
per fault key (`self_healing_probes`, `self_healing_incidents`, migration
`0104_self_healing_probes`). The backup probe is always `unknown` — no
backup code exists in `app/`. If the database itself is down, storing is
impossible, so that escalation must travel outside the database (slice 4
built that path). Real bug fixed: an `unknown` workflow probe shadowed a
`degraded` ingestion probe, so `ingestion_backlog` was never classified
when both were present. 10 tests passed.

### Slice 3 — Remedy executor (Agent 149)
`app/self_healing/executor.py` runs the detector decision through
`execute_once` (duty `self_healing.remedy`, a new permission deliberately
NOT a department capability and NOT in the legacy admin bridge) with
read-back verification, and records every attempt on the incident row and
in the audit log. Verified handlers exist for `clear_stale_lock` and
`requeue_job`; `retry_once` runs a caller-supplied operation once.
`restart_worker` and `switch_fallback` have NO verified handler yet (no
supervisor API / configured fallback target exists) — the executor
escalates them instead of pretending. 9 tests passed.

### Slice 4 — Escalation channel
The owner report now has a delivery record. `SelfHealingEscalation`
(migration `0105_self_healing_escalations`) stores one row per incident
key: the scrubbed report, when it was delivered, and whether a human has
acknowledged it. A fault that recurs after acknowledgement reopens the
escalation instead of silently reusing the old one. The executor records
an escalation on both escalate paths (explicit escalate and budget
exhausted), so an escalated incident is never silent. The admin UI is the
delivery channel: `GET /api/self-healing/overview|incidents|escalations`
and `POST /escalations/{key}/acknowledge`, gated by the new explicit
permission `self_healing.read` (NOT a department capability, NOT in the
legacy admin bridge, like the remedy duty). When the database itself is
down, the report goes to a configured file as one scrubbed JSON line
(`escalate_out_of_band`); with no file configured it reports that nothing
was delivered instead of pretending. WhatsApp/email delivery does NOT
exist yet — it depends on Step F / integrations, and non-`admin_ui`
channels are refused, never faked. 15 tests passed.

### Slice 5 — Auto-test loop
`app/self_healing/autotest.py` runs a test suite on a throwaway database
and triages the result: a passing run is recorded, a failing run becomes
a `self_test_failed` incident (high severity, escalate-only — a failing
test never triggers a remedy) with the failing test names in the evidence,
and a run with no parseable summary is recorded as an error, never a
faked pass. The run history lives in `self_healing_test_runs` (migration
`0106_self_healing_test_runs`) and is readable at
`GET /api/self-healing/test-runs`. The CLI is
`python -m app.self_healing.autotest --suite focused|full --once
[--loop SECONDS] [--pg-bin] [--timeout-seconds]` (exit 0 passed, 1 failed,
2 misconfigured). The focused suite is the 5 self-healing test files; the
full suite is the whole `tests/` tree and is meant for nightly runs only.
The real suite is never run inside a test (stub runners only). 19 tests
passed.

### Slice 6 — Watchdog integration
`app/self_healing/watchdog.py` turns the Windows watchdog from blind
restarts into recorded decisions. `decide_watchdog` is pure: API not
listening → `restart_api` + a critical `api_down` incident; API port open
but `/health` silent → `escalate_only` (the process is wedged — never
kill it); worker heartbeat stale and no local worker process →
`restart_worker` + a `worker_stalled` incident; heartbeat stale but the
process exists → `escalate_only`; otherwise `ok`. A fresh heartbeat with
no local process is fine — workers may run on another machine sharing the
database. `run_watchdog_check` probes `/health`, reads the heartbeat from
the database, and records the incident + escalation + audit event. If the
database is down, the incident goes to the out-of-band file instead and
the check still returns a decision — a watchdog that crashes on a dead
database would be useless exactly when it is needed most. The module
never starts, stops or kills a process; the scheduled task performs the
restart. `scripts/ops/windows/watchdog.ps1` now asks the CLI
(`python -m app.self_healing.watchdog`) first and falls back to its old
blind checks, with a log line, when the CLI is unavailable. The PowerShell
syntax was NOT tested on this Linux VM — validate it once on the Windows
host before relying on it. Real bug fixed: `escalate_out_of_band` takes a
report dict and a keyword-only path; the first version called it
positionally and the database-down test caught the `TypeError`.
`build_escalation` also lives in `escalation.py`, not `detector.py`.
17 tests passed.

### Slice 7 — Recovery/continuity hookup
`app/self_healing/recovery_hook.py` files a self-healing incident as a
recovery event through the core recovery service: the caller's recovery
workspace is resolved (404 for non-members, 403 for observers — the
recovery service's own gates), the first enabled target in the tenant is
used, and the event is filed with key `self-healing:{incident.key}`,
source/category mapped from component/kind, condition `failure`, and
scrubbed evidence. The recovery service's threshold, duplicate and
incident rules apply unchanged; a tenant with no enabled target reports
`no_target` and writes nothing. `continuity_options` lists the caller's
continuity workspaces that have an approved or in-review plan, as
candidates a human may activate — it never calls `activate` or
`decide_activation`, and the hook never calls `propose` or `decide`:
playbook execution stays approval-gated in the recovery UI. Two admin
endpoints: `POST /api/self-healing/recovery/file-event` and
`GET /api/self-healing/continuity-options`. Honest boundaries: Step G
drills are not wired (no drill import exists on this branch) and Step AH
does not exist here (no `app/resilience`); both arrive with their own
merges. 12 tests passed.

### Slice 8 — Frontend + gates (this slice)
The admin page `Self-healing` (`frontend/src/self-healing/`) shows open
incidents, unacknowledged escalations, latest probes, self-test runs and
continuity options. It is read-only except for two explicit human acts:
acknowledging an escalation and filing a recovery event (which asks for
the workspace first). A 403/404 shows a muted note — the page needs the
`self_healing.read` grant — instead of an error. The page is wired into
the Administration sidebar and the admin view guard. Pure helpers live in
`selfHealing.mjs` with a `.d.mts` type file and 6 `node --test` tests.
Frontend: 28 tests passed (22 prior + 6 new), `tsc --noEmit` clean,
`vite build` exit 0 (built in 3m 1s; the page ships as its own chunk).
Backend: 109 Step W tests passed across the 7 test files
(27 + 10 + 9 + 15 + 19 + 17 + 12). Alembic: upgrade → downgrade → upgrade
roundtrip green on a fresh throwaway database, single head
`0106_self_healing_test_runs`, `alembic check` clean.

## Bugs found and fixed in slices 4–8
- Two slice-4 test bugs: the audit table column is `action`, not `event`;
  and the scrubber only redacts `key=value` forms, so the test's bare
  `token abc123` was never a case the scrubber claims to catch (the test
  now uses `api_key=abc123`; the scrubber regex was deliberately NOT
  widened — it would over-redact ordinary words).
- Slice-6 `escalate_out_of_band` call-signature bug (above) + the
  `build_escalation` import location (above).
- Slice-7 test-seeding bugs (not product bugs): raw-SQL seeds need
  explicit ids (Python-side `uuid` defaults do not apply to raw SQL);
  the file-event routes are admin-only, so a non-admin stranger answers
  403, not 404; `TestClient.post` needs the `json=` keyword.
- History fix (no product change): the slice-7 route additions were first
  committed inside the slice-5 commit, which left that commit importing a
  module that did not exist yet. The branch was rebased before any push so
  each commit now holds exactly its own slice; the final tree is
  byte-identical to the pre-rebase tree (verified by tree hash).

## What is still owed (not hidden)
- Full backend suite on a quiet VM before merge (load control: the Step E
  verification pass owns the machine while slices 4–8 were built; only
  focused suites ran here).
- `watchdog.ps1` syntax validation on the Windows host (see slice 6).
- `restart_worker` / `switch_fallback` still have no verified remedy
  handler (no supervisor API / configured fallback target exists) — the
  executor escalates them.
- WhatsApp/email escalation delivery (Step F / integrations).
- The auto-test loop and the watchdog check have CLIs but no scheduled
  task wired to them yet — the owner (or the deploy step) picks the
  schedule; the focused suite is the safe default, the full suite is
  nightly-only.
- Step G drill and Step AH resilience hookups arrive with their merges
  (see slice 7 boundaries).

## Merge notes
- Migrations minted here, in chain order: `0104_self_healing_probes`
  (slice 2) → `0105_self_healing_escalations` (slice 4) →
  `0106_self_healing_test_runs` (slice 5). **All three numbers collide at
  merge** (`0104` is also claimed by HR, WhatsApp AE, AI freshness, AB,
  G, AA, AM, Z, R, AF, AL, O, J, F and AG; `0105` by AK, AI, AB, AA and G;
  `0106` by K, M, G and L) — renumber into one linear chain at merge and
  record the renumber map.
- `app/core/permission_catalog.py` is CRLF — preserved, not reformatted.
- New permissions: `self_healing.remedy` (slice 3) and
  `self_healing.read` (slice 4). Both are explicit grants: NOT department
  capabilities, NOT in the legacy admin bridge.
- Touched shared files: `app/main.py` (router registration),
  `frontend/src/components/AdminConsole.tsx` (view + guard + lazy page),
  `frontend/src/components/AppSidebar.tsx` (Administration entry),
  `scripts/ops/windows/watchdog.ps1` (CLI-first with blind fallback).
  `ModuleWorkspaces.tsx` was NOT touched.
- The copied `frontend/node_modules/` (169 MB, from the
  frontend-redesign worktree — byte-identical `package-lock.json`) is a
  local build aid, never committed.

## Owner decisions still open
- Thresholds (implemented defaults, plan §6): 5-minute heartbeat,
  5% error rate, 10-minute stale lock, 3 attempts, 5-minute cooldown.
  Confirm or change them.
- Escalation destination: admin UI is built (slice 4). WhatsApp/email
  delivery depends on Step F / integrations — no channel was invented.
- Schedule for the auto-test loop and the watchdog check (see "owed").
- Who approves a supplier switch / data restore is out of scope here —
  the hook never performs one.
