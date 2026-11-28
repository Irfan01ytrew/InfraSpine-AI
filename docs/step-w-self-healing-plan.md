# Step W — Self-Healing + Auto-Tests: implementation plan

Prepared by Agent 29 (self-healing), branch `agent/self-healing`, base `step-e-gaps-wip`
`ca956c2`. Status: slices 1–3 done (Agent 149). Slices 4–8 remain for the full Step W build.

## 1. What Step W is

The software watches its own health, classifies faults from health signals, applies
only **safe, reversible, idempotent** remedies on its own, runs its own test suite on
a schedule, and **escalates to the owner with a plain-language report** whenever a
fault is unsafe to fix automatically, keeps failing, or is unknown.

Hard rule, from the handover §5: self-healing must **never** change production data,
grant permissions, change payees, or move money. A remedy that touches data,
permissions, approvals or payments is not a remedy — it is an escalation.

## 2. What already exists (not rewritten)

| Area | Existing code | Gap for Step W |
|---|---|---|
| Liveness/readiness | `GET /health` (no DB calls), `GET /health/ready` (DB + ingestion workers) in `app/api/routes/health.py` | No per-component probe history, no thresholds, no fault classification |
| Operational health | `app/services/operational_health.py` (LLM spend, quotas, usage dashboard) | Cost/quota only; no fault or error-rate tracking |
| Document worker | `app/worker.py` (claims jobs, `--once`, `--check`, schema check) | No stall detection, no self-restart, no missed-beat alarm |
| Recovery worker | `app/recovery/worker.py` + `app/recovery/policy.py` (leased claims, fencing, bounded retries, approved commands, action digests, secret redaction) | Recovery is incident-driven and approval-gated; it is **not hooked to live workflow/agent/integration failures** and playbooks do not auto-start |
| Continuity | `app/continuity/*` (graph, scenarios, plans, activation) | Activation is its own code path, not the unified workflow engine; no backup/restore code in `app/` at all (Agent 16 finding) |
| Windows watchdog | `scripts/ops/windows/watchdog.ps1` (every 5 min: restart missing worker/API task) | Process-level only; cannot see API errors, DB stalls, ingestion backlog, or error-rate spikes; restarts without a remedy record |
| Error handling | `app/core/errors.py` (consistent JSON errors, `AppError` hierarchy) | Errors are returned, not counted or classified |
| Test harness | `scripts/irfanix_postgres_tests.py`, `tests/conftest.py` (throwaway `*_test` DB), `scripts/irfanix_cycle.py status` | No scheduled self-test run, no suite-result history, no auto-triage of failures |

## 3. Fault taxonomy (slice 1 implements this)

Signals in: one probe per component (`api`, `database`, `workflow_worker`,
`ingestion_worker`, `vector_index`, `backup`) with status `ok` / `degraded` /
`down` / `unknown` and age in seconds; error counts per window; lock ages;
worker heartbeat ages.

| Incident kind | Trigger | Severity | Allowed remedy (auto) |
|---|---|---|---|
| `worker_stalled` | worker heartbeat older than threshold (default 5 min) | high | `restart_worker` (bounded, cooldown) |
| `api_down` | API probe `down` | critical | none — escalate |
| `database_unreachable` | DB probe `down` | critical | none — escalate |
| `ingestion_backlog` | queued jobs growing / workers `degraded` | medium | `requeue_job` (idempotent claim) |
| `error_rate_spike` | error rate above threshold (default 5% over 100+ requests) | high | `switch_fallback` (provider/model fallback only) |
| `stale_lock` | lock age above max (default 10 min) | medium | `clear_stale_lock` (lock only, never data) |
| `transient_failure` | single retryable error (timeout, 503) | low | `retry_once` |
| `unknown` | anything unclassified | high | none — escalate |

## 4. Safety rules (enforced in code, not just in prose)

1. **Allow-list only.** A remedy must be in `SAFE_REMEDIES`. Anything else escalates.
2. **Severity gate.** `critical` incidents always escalate, even when a remedy exists.
3. **Bounded attempts.** At most 3 auto-remedy attempts per incident key; the 4th
   decision is always `escalate`. No infinite retry loops.
4. **Cooldown.** The same incident key cannot be remedied again within 5 minutes
   (default) — no restart storms.
5. **No data, no permissions, no money.** Remedies may only restart a process,
   clear a lock, retry a transient call, requeue a job, or switch to a configured
   fallback. They never write business data, change roles, approve anything, or pay.
6. **Every decision is recorded.** Incident key, kind, severity, decision, remedy,
   attempt count, and timestamp go to the audit log (wiring slice); a remedy with no
   record is a bug.
7. **Escalation reports are plain language and secret-free.** Evidence is scrubbed
   (passwords, tokens, keys, DSNs) before it reaches the owner report.
8. **Auto-tests run on throwaway databases only**, with log capture on, exactly like
   the normal suite. A failing self-test never triggers a remedy — it escalates.

## 5. Build order (slices)

1. **Fault detector (DONE, this agent):** `app/self_healing/detector.py` — pure
   stdlib functions: classify signals → incident, decide remedy vs escalate with
   attempts/cooldown guards, build a scrubbed escalation report.
   Tests: `tests/test_self_healing_detector.py` (22 tests, all passing).
2. **Signal collection (DONE, Agent 149):** `app/self_healing/collector.py` —
   reads `/health` liveness (caller-supplied), DB `SELECT 1`, worker heartbeats
   (`IngestionWorkerHeartbeat`), ingestion queue depth, expired workflow-step /
   document leases, and caller-supplied error counts into detector probes;
   stores probe snapshots + upserts the incident row (`self_healing_probes`,
   `self_healing_incidents`, migration `0107_self_healing_probes`). The backup
   probe is always `unknown` (no backup code exists in `app/`). If the database
   itself is down, the collector returns the probes without further reads —
   storing is impossible then, so that escalation must travel outside the DB.
   Fixed a real slice-1 bug the collector exposed: an `unknown` workflow probe
   shadowed a `degraded` ingestion probe, so no `ingestion_backlog` was ever
   classified when both were present (detector now prefers a degraded probe).
   Tests: `tests/test_self_healing_collector.py` (10 tests) + 1 detector
   regression test; slice-1 total now 27.
3. **Remedy executor (DONE, Agent 149):** `app/self_healing/executor.py` —
   runs the detector decision through `execute_once` (duty
   `self_healing.remedy`, new permission in the catalog, deliberately NOT a
   department capability and NOT in the legacy admin bridge) with read-back
   verification; records every attempt on the incident row + audit log.
   Verified handlers ship for `clear_stale_lock` (release an expired
   workflow-step lease) and `requeue_job` (return a stuck ingestion document
   to the queue, mirroring the worker's `_release`); `retry_once` runs a
   caller-supplied operation once. `restart_worker` and `switch_fallback`
   have NO verified handler yet (no supervisor API / configured fallback
   target exists) — the executor escalates them instead of pretending.
   Tests: `tests/test_self_healing_executor.py` (9 tests).
4. **Escalation channel:** owner report delivery (admin UI + notification), with the
   scrubbed report from slice 1.
5. **Auto-test loop:** scheduled suite run (focused suite on a schedule, full suite
   nightly) on a throwaway DB; failures auto-triaged into incidents via slice 1.
6. **Watchdog integration:** replace/augment `watchdog.ps1` process checks with
   detector decisions so restarts are recorded remedies, not blind restarts.
7. **Recovery/continuity hookup:** detector incidents feed `app/recovery` playbooks
   (approval-gated for anything above low risk) and Step G continuity activation.
8. **Frontend + docs:** self-healing dashboard (incidents, remedies, escalations),
   `CHANGES-step-W.md`, full suite + Alembic + frontend gates, then merge to main.

## 6. What Step W still needs from the owner

- Confirm the thresholds (defaults above: 5-min heartbeat, 5% error rate, 10-min
  stale lock, 3 attempts, 5-min cooldown) or set different ones.
- Confirm where escalation reports should arrive (admin UI is built in slice 4;
  WhatsApp/email delivery depends on Step F / integrations).
- Nothing else: no credentials, no hardware, no live data access.
