# CHANGES — Step T: Smart Model Router

> Branch `agent/model-router`, base `step-e-gaps-wip` (`ca956c2`).
> Status: slices 1–4 done. Step T is code-complete.
> Focused gates green (see "Test evidence"). The full-backend-suite count is
> covered by the merged-HEAD verification (Agent 96 / Agent 104 / Agent 168);
> merge only after that tally is green.

## What this step does (plain language)

InfraSpine used to send every chat and RAG question to one configured model,
whatever the question was. The smart model router now picks the right model
for each question — a cheap fast one for simple questions, a stronger one for
hard ones — and it keeps a monthly spending budget so the bill can never run
away unnoticed.

- **It picks by task, not by guess.** Every question is classified (simple,
  analysis, complex, vision, research). Simple questions go to the cheapest
  model that can do the job; hard questions go to a stronger model. If the
  first model fails, the next one in line answers instead.
- **Admins control it.** Settings → Models has a routing policy editor: the
  default policy (cheapest / fastest / balanced), a different policy per
  task type, a capability tier per model, an org-wide monthly budget in
  dollars, and a different monthly budget per department. A department's
  own budget beats the org-wide one. Budgets are capped at $1,000,000 so a
  typo can't set a billion-dollar budget.
- **The budget guard is real.** If the month's spend is already over budget,
  a hard question is refused with a clear "over budget" error (HTTP 402) and
  a simple question is quietly downgraded to the cheapest model. A model
  with no price can never prove it fits a budget, so it is refused rather
  than assumed free.
- **"Why this model?"** Every chat answer now shows which model answered,
  what kind of task it was, which policy picked it, the estimated cost, and
  whether the budget changed anything — with the router's reason in plain
  words. No prompt text is ever shown.
- **Everything is recorded.** One routing record per routed call (model,
  cost estimate, task type, department, budget action), a cost/latency
  report per task type, and a spend report in the policy editor.

## The problem it fixes

Before this step there was no routing at all: every question paid the price
of the one configured model, and the budget guard existed as code but no
route ever passed it a budget, so it could never refuse or downgrade
anything. The admin also had no way to see or change how models were
picked, and a user could not tell why a particular model had answered.

## Files changed (vs `ca956c2`)

### Slice 1 — pure-function router (Agent 25, plan; tests 31)
- `app/services/model_router.py` (new) — task classification, capability
  filtering, cheapest/fastest/balanced ranking, ordered fallback chain,
  exact `Decimal` cost estimation (unpriced → `None`, never free).
- `tests/test_model_router.py` (new, 31 tests).

### Slice 2 — wiring (Agent 105, `49ce339`)
- `app/services/llm_runtime.py` — `get_routed_client()` picks the routed
  model; `resolve_chat_client()` is the single entry point for
  `/api/ai/chat`, `/api/conversations` and `/api/rag/chat`; `RoutingInfo`
  metadata (provider/model, cost estimate, reason — never prompt text or
  keys); 402 `routing_budget_exceeded`.
- `app/api/routes/chat.py`, `conversations.py`, `rag.py` — call sites use
  the routed client and record the routing decision.
- `tests/test_model_router_wiring.py` (new, 13 tests).

### Slice 3 — telemetry + budgets (Agent 105, `3339171`)
- `app/db/models.py` — `ModelRoutingRecord`.
- `migrations/versions/rt05_0001_model_routing.py` (new, down 0103) —
  routing records table.
- `app/services/model_router.py` / `app/services/llm_runtime.py` —
  `routing_cost_report()`, `routing_spent_this_month()`, budget guard
  (downgrade / refuse).
- `tests/test_model_routing_records.py` (new, 9 tests).

### Slice 4 — stored policy + budget wiring + admin UI (Agent 139)
- `app/services/routing_policy.py` (new) — `validate_routing_policy` /
  `parse_routing_policy` / `effective_policy` / `effective_budget_usd` /
  `effective_capability_tier`, `load_routing_policy` /
  `save_routing_policy` (single row, id 1; a corrupt stored row logs a
  warning and falls back to the default), `department_for_user` (first
  department by name — the Step N cost-attribution convention),
  `routing_context_for_user` (one load: policy + department + budget).
- `app/db/models.py` — `RoutingPolicySetting` (`routing_policy_settings`,
  JSONB task-class policies / capability tiers / department budgets with
  amounts as strings, monthly budget >= 0).
- `migrations/versions/rt05_0002_routing_policy.py` (new, down
  `rt05_0001`) — routing policy table.
- `app/services/llm_runtime.py` — `get_routed_client` takes a stored
  policy config (per-task-class policy, tier overrides) and a monthly
  budget (explicit caller budget, else the stored policy's budget for
  the caller's department); unpriced routing prefers the active
  provider's configured model (see "Bugs fixed" below).
- `app/api/routes/chat.py`, `conversations.py`, `rag.py` — each resolves
  `routing_context_for_user` once per request and passes the department,
  budget and policy into `resolve_chat_client`; routing records now carry
  the department id (was `None` — per-department spend is now
  attributable).
- `app/api/routes/admin_routing.py` (new) — admin-only `GET/PUT
  /api/admin/routing-policy` (PUT validates every problem at once → 422,
  audits `routing.policy_updated` with from/to, no secrets) and `GET
  /api/admin/routing-report` (`?days=1..90`, `?department_id=`; spent
  this month vs budget, remaining, per-task-class spend).
- `frontend/src/components/RoutingPolicySection.tsx` (new) — the policy
  editor in Settings → Models: default policy, per-task-class overrides,
  per-model tiers, org + per-department budgets, spend panel.
- `frontend/src/components/SettingsPage.tsx` — models tab renders the
  editor. `frontend/src/components/KnowledgePage.tsx` — "Why this model?"
  on answers. `frontend/src/types.ts`, `frontend/src/api.ts` — routing
  types + API client.
- `tests/test_routing_policy.py` (new, 20 tests).
- Commits: `8423798` (policy + wiring), `e2d32c1` (frontend), `1fda4ad`
  (user-id lookup + error-body fix), `d47c639` (`User` import fix),
  `4c0acf7` (unpriced routing prefers the configured model),
  `554565e` (routing candidates must be active providers), plus the
  CHANGES + plan-doc commit below.

## Test evidence

All runs on the private throwaway test cluster
(`/home/hatch/.pg-agent105/cluster`, port 55501, superuser `postgres`,
trust auth; databases `infraspine_t105` / `infraspine_t105_test`) with the
setup-baseline venv, except the pure tests which need no database.

- Slice 4 policy file (`tests/test_routing_policy.py`, 20 tests):
  **20 passed in one process** (1,069 s) on final HEAD `554565e`.
  History: 19 passed in one process (2,458 s) on HEAD `d47c639`; the
  2 tests that failed in the first full run were fixed and re-ran green
  (2 passed in 451 s); the 20th test (configured-model preference,
  HEAD `4c0acf7`) passed in the conversations run below.
- Slice 2/3 regressions + owed re-runs, all on HEAD `4c0acf7` unless
  noted: `test_conversations.py` (10 tests) **10 passed** (940 s for
  11 tests incl. the new policy test). `test_providers.py` (20),
  `test_model_router_wiring.py` (13), `test_model_routing_records.py`
  (9), `test_model_router.py` (31) combined: **72 passed, 1 failed**
  (3,587 s) — the one failure was `test_deactivate_stops_traffic`
  (see bug 5); it **passed on re-run** (171 s) after the fix on HEAD
  `554565e`. So providers is 20/20 across the two runs, wiring 13/13,
  records 9/9, router 31/31 in the combined run.
- Slice 1 regressions: 31 passed (Agent 105); slice-2 wiring 13 passed,
  slice-3 records 9 passed, `test_llm.py` 18 passed (Agent 105).
- Frontend: `npm test` **22 passed, 0 failed**; `tsc --noEmit` clean;
  `vite build` clean (exit 0, full `dist` emitted). Note: backgrounded
  `tsc` runs died silently five times on this loaded VM (no output, no
  OOM-kill counter movement); the same command in the foreground
  finished in ~13 s with exit 0. Run frontend checks in the foreground.
- Alembic: upgrade → downgrade (`rt05_0002` → `rt05_0001`) → upgrade
  roundtrip green on a fresh throwaway database; single head
  `rt05_0002_routing_policy`; `alembic check` clean ("No new upgrade
  operations detected").

## Bugs fixed

Real product bugs found and fixed in this step (all with regression
tests):

1. **Department budget could never apply after a session commit
   (slice 4).** `department_for_user` took the user object; after a
   commit in the same session the ORM user is expired and reading its
   attributes raises `MissingGreenlet`, which the best-effort lookup
   swallowed as "no department" — so the department budget silently
   never applied. The helpers now take the user id and load the owner
   fresh (`db.get(User, ...)`); call sites pass `user.id` while the user
   is fresh.
2. **Missing `User` import made every department lookup return `None`
   (slice 4).** The fix above referenced `User` in
   `app/services/routing_policy.py` without importing it; the same
   best-effort `except` swallowed the `NameError`. Import added; a
   direct repro now resolves the department after a commit.
3. **The router silently overrode the admin's configured model
   (slice 2, found by the owed `test_conversations.py` re-run).**
   `test_history_survives_a_provider_model_change` failed: after the
   admin changed the provider model to `deepseek-v4-pro`, routed answers
   still recorded `deepseek-flash`, because with no prices configured
   the router ranked unpriced models by latency tier. The configured
   model is documented as the deployment default
   (`model_options`: "The active provider's configured model is the
   default"), so it was a dead control. Fix: when the chosen model is
   unpriced and the policy is cost-driven (cheapest / balanced),
   `get_routed_client` prefers the active provider's configured model
   if it can do the task. A priced choice still stands (that is the
   router's job), and FASTEST still ranks on latency. Test:
   `test_unpriced_routing_prefers_configured_model` (unpriced follows
   the configured model; priced follows cheapest).
4. **Test bug:** the 402 test asserted `response["code"]`; error bodies
   are `{"error": {"code": ...}}`. Assertion fixed, product unchanged.
5. **Deactivating a provider no longer stopped traffic (slice 2,
   found by the owed `test_providers.py` re-run).**
   `test_deactivate_stops_traffic` failed: after deactivation, routed
   chat still answered 200. Deactivation flips
   `ProviderConfig.is_active`, not the credential status — but
   `_routing_candidates` filtered only on the credential, so a
   deactivated provider stayed a routing candidate. The pre-router
   `_client` path filtered `is_active` for the default client, so this
   was a slice-2 regression. Fix: require `is_active` in the candidate
   query; with no active provider the router raises
   `LLMNotConfiguredError` (503), as before. The test itself is the
   regression test (passed on re-run after the fix).

## Known limits

- The router only picks among models of providers with an active
  credential; a provider with no credential never appears.
- Pricing comes from `Settings.llm_model_pricing_json`. Without prices
  every model is unpriced: the router ranks by latency/capability tiers
  and the configured model wins (see bug 3). Cost estimates are `None`
  for unpriced models; the spend report shows them as unknown, never $0.
- The budget guard counts spend from routing records only (routed calls
  through `resolve_chat_client`). Calls that bypass the router are not
  counted.
- A department is the caller's first department AccessGroup by name
  (the Step N convention). A user in no department gets the org-wide
  budget. If the department lookup fails for any reason, routing
  continues with the org-wide budget and logs at debug level — routing
  never breaks chat.
- The policy editor is admin-only. There is no per-user policy.

## Merge notes

- Migrations: `rt05_0001_model_routing` (down `0103`) and
  `rt05_0002_routing_policy` (down `rt05_0001`). Rebase onto the
  merge-time head if `0103` is no longer the head. Integration snapshot
  map v5 reserves **0124–0125** for Step T (`rt05_0001` → 0124,
  `rt05_0002` → 0125); renumber at merge per the map.
- Step T is the pricing source for Step N (cost intelligence). Step N
  reads routing records and the cost report; no interface changed in
  slice 4 except the records now carry `department_id` (Step N's
  one-run-one-department rule is now fed directly) and the policy
  endpoints are new (no N dependency).
- `app/services/llm_runtime.py`, `app/api/routes/chat.py`,
  `conversations.py`, `rag.py`, `app/db/models.py`,
  `frontend/src/types.ts`, `api.ts`, `KnowledgePage.tsx` are CRLF files
  — edit them via CRLF-safe Python scripts, never a plain LF edit.
  New files are LF.
- `frontend/package-lock.json` was touched by `npm install` (54
  `"libc"` metadata lines removed by the newer npm); reverted, not
  part of this step.
- No migration in the frontend commit; `node_modules` is not committed.

## Still owed

- Full backend suite before merge (load control; the merged-HEAD
  verification owns that gate).
- The `test_providers.py` + wiring + records + router combined run was
  on HEAD `4c0acf7`; the only later change is the one-line `is_active`
  filter in `_routing_candidates` (HEAD `554565e`), verified by the
  re-run of `test_deactivate_stops_traffic` and the full policy file
  (20 passed) on `554565e`. A combined re-run on the final HEAD is a
  nice-to-have, not a blocker: every file in it passes on a HEAD that
  differs only by that filter, and the filter's own test passes.
- **Integration note:** the integrate branch merged Step T at `1fda4ad`
  (HEAD `9f0b306`, 2026-11-28 ~15:54 PKT). That snapshot predates the
  three fix commits `d47c639` (missing `User` import — without it every
  department lookup returns `None`), `4c0acf7` (configured-model
  preference) and `554565e` (active-provider filter). The integrator
  must merge the branch HEAD `554565e`, not `1fda4ad`.
- Owner decisions still open: none new in slice 4. The default policy
  is cheapest; the monthly budget cap is $1,000,000 (typo protection);
  per-department budgets override the org-wide budget. All are
  admin-editable in the UI, so no owner decision blocks the merge.
