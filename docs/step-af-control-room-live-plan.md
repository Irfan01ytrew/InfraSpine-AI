# Step AF — Control Room live dashboard + WhatsApp alerts: plan

> Prepared by Agent 39 (control-room-live), branch `agent/control-room-live`,
> HEAD `ca956c2` (step-e-gaps-wip).
> Status: **slices 1–5 done** (see §6); slice 6 (docs + gate + merge) in progress.
> Owner-approved 2026-11-28: "Live dashboard charts ke saath, aur zaroori alert
> seedha aapke WhatsApp par."

## 1. What Step AF is (and how it differs from Steps Q and AA)

- **Step Q (Agent 22)** — the control room's *status view*: read-only overview
  API (run counts, agent registry, blockers, worker flag). Slice 1 done on
  branch `agent/control-room`, **not merged** into `step-e-gaps-wip` yet.
- **Step AA (Agent 33)** — *scheduled* reports: the clock fires (daily cash
  summary, weekly invoices), the owner chose the template in advance.
- **Step AF (this step)** — *live* dashboard + *event-driven* alerts: charts
  that refresh from live data, and alerts that fire the moment a metric
  crosses a threshold (worker down, failed payment, exceptions piling up).
  AA is "tell me every morning"; AF is "tell me the second something breaks".

Shared with AA by design (same rules, no duplication of behaviour): quiet
hours (default 22:00–07:00 Asia/Karachi), per-rule cooldown/dedupe so the
owner is never spammed, WhatsApp first via Step F channels with email as
second channel, in-app as always-on fallback. Alerts are **read-only
notifications — they never execute an action**, never approve anything, never
change a payee or an invoice. Any link in an alert opens the normal screen;
the action itself still needs the exact-action approval flow (§5 rule 4).

## 2. What already exists (mapped 2026-11-28, base `ca956c2`)

| Need | Existing surface | Gap |
|---|---|---|
| Run/agent status | Step Q slice 1 (`app/control_room/service.py` + `GET /api/control-room/scopes/{scope_id}/overview`) — **only on branch `agent/control-room`, not in this base** | AF must not re-implement it; AF consumes the Q overview once Q merges. Merge note: Q creates `app/control_room/service.py`, AF creates `app/control_room/alerts.py` in the same new package — different files, but both branches may add `app/control_room/__init__.py`; reconcile at merge time (keep one, empty). |
| Health signals | `/health` (liveness, no DB) and `/health/ready` (`app/api/routes/health.py`: DB + ingestion workers only) | No workflow-engine worker state, no error-rate tracking, no alert history |
| Worker state | `app/worker.py --check` (Gap 5, `workflow_engine_on`, default on); `app/services/operational_health.py` (LLM spend/quota dashboard only) | No stall detection, no tick age exposed to an API |
| Finance chart data | `FinanceService.spend(months=12)` (monthly sums by department/currency, permission-filtered via `readable_groups()`), `summary()`, `exceptions()` (`app/finance/service.py`) | No control-room chart aggregation; no per-day series for a live chart |
| Alert schema pattern | `QuotaAlert` (`app/schemas/reports.py`: threshold %, severity) | No alert-rule model, no evaluator, no delivery log |
| WhatsApp outbound | `app/integrations/whatsapp.py` — Meta Cloud API `send_text` / `send_text_once` (exactly-once via dispatch key); Baileys path via `app/services/agent_studio_whatsapp.py` (Step F, Agent 13, in prep) | No alert sender yet; **live Cloud send needs Meta WhatsApp credentials from the owner** (Step F blocker, unchanged) |
| Email transport | SMTP, verified TLS (`app/services/personal_task_email.py`) | Not wired to alerts yet |
| Dispatch pattern | Reminder workers (`app/services/approval_reminders.py` etc.): poll loop, SKIP LOCKED claims, escalation, max reminders; outbox pattern (`app/services/outbox.py`) | Reuse the pattern for alert dispatch (slice 4); do not invent a new one |
| Alert rules / evaluator / history | **Nothing** | This step (slice 1 = evaluator, pure functions) |

## 3. Authorization model

- Dashboard reads go through the same scope gate as Step Q: `require_scope`
  (`app/workflows/security.py` → `app/platform`, permission
  `workflow.access`); non-member/unknown scope → 404, member without
  permission → 403. Finance chart data is permission-filtered by
  `FinanceService` (`finance.read`, group-scoped) — the dashboard never widens
  visibility and never shows amounts for groups the viewer cannot read.
- Alert *configuration* (thresholds, recipients, quiet hours) is an admin
  action: `platform.manage` on the org scope, audited. Alert *delivery*
  lists contain only the owner's (and explicitly added managers') numbers;
  no phone number is ever taken from document or message text.
- Alert text is untrusted data to any downstream consumer: it can never
  grant permissions, change payees, or change what an approval covers. No
  secrets in alert bodies or logs (no account numbers, no tokens, no keys).
- Every send is audited (`control_room.alert_sent`: rule key, severity,
  channel, recipient, metric value) and visible in admin reports.

## 4. Live dashboard (slices 2–3)

Charts (all from existing permission-filtered queries, no new tables for v1):
1. **Spend by month** — `FinanceService.spend()` rows, 12-month series, per
   department/currency (never converted — no invented exchange rates).
2. **Invoices by status** — counts from `summary()`/`exceptions()`:
   pending / verified / exception / approved / paid, plus exceptions backlog.
3. **Runs & agents** — Step Q overview: runs by status/origin, agent registry
   with latest run, blockers first.
4. **Health strip** — worker on/off + last tick age, ingestion workers online,
   LLM quota % (from `operational_health`).

Frontend: control-room page (Agent 18 redesign owns the design system),
polls the overview + chart endpoints, blockers and critical alerts pinned
at the top. `npm test` + build + typecheck clean.

## 5. Alert rules (slice 1 — implemented)

Rules are data, not code: each rule is a `key`, `severity`
(`critical` | `warning`), title/detail templates, a numeric `threshold`, a
`cooldown_seconds` (dedupe window, default 1 h), and `breaks_quiet_hours`
(default `False` — only the owner enables it per rule, same conservative
rule as Step AA). The evaluator (`app/control_room/alerts.py`, pure stdlib,
no I/O/DB/network) turns a `MetricsSnapshot` into the alerts to send now:

| Rule key | Fires when | Severity |
|---|---|---|
| `worker_down` | engine off, or no tick for > 300 s | critical |
| `worker_stalled` | runs queued and no tick for > 120 s | warning |
| `failed_runs_spike` | ≥ 5 failed runs in the last hour | warning |
| `failed_runs_critical` | ≥ 20 failed runs in the last hour | critical |
| `failed_payment` | ≥ 1 failed payment in the last hour | critical |
| `exceptions_backlog` | ≥ 10 finance exceptions open | warning |
| `exceptions_critical` | ≥ 50 finance exceptions open | critical |
| `approval_stuck` | oldest unapproved payment > 24 h | warning |
| `approval_stuck_critical` | oldest unapproved payment > 72 h | critical |
| `ingestion_down` | 0 ingestion workers online | warning |
| `ingestion_failures` | ≥ 5 failed ingestions in the last hour | warning |
| `llm_quota` | LLM quota ≥ 80% | warning |
| `llm_quota_critical` | LLM quota ≥ 95% | critical |

Evaluator rules (enforced, tested):
- A missing (`None`) or invalid (negative) metric skips that rule — never a
  crash, never a false alert.
- Cooldown: a rule that already fired within its window does not fire again.
- Quiet hours (default 22:00–07:00 Asia/Karachi, overnight window handled):
  non-breaking alerts wait; breaking alerts still fire.
- Output is critical-first, stable rule order inside each severity.
- `validate_rules()` rejects duplicate keys, bad severities, non-positive
  cooldowns, and unknown rule keys (a rule with no check function).

## 6. Slices 1–5 — done

Slice 1 — evaluator (Agent 39, commits `3284db4` + `97a88e1`):
- `app/control_room/alerts.py` — `MetricsSnapshot`, `AlertRule`,
  `DEFAULT_RULES`, `evaluate_alerts()`, `is_quiet_hour()`,
  `format_alert_message()`, `alert_summary()`, `validate_rules()`.
- `app/control_room/__init__.py` — empty package marker.
- `tests/test_control_room_alerts.py` — see §7.

Slice 2 — chart + health API (Agent 50, commit `b681fd9`):
- `app/control_room/charts.py` — `build_charts()` (spend series, invoices
  by status, runs, health strip); `GET /api/control-room/scopes/{scope_id}/charts`
  via `require_scope`, finance sections gated by `finance.read`.
- `tests/test_control_room_charts.py` — 5 tests.

Slice 3 — frontend dashboard (Agent 50, commit `49516ab`):
- `frontend/src/control-room/` — `ControlRoomPage.tsx`, `api.ts`,
  `chartData.mjs`; 30 s polling, active alerts pinned first, health strip.
  Wired into `AdminConsole.tsx` + `AppSidebar.tsx`.

Slice 4 — alert dispatch worker (Agent 91, commit `339b4ea`):
- `app/db/control_room_models.py` — `ControlRoomAlertDelivery`
  (pending/sending/sent/failed/skipped) + `ControlRoomAlertConfig`;
  migration 0111_control_room_alerts (down_revision `0103`).
- `app/control_room/dispatch.py` — `collect_snapshot()` reads live
  signals (failed runs/payments, exceptions, stuck approvals, failed
  ingestions); `queue_alerts()` dedupes on the delivery log (survives
  restarts); `send_pending()` claims with SKIP LOCKED and audits
  `control_room.alert_sent`; `DefaultAlertSender` sends WhatsApp first
  (Step F transport), email second, in-app fallback; `process_control_room_alerts()`
  + `run_alert_dispatch()` wired into `app/worker.py` (`--once` and the
  5-minute background loop).
- `tests/test_control_room_dispatch.py` — see §7.

Slice 5 — alert configuration API + admin UI (Agent 91, commit `a787780`
+ slice-5 frontend):
- `app/control_room/config.py` — pure validation/merging
  (`validate_config_payload`, `effective_rules`, `effective_rules_list`,
  `rule_channels`, `config_defaults`, `config_to_dict`).
- `app/api/routes/control_room.py` — `GET`/`PUT /scopes/{scope_id}/config`
  (admin + `platform.manage`, audited as
  `control_room.alert_config_changed`, 422 on invalid payload) and
  `GET /scopes/{scope_id}/deliveries` (scope members only, limit 1–200).
- `frontend/src/control-room/` — config panel (enable/disable, quiet
  hours, recipients, per-rule threshold/cooldown/enabled/channels,
  overrides-only payload via `controlRoomConfig.mjs`) + delivery history.
- `tests/test_control_room_config.py` + `frontend/test/controlRoomConfig.test.mjs` — see §7.

## 7. Test list

Slice 1 (implemented):
- `worker_down` fires when the engine is off and when the tick is stale;
  not when healthy.
- `worker_stalled` fires only when runs are queued *and* the tick is stale.
- `failed_payment` fires at ≥ 1, severity critical.
- `exceptions_backlog` / `approval_stuck` / `llm_quota` fire at their
  warning thresholds and again at their critical thresholds.
- `ingestion_down` fires at 0 workers; a missing metric skips the rule.
- Cooldown suppresses a repeat inside the window and allows it after.
- Quiet hours suppress a warning at 23:30 and 06:59, allow it at 07:00 and
  12:00; a breaking critical still fires at 23:30.
- Negative / missing metrics never crash and never alert.
- `validate_rules` rejects duplicate keys, bad severity, unknown keys.
- Output is critical-first; `format_alert_message` and `alert_summary`
  behave.

Still to test (later slices) — all covered by slices 4–5 tests:
- chart endpoints return permission-filtered series; a non-member gets 404.
  (slice 2, `tests/test_control_room_charts.py`)
- alert dispatch claims a rule once (SKIP LOCKED), sends via mocked
  WhatsApp, audits `control_room.alert_sent`, and dedupes on retry.
  (slice 4, `tests/test_control_room_dispatch.py`)
- quiet-hours + cooldown hold across worker restarts (delivery log in DB).
  (slice 4, `tests/test_control_room_dispatch.py`)
- frontend: dashboard renders charts, polls, pins critical alerts first;
  `npm test` + build + typecheck clean. (slices 3 + 5)

## 8. What remains for Step AF

1. **Slice 2 — chart + health API.** DONE (Agent 50, `b681fd9`).
2. **Slice 3 — frontend dashboard.** DONE (Agent 50, `49516ab`).
3. **Slice 4 — alert dispatch.** DONE (Agent 91, `339b4ea`).
4. **Slice 5 — alert configuration API.** DONE (Agent 91, `a787780` + frontend).
5. **Slice 6 — docs + merge.** IN PROGRESS (Agent 91, then 131, then
   145): `assessment/CHANGES-step-AF.md` (plain language, file list,
   test counts), full suite green, Alembic roundtrip + check clean,
   frontend green, merge to `main` only when green. Agent 131's
   finishing work (7 real bug fixes + 1 test fix, see CHANGES §"Bugs
   found") verified: combined dispatch+config+charts 28 passed,
   frontend 31 passed; Agent 145 re-verified alerts 41 + config 14
   and filled the Alembic `check` (clean). Still owed before merge:
   Alembic roundtrip on a scratch DB + the full backend suite, both
   deferred by Master load-control while the Step E fifth pass runs
   (VM load ~35).

**Owner inputs needed:** Meta WhatsApp Cloud API credentials (already
needed for Step F — same credentials, no new ask); confirm the default
thresholds above; confirm who else besides the owner receives critical
alerts; confirm whether any rule may break quiet hours.
