# CHANGES — Step AF: Control Room live dashboard + WhatsApp alerts

Step AF adds a live control room to InfraSpine: one page per department that
watches the workflow engine, finance and document ingestion, and sends an
alert the moment something crosses a threshold — WhatsApp first, then email,
in-app when no recipient is configured. It never acts: it watches, warns and
records. It does not approve anything, change a payee, pay an invoice or
start a workflow.

Event-driven alerts are this step (AF). Schedule-driven reports are Step AA
(a different step). The run/agent status overview is Step Q; AF reads the
same live data but does not re-implement Q.

## What was added

### 1. Alert rules — the evaluator (slice 1, Agent 39)
- 13 rules over live signals: workflow worker down/stalled, failed runs
  spiking, many runs failing, payment failed, finance exceptions piling up
  (warning) and critical, payment approval stuck (24 h warning, days
  critical), document ingestion down/failing, LLM quota nearly used up and
  almost exhausted.
- Quiet hours 22:00–07:00 PKT: warnings wait until morning; only the two
  breaking critical rules (`worker_down`, `failed_payment`) are sent at
  night.
- Cooldown per rule (default 1 hour): the same alert is not sent twice
  inside its window.
- Missing or negative readings skip the rule — never a crash, never a
  false alert. Alert text carries only metric values and thresholds, never
  account numbers, tokens or secrets.

### 2. Chart + health API (slice 2, Agent 50)
- `GET /api/control-room/scopes/{scope_id}/charts`: spend by month,
  invoices by status, runs (counts + recent failures), health strip
  (worker on/off, last run activity, ingestion workers online, highest
  department LLM quota use), plus the alerts that would fire right now.
- A scope member without a finance role sees no money: spend and invoice
  sections come back empty, runs and health still visible. A non-member
  gets 404; a member without `workflow.access` gets 403.

### 3. Frontend dashboard (slice 3, Agent 50)
- Control-room page: department picker, active alerts pinned first, health
  strip, spend bar chart, invoices-by-status bars, runs card with recent
  failures. Refreshes every 30 seconds.

### 4. Alert dispatch worker (slice 4, Agent 91)
- Every 5 minutes the worker builds a live snapshot (failed runs and
  payments in the last hour, exception count, oldest awaiting-approval age,
  failed ingestions, worker tick age), evaluates the rules and sends what
  fired.
- WhatsApp first (Step F transport), email second, in-app when a scope has
  no recipients. A failed send is recorded as failed and retried only after
  the rule cooldown — never in a tight loop.
- Dedupe survives restarts: every alert is a row in
  `control_room_alert_deliveries`, so a second pass (or a restarted worker)
  never re-sends inside the cooldown. Claims use SKIP LOCKED so two workers
  never send the same alert twice.
- Every send is audited as `control_room.alert_sent`. A disabled config
  sends nothing.
- New database migration 0111_control_room_alerts: tables
  `control_room_alert_deliveries` and `control_room_alert_configs`.

### 5. Alert configuration API + admin UI (slice 5, Agent 91)
- `GET` / `PUT /api/control-room/scopes/{scope_id}/config` (admin +
  `platform.manage`, audited as `control_room.alert_config_changed`):
  enable/disable, quiet hours, recipients (WhatsApp E.164 numbers, email
  addresses — at most 10), per-rule threshold / cooldown / enabled /
  quiet-hours override / channels.
- `GET /api/control-room/scopes/{scope_id}/deliveries`: delivery history
  for scope members (newest first, limit 1–200).
- Invalid payloads are rejected with 422 and nothing is stored: unknown
  rule keys, negative thresholds, non-positive cooldowns, unknown channels
  or override fields, malformed recipients, unreadable quiet-hour times.
  Severity is not configurable.
- The control-room page now has an alert-settings card (managers only)
  and a delivery-history card (all members). The settings form only sends
  overrides for rules whose values differ from the default, so an untouched
  rule can return to its default.

## Files changed (branch `agent/control-room-live`, base `step-e-gaps-wip`)

New:
- `app/control_room/alerts.py` — rule evaluator (slice 1).
- `app/control_room/charts.py` — chart + health builder (slice 2).
- `app/control_room/dispatch.py` — snapshot collector, queue/send, worker
  loop (slice 4).
- `app/control_room/config.py` — config validation + rule merging (slice 5).
- `app/db/control_room_models.py` — `ControlRoomAlertDelivery`,
  `ControlRoomAlertConfig`.
- `migrations/versions/0111_control_room_alerts.py` — creates both tables.
- `tests/test_control_room_alerts.py`, `tests/test_control_room_charts.py`,
  `tests/test_control_room_dispatch.py`, `tests/test_control_room_config.py`.
- `frontend/src/control-room/ControlRoomPage.tsx`,
  `frontend/src/control-room/api.ts`,
  `frontend/src/control-room/chartData.mjs`,
  `frontend/src/control-room/chartData.d.mts` (type declarations for the
  chart helpers),
  `frontend/src/control-room/controlRoomConfig.mjs`.
- `frontend/test/controlRoomCharts.test.mjs`,
  `frontend/test/controlRoomConfig.test.mjs`.
- `docs/step-af-control-room-live-plan.md`.

Changed:
- `app/control_room/__init__.py` — empty package marker (also created by
  Step Q's branch; reconcile to one empty file at merge).
- `app/api/routes/control_room.py` — charts endpoint (slice 2) + config and
  deliveries endpoints (slice 5).
- `app/finance/service.py` — `status_counts()` for the invoices-by-status
  chart (slice 2).
- `app/main.py` — registers the control-room router.
- `app/worker.py` — alert dispatch in `--once` and in the 5-minute
  background loop (slice 4).
- `migrations/env.py` — import the control-room models so Alembic sees the
  tables.
- `frontend/src/components/AdminConsole.tsx`,
  `frontend/src/components/AppSidebar.tsx` — control-room navigation
  (slice 3).

## Tests

New tests, all passing in dedicated runs on throwaway `*_test` databases
(cluster port 55439, `infraspine_af_test`):
- Slice 1 evaluator: **41 passed** (38 original + 2 UTC quiet-hours
  regressions + 1 threshold-override regression, Agent 131).
- Slice 2 charts API: **5 passed**.
- Slice 4 dispatch worker: **9 passed** (live-signal snapshot, send-once
  + audit, cooldown survives a second pass, quiet hours hold warnings but
  not breaking criticals, in-app fallback, failed-send retry after
  cooldown, disabled config, SKIP LOCKED claim, per-rule threshold
  override).
- Slice 5 config API + pure helpers: **14 passed** (defaults, roundtrip,
  every validation rejection leaves nothing stored, non-admin 403, unknown
  scope 404, audit row, deliveries visible to members only, limit bounds,
  effective-rule merging).
- Slices 4+5+2 combined re-run after the Agent 131 fixes:
  **28 passed in 953.50s**, 0 failed
  (`tests/test_control_room_dispatch.py tests/test_control_room_config.py
  tests/test_control_room_charts.py`, log
  `/home/hatch/tmp-agent131/pytest-af131-rerun.log`).
- Slice 5 frontend helpers: **5 passed**.
- Frontend suite: **31 passed, 0 failed** (26 before slice 5 + 5 new);
  typecheck clean; build clean.

Full-suite gate (slice 6): the full backend suite was NOT run on this
branch — the Step E verification pass owns the VM (fifth pass, PID
19118, chunk 0 running) and two full-suite passes at once OOM-kill
each other. Focused suites above (69 backend AF tests) are green; the
full-suite run is owed on a quiet VM, after Step E merges. Agent 145
independently re-ran slice 1 (41 passed, `--noconftest`) and slice 5
config (14 passed in 1062.92s) on this tree; dispatch + charts re-runs
were deferred by Master load-control (VM load ~35, free RAM ~150 MB)
— Agent 131's combined 28-passed run above stands as the
dispatch/charts evidence. Alembic `check` on the migrated test DB:
"No new upgrade operations detected." Alembic roundtrip
(upgrade→downgrade→upgrade) is still owed — it needs a scratch DB and
was deferred with the other heavy runs. Frontend gates: 31 passed, 0
failed (Agent 131's `npm-test-af131.log`); the Agent 131/145 fixes
touched no frontend file, so no frontend re-run was needed.

## Bugs found while finishing slices 4–6 (Agent 131)

The slices 4–5 code was written by Agent 91 and interrupted before its
tests were run. Running them exposed seven real product bugs and one test
bug; all are fixed on this branch:

1. **Quiet hours never fired for UTC datetimes (real).**
   `is_quiet_hour` read an aware datetime's wall clock as-is, so the
   `now` the dispatcher passes (UTC) was never quiet at 23:30 PKT.
   Fixed in `app/control_room/alerts.py`: aware datetimes are converted
   to Asia/Karachi first. Two regression tests added
   (`test_utc_datetime_is_converted_to_org_time`,
   `test_evaluate_alerts_suppresses_warning_for_utc_night`).
2. **Per-rule threshold overrides were ignored (real).** The check
   functions hardcoded the default thresholds (`failed >= 5`, `age > 24`,
   …), so an admin raising a threshold changed only the message text
   ("limit 10") while the rule still fired at the default. Fixed: every
   check now takes the rule's threshold and compares against it, with
   each rule's operator preserved (`>=` for counts/quotas, `>` for ages
   and tick staleness, `<=` for ingestion workers). One regression test
   added (`test_rule_threshold_override_changes_the_firing_decision`).
3. **A failed send was re-queued as in-app (real).** When a WhatsApp
   send failed, the next dispatch saw the recent failed row, blocked the
   WhatsApp re-queue — and then the in-app fallback fired, because it
   only checked whether an in-app row existed. Fixed in
   `app/control_room/dispatch.py`: `queue_alerts` tracks whether any
   configured channel was cooldown-blocked and skips the fallback then.
   The fallback still fires when no channel is reachable at all.
4. **Config validation accepted bad values (real).** Thresholds of 0,
   negative or float, and quiet start == end, passed validation.
   Fixed in `app/control_room/config.py`: threshold must be a positive
   int; quiet start and end must differ (effective values, defaults
   included); unknown top-level fields are rejected.
5. **Config GET omitted `scope_id` (real).** `_config_response` never
   included the scope. Fixed: it now takes the scope id from the path
   and returns it as a string.
6. **Config PUT on an unknown scope returned 200 and minted a row
   (real).** Fixed in `app/api/routes/control_room.py`: both config
   endpoints check the scope exists (AccessGroup lookup, 404
   `NotFoundError`) after the admin check. Membership is not required —
   `platform.manage` is the gate.
7. **Cooldown compared the wrong clock (real).** The dispatch dedupe
   (`queue_alerts`) compared `created_at` — the database clock —
   against the cooldown cutoff, while the evaluator's cooldown is
   defined on the dispatch clock (`evaluated_at`). When the two clocks
   disagree, a rule could re-fire inside its cooldown or stay blocked
   after it. Fixed in `app/control_room/dispatch.py`: the dedupe
   compares `evaluated_at` (never null — the model requires it and
   every queued row sets it from the alert). `send_pending` also
   short-circuits `in_app` rows instead of routing them through the
   transport sender (the sender already returned `None` for `in_app`,
   so this is hardening, not a behaviour change).
8. **Test bug:** `test_effective_rules_merge_overrides_and_ignore_unknowns`
   passed a bare rules mapping to `effective_rules`, which takes a config
   dict (the shape every production caller passes). The test was fixed
   to pass `{"rules": stored}`; the function was not changed.

## Known issues and merge notes

- **Migration number collision:** this step minted `0104`
  (`0104_control_room_alerts`). The same number was also minted on other
  branches: HR leave/attendance (Step AG), language, AI freshness
  (Step AI), finance autopilot (Step R), AB, continuity (Step G), foundry
  (Step AM) and scheduled reports (Step AA); `0105` was also minted by
  more than one step (AK, AI, AB). At integration it was renumbered to
  `0111_control_room_alerts` (down_revision
  `0110_whatif_rehearsals`) in the linear chain recorded in
  INTEGRATION-SNAPSHOT.md (map v4).
- **Step Q overlap:** Step Q's branch also creates
  `app/control_room/__init__.py` (keep one empty file) and owns the
  overview endpoint. The runs section of the charts API reads `wf_runs`
  directly; switch it to Q's overview once Q lands, or keep the direct
  read if Q's shape differs.
- **`last_tick_age_seconds` is honestly None:** the health snapshot reports
  no worker tick age because no worker heartbeat row is persisted yet
  (same gap as the slice-2 charts). `worker_down` still fires when the
  engine is off.
- **Failed-ingestion signal is org-wide:** documents carry no department,
  so the count is for the whole organisation, not the scope. Documented in
  the code.
- **WhatsApp needs Meta credentials:** the sender reuses the Step F
  transport; until the owner supplies Meta WhatsApp Cloud API credentials
  the WhatsApp leg cannot deliver to a real phone (tests use a recording
  sender). Email needs the SMTP settings already used by personal-task
  mail; without them, alerts fall back to in-app.

## Owner decisions still needed

- Meta WhatsApp Cloud API credentials (same ask as Step F — no new
  credentials needed beyond those).
- Confirm the default thresholds (e.g. 5 failed runs/hour, 10 exceptions,
  24 h stuck approval).
- Who besides the owner receives critical alerts (recipients are
  per-department; none are configured by default).
- Confirm which rules may break quiet hours (today: `worker_down` and
  `failed_payment` only).
