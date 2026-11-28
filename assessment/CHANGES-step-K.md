# CHANGES — Step S / v2 Step K: Company Digital Twin (slices 1–6)

Plain-language changelog for the digital twin: one live model of the
company — people, teams, systems, suppliers, money flow — that answers
"what breaks if X is away?", runs what-if simulations that change
nothing, and shows how fresh the model is.

Base: `step-e-gaps-wip` `ca956c2`. Branch: `agent/digital-twin`.
No migration, no new tables, no live behaviour changed by this step:
the twin is a read-only projection of data the modules already own.

## What it does now

- **One twin snapshot** (`app/digital_twin/snapshot.py`): builds a
  plain-dict model from an authorized company-graph projection +
  continuity nodes/links + finance batches/payments + workflow runs.
  One real-world node is never counted twice (company graph wins on
  conflicts); dangling edges are dropped; a payment's bank account is
  never copied into the twin. Every source reports fresh / stale /
  unknown, and the snapshot carries a fingerprint + "last confirmed"
  timestamp.
- **Live sync feeds** (`app/digital_twin/feeds.py`): five real outbox
  events (`finance.batch_started`, `finance.payment_verified`,
  `finance.payment_failed`, `workflow.step_verified`,
  `workflow.blocked`) update the twin through the governed-change
  path only — pending graph-change proposals, never a direct write,
  never an approval. Unsupported events are ignored, not errors;
  malformed money events raise so the outbox retries instead of
  silently dropping them.
- **"What breaks if X is away?"** (`app/digital_twin/impact.py`):
  person/team absence → at-risk entities, backup coverage (a backup
  counts only when the person is not absent and holds the required
  skills), money at risk per currency, recovery waves in dependency
  order, dependency cycles reported instead of dropped. Deterministic
  for the same twin version.
- **Horizon predictions + watch** (`app/digital_twin/horizon.py`):
  "unavailable N days" predictions with a 0–100 advisory risk score
  and explainable factors; the watch refreshes predictions only when
  the twin fingerprint changes, so an unchanged twin never re-fires.
  Rules as data (severity threshold, named factor, entity kinds);
  quiet hours 22:00–07:00 Asia/Karachi — only critical alerts break
  through; repeats inside cooldown are reported as suppressed, never
  silently dropped.
- **What-if engine + drill comparison** (`app/digital_twin/whatif.py`):
  the three founder questions — key employee leaves, server down,
  supplier fails — plus cyber incident, site unavailable, and
  compound scenarios. States are failed / degraded / exposed (cyber
  only); a target with several providers fails only when all of them
  fail. Recovery options are ranked (activate backup →
  alternate supplier → restore from backup → manual workaround);
  rehearsal mode rewrites the plan as numbered practice-only steps
  (`dry_run: True`, `side_effects: []` by construction). Every run is
  a dry run. `compare_drill` compares a simulation against a real
  Step G recovery drill and lists every difference (affected sets,
  money totals, recovery waves) without re-running anything.
- **Read-only API** (`app/api/routes/digital_twin.py`, registered in
  `app/main.py`): `GET /api/digital-twin/{tenant_id}/overview`,
  `POST /api/digital-twin/{tenant_id}/what-if`,
  `POST /api/digital-twin/{tenant_id}/predict-absence`. Tenant
  membership is enforced (`scope_for` → 404 for unknown tenant or
  non-member, 401 unauthenticated). Nothing here approves, dispatches,
  grants access, or pays.
- **Twin UI** (`frontend/src/digital-twin/`): "Digital twin" in the
  admin sidebar + ModuleWorkspaces. One page per tenant: entity /
  relationship / money-at-risk stats, snapshot freshness ("last
  confirmed …" + fingerprint + per-source fresh/stale badges), live
  company map, what-if panel (5 scenario templates, horizon days,
  rehearsal checkbox, affected list with state/severity/confidence,
  recovery waves, rehearsal steps), absence prediction panel (risk
  score, factors, money at risk). Every label states the twin is
  read-only and simulations are dry runs; restricted users only ever
  see their authorized projection, and counts never leak hidden nodes.

## Safety rules (kept, not new)

- The twin simulates only. It never approves, dispatches, grants
  access, or pays. Money figures are exposure ("money at risk"),
  never payment instructions.
- A `backup_for` link is a recorded assignment, never an access
  grant. Effective-permission gaps are listed, not bridged.
- Finance amounts in the twin stay owner-only until the owner
  decides otherwise (see "Owner decisions" below).
- No secrets in the twin, its snapshots, its logs, or its tests.
  Test data is synthetic (fake people, systems, amounts).

## Files changed

Backend (new):
- `app/digital_twin/impact.py` — absence simulation (slice 1)
- `app/digital_twin/snapshot.py` — snapshot builder (slice 2)
- `app/digital_twin/feeds.py` — live sync feeds (slice 3)
- `app/digital_twin/horizon.py` — horizon predictions + watch (slice 4)
- `app/digital_twin/whatif.py` — what-if engine + drill comparison (slice 5)
- `app/digital_twin/service.py` — read-only service adapters (slices 5–6)
- `app/api/routes/digital_twin.py` — read-only API (slices 5–6)

Backend (edited):
- `app/digital_twin/__init__.py` — public exports
- `app/main.py` — router registration (2 lines)

Frontend (new):
- `frontend/src/digital-twin/TwinWorkspace.tsx` — twin page
- `frontend/src/digital-twin/api.ts` — API client + 5 scenario templates
- `frontend/src/digital-twin/types.ts` — twin types

Frontend (edited):
- `frontend/src/components/AdminConsole.tsx` — `digital-twin` view
- `frontend/src/components/AppSidebar.tsx` — nav entry
- `frontend/src/components/ModuleWorkspaces.tsx` — workspace mount

Tests (new):
- `tests/test_digital_twin_impact.py` (slice 1)
- `tests/test_digital_twin_snapshot.py` (slice 2)
- `tests/test_digital_twin_feeds.py` (slice 3)
- `tests/test_digital_twin_horizon.py` (slice 4)
- `tests/test_digital_twin_whatif.py` (slice 5)
- `tests/test_digital_twin_service.py` (slice 5, incl. zero-side-effect proof)
- `frontend/test/digitalTwin.test.mjs` (slice 6)

Docs:
- `docs/step-s-digital-twin-plan.md` — plan + slice status
- `assessment/CHANGES-step-K.md` — this file

## Test counts

- Slice 1: 22 new tests passed; 16 company-graph/continuity regressions passed.
- Slices 2–3: 46 new tests passed (snapshot + feeds); 38 regressions passed; canonical combined run 84 passed, 0 failed.
- Slice 4: 27 new tests passed (pure functions, `--noconftest`).
- Slice 5: 20 new pure what-if tests passed (`--noconftest`); 8 new service/API tests passed on a throwaway `*_test` database — including the zero-side-effect proof (overview + what-if + predict-absence change no rows in `platform_action_executions`, `fin_invoice_batches`, `fin_payments`, `wf_runs`, `company_graph_records`, `company_graph_changes`, `platform_approval_requests`, `platform_outbox`).
- Slice 6: 5 new frontend tests passed; full frontend suite 27 passed, 0 failed; typecheck clean; `vite build` clean (twin UI present in built chunks).
- Combined pure run (what-if + horizon): 47 passed, 0 failed.
- Combined DB run (service + snapshot + feeds + impact): **76 passed,
  0 failed in 44:14** on a throwaway `*_test` database
  (`infraspine_twin_test`, Postgres 16 cluster 127.0.0.1:55442), run
  after a VM restart in a quiet window. Earlier attempts on the loaded
  VM were OOM-killed after 2–4 tests each (no failures, only kills);
  every service test was also verified individually across those
  attempts (8/8). 37 warnings only: Pydantic serializer warnings
  about slice 1's synthetic `Evidence` dicts (cosmetic, pre-existing).
- Full backend suite: **could NOT complete on this VM** — same
  environment that blocks the Step E gate (load 40–85, ~8 GB RAM
  shared by ~40 agents, OOM kills; Agent 104 owns the Step E
  full-suite gate). No twin code failure was observed in any run;
  quiet-VM re-run advised before merge. **Alembic roundtrip + check
  on the throwaway DB: CLEAN** — downgrade base → upgrade head
  (head `0103_unified_studio_governance`), `alembic check` = "No new
  upgrade operations detected". No migration in this step.

## Known limitations

- The twin-native failure engine in `whatif.py` intentionally matches
  Step AL's general engine (`app.whatif.simulator`) in meaning (same
  states, same all-providers rule, same recovery ranking). When the AL
  branch merges, the twin engine should delegate to
  `app.whatif.simulator.simulate` and keep only the scenario library,
  service wiring, drill comparison, and API. The two engines must never
  diverge — reconcile at merge time.
- The twin UI reads the company-graph map for the visual map; the
  twin snapshot itself is the simulation source of truth.
- Finance-amount visibility in the twin view is owner-only until the
  owner decides otherwise (the snapshot builder already carries the
  amounts; the UI states the restriction).
- No migration: the twin adds no tables. If a future slice needs
  persisted predictions, that is a new slice with its own migration.
- 37 Pydantic serializer warnings in DB runs (slice 1's synthetic
  `Evidence` dicts serialize as plain dicts, not `Evidence` models).
  Cosmetic only — no test fails; fix by typing the evidence dicts in
  a later cleanup slice, not by changing twin behaviour.

## Owner decisions (still open, safe defaults in place)

- May invoice-batch/payment amounts appear in the twin view for
  authorized finance viewers, or stay owner-only? (Default today:
  owner-only; the UI says so.)
- Which HR/membership source is authoritative for people/teams for
  live sync, and may supplier/system records be seeded from
  connectors? (Default today: live memberships only; nothing is
  seeded from connectors.)

## Merge readiness

- [x] DB run numbers filled in above (combined 76 passed, 0 failed)
- [ ] Full backend suite green — NOT completed (VM OOM; quiet-VM re-run advised; Step E gate owned by Agent 104)
- [x] Alembic roundtrip + check green (done on throwaway DB: downgrade base → upgrade head, head 0103, check clean)
- [x] Frontend suite + typecheck + build green (done: 27 passed, clean, clean)
- [ ] Reconcile with Step AL at merge (delegate failure engine to `app.whatif.simulator`)
- [ ] Reconcile with Step G at merge (drill record shape for `compare_drill`)
