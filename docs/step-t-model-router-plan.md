# Step T — Smart Model Router: implementation plan

> Prepared by Agent 25 (model-router), branch `agent/model-router`, HEAD `ca956c2`.
> Status: slices 1–3 DONE (Agent 105), slice 4 DONE (Agent 139, HEAD `554565e`; CHANGES `2051b9f`).
> Step T is code-complete; merge gates (full-file re-run, Alembic roundtrip,
> tsc + vite build) are recorded in `assessment/CHANGES-step-T.md`.

## 1. What exists today (verified in the repo, 2026-11-28)

| Piece | Location | What it does |
|---|---|---|
| Provider catalog | `app/providers/registry.py` | `ProviderDefinition` + `ModelOption` (id, label, context window, max output tokens, `vision` flag). Only builtin provider is **DeepSeek** (`deepseek-flash`, `deepseek-v4-pro`, 1M context); Gemini etc. arrive as **custom OpenAI-compatible providers** configured in PostgreSQL. Admin UI renders from this metadata. |
| Provider admin | `app/services/provider_admin.py` | One active provider at a time, credential staging/activation with optimistic locking, endpoint validation, image-sharing consent. Model choice is admin-configured per provider. |
| LLM runtime | `app/services/llm_runtime.py` (`LLMRuntime.get_client`) | Returns the active provider's client, or the client for a `(provider, model)` **picked by the user in chat**. A pick never changes the active provider. No automatic selection anywhere. |
| Fallback | `app/services/llm_fallback.py` (`FallbackLLM`) + `Settings.llm_fallback_provider` | A single fallback provider id, used only when the primary fails with a balance / rate-limit / timeout / availability error. One fallback, no ordered chain, no cost awareness. |
| Cost data | `Settings.llm_model_pricing_json` (`"provider:model"` → input/output USD per 1M tokens) + `app/services/operational_health.py` (`PROVIDER_RATES` per-1k table, `get_provider_costs` from `RagTrace`) | Agent Studio estimates run cost from token usage × rates (`app/agent_studio/service.py:633`). Reports aggregate spend by provider/model over 30 days. No per-task cost, no latency tracking, no budget enforcement. |
| Usage tracing | `RagTrace` (`app/db/models.py`), `AgentStudioRun.estimated_cost_usd` | Token counts + provider/model recorded per trace/run. This is the raw material for router telemetry (slice 3). |

## 2. What is missing (the gaps this step closes)

1. **No task classification.** Every question, summary, invoice check and reasoning
   task goes to the same (user-picked or active) model. Cheap tasks pay flagship prices.
2. **No cheapest-capable selection.** Nothing compares models by capability vs price.
3. **No capability filtering.** Only the `vision` flag and context window exist as
   capability metadata; nothing checks "does this model fit this task" before calling it.
4. **No ordered fallback chain.** One fallback provider, no per-task alternates.
5. **No per-task cost/latency record.** Spend is visible only in aggregate reports;
   a single expensive task is invisible.
6. **No budget guard.** Nothing stops a runaway task from spending past a limit.

## 3. Design

### 3.1 Task classes

| Class | Examples | Needs |
|---|---|---|
| `simple` | greetings, yes/no, short lookups, classification | small context, short output |
| `standard` | RAG Q&A, summarise a document, draft a reply | medium context + output |
| `complex` | multi-step reasoning, finance verification, long-document analysis, code | large context, large output, strongest reasoning |
| `vision` | describe a chart/photo in a document | model with `vision=True` |
| `grounded_research` | live web / market / competitor questions | provider with native grounded search |

Classification is a **pure heuristic first** (prompt length, keywords, explicit
hints from the caller such as `needs_vision` / `needs_research`); a learned
classifier is explicitly out of scope for this step. The caller always wins:
an explicit `task_class` argument overrides the heuristic.

### 3.2 Routing policy

For a task, the router takes the configured candidate models (from the provider
catalog + admin-enabled providers) and:

1. **Filters** out models that cannot do the job: `vision` required but not
   supported; estimated prompt tokens > context window; required output >
   `max_output_tokens`; provider not enabled / no active credential (caller
   supplies the enabled set — the router never reads credentials or the DB).
2. **Ranks** the survivors by policy:
   - `cheapest` (default): lowest estimated cost for the estimated token counts;
     unpriced models sort last (never silently treated as free).
   - `fastest`: lowest latency tier, then cheapest.
   - `balanced`: cheapest model whose capability score meets the task class.
3. **Returns a `RoutingDecision`**: chosen `(provider, model)`, an ordered
   alternate list (the fallback chain), estimated cost in USD (or `None` when
   any leg is unpriced), and a human-readable `reason`. The caller (later:
   `LLMRuntime`) tries the chosen model, then each alternate in order — this
   replaces the single `llm_fallback_provider` for routed calls while leaving
   it in place for unrouted calls.
4. **Never logs secrets.** Decisions log only provider/model ids, task class,
   token estimates, cost estimate and latency — never prompts, keys or endpoints.

### 3.3 Cost + latency logging (slice 3, not this slice)

Each routed call records one row: task class, chosen provider/model, alternates
tried, prompt/completion tokens, measured latency ms, estimated cost USD.
Aggregated by the existing `get_provider_costs` report path and a new
per-task-class breakdown. Department monthly budgets (already configurable in
`operational_health.py`) become enforceable: routing refuses `complex` tasks
over budget and downgrades to the cheapest capable model with an audit entry.

### 3.4 Model capability / cost table (seed data)

The router reads pricing from `Settings.llm_model_pricing_json`
(`"provider:model"` → per-1M-token USD), falling back to the
`operational_health.PROVIDER_RATES` table only for reporting. Capability data
comes from the provider catalog (`ModelOption`); the router adds a small
per-model `capability_tier` (1 = fast/cheap, 2 = standard, 3 = flagship
reasoning) supplied by the caller/config, because the catalog has no reasoning
tier today. No prices are hard-coded in the router module itself.

| Model (catalog today) | Tier | Notes |
|---|---|---|
| `deepseek:deepseek-flash` | 1 | Default for `simple`/`standard`; vision-capable |
| `deepseek:deepseek-v4-pro` | 3 | Default for `complex` |
| Custom Gemini flash-lite (admin-configured) | 1 | Fast/cheap chat default on the owner's PC today |
| Custom Gemini pro-class (admin-configured) | 3 | `complex` / `grounded_research` when configured |
| Local Ollama endpoint (admin-configured) | 1 | `llm_local_only` deployments; zero cloud cost, higher latency |

## 4. Slices

1. **Slice 1 — DONE here:** pure-function router (`app/services/model_router.py`):
   task classification, capability filtering, cheapest/fastest/balanced ranking,
   ordered fallback chain, `Decimal` cost estimation. No I/O, no DB, no network,
   no model calls. Tests: `tests/test_model_router.py`.
2. **Slice 2 — wiring:** `LLMRuntime` gains `get_routed_client(db, task_class,
   ...)`; chat/RAG call sites pass a task class; routed calls keep the existing
   `FallbackLLM` behaviour as the last resort. Admin API exposes the routing
   decision (chosen + alternates + estimated cost) in the chat response metadata.
3. **Slice 3 — telemetry + budgets:** persist one routing record per routed call
   (migration), per-task-class cost/latency report, department budget guard.
4. **Slice 4 — admin UI — DONE (Agent 139):** stored routing policy
   (`routing_policy_settings`, migration `rt05_0002_routing_policy`) with a
   monthly budget cap of $1,000,000 as typo protection; department budgets
   override the org-wide budget; the budget guard is wired into the
   `resolve_chat_client` / `get_routed_client` path at the chat, conversations
   and RAG call sites, so an over-budget call now downgrades (simple) or
   refuses with 402 (complex). Admin API `GET/PUT /api/admin/routing-policy`
   (admin-only, audited) + `GET /api/admin/routing-report`; Settings → Models
   tab has the routing policy editor (default policy, per-task-class overrides,
   capability tiers, org + per-department budgets, spend report); chat answers
   show "Why this model?" with task class, policy, estimated cost and budget
   action. Two real bugs fixed in the slice: (1) department lookup took the
   user object, which expires after a session commit (`MissingGreenlet`
   swallowed to "no department") — helpers now take the user id and load the
   owner fresh (and the missing `User` import that made every lookup return
   None); (2) the 402 test asserted the wrong error-body shape (`{"error":
   {"code": ...}}`). (3) Unpriced routing ignored the admin's configured
   model (latency tie-break) — fixed: with no price basis, a cost-driven
   policy prefers the configured model (`4c0acf7`). (4) Routing
   candidates ignored `ProviderConfig.is_active`, so deactivating a
   provider did not stop routed traffic — fixed (`554565e`). Commits:
   `8423798` (policy + wiring), `e2d32c1` (frontend), `1fda4ad`
   (user-id lookup + error-body fix), `d47c639` (`User` import fix),
   `4c0acf7` (configured-model preference), `554565e` (active-provider
   filter), `2051b9f` (CHANGES-step-T.md). **Integration warning: the
   integrate branch merged Step T at `1fda4ad`; the three fix commits
   after it (`d47c639`, `4c0acf7`, `554565e`) are NOT in integrate —
   merge branch HEAD `554565e` (or later).**

## 5. Test list

- Slice 1 (implemented): classification of all five classes + caller override;
  vision/context/output filtering; cheapest ranking with priced vs unpriced
  models; fastest/balanced ranking; ordered fallback chain; cost estimation
  (exact `Decimal` math, unpriced → `None`); budget cap refuses over-budget
  choices; empty/unknown candidates raise a clear error; no secrets in decisions.
- Slice 2: routed client picks the router's choice; fallback chain is honoured
  on provider failure; unrouted calls behave exactly as before (regression).
- Slice 3: routing records persist; cost/latency report matches `RagTrace`
  aggregates; budget guard downgrades + audits.
- Slice 4: policy editor round-trips through the admin API; explanation text
  contains no prompt content.

## 6. Rules (from HANDOVER-PROMPT.md §5, apply to every slice)

- Do not redo finished work; add the router beside `LLMRuntime`, do not rewrite it.
- Every lookup/dispatch through `app/platform`; routing never changes what a user
  may see or do — it only picks which model answers.
- Prompts, documents and web content are untrusted; they can never change the
  routing policy, the budget, or which provider is used.
- Never print, log or commit secrets (API keys, endpoints with credentials).
- Tests only on throwaway `*_test` databases; pytest log capture stays on.
