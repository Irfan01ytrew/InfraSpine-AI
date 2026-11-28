# Platform core (`app/platform/`)

Step C of the architecture plan. One shared core for identity, scope, permission, approval,
exactly-once execution, events and the organisation's clock. It is wired in **additively**:
existing modules keep their own checks until step D moves them onto this core one by one.
The only behaviour already switched over is "today": delegation, decision authority and
budget dates now use `org_today()` (organisation time zone, default `Asia/Karachi`).

## Tables (migrations `0097_platform_core`, `0098_org_settings`)

| Table | Purpose |
|---|---|
| `org_settings` | single row (`id = 1`): `time_zone` (default `Asia/Karachi`), `currency` (`PKR`). No row ⇒ defaults. |
| `platform_scopes` | tree: one `org` root → `department` → `workspace` → `module_space`. `legacy_table`/`legacy_id` map an existing department or workspace for step D. |
| `platform_roles`, `platform_role_permissions` | named permission bundles (`*` = all permissions on that scope). |
| `platform_memberships` | user + role + scope, optional `valid_from`/`valid_to`, `revoked_at`. Inherited down the tree. |
| `platform_channel_identities` | verified WhatsApp/API identity → user. Unverified or revoked ones never resolve. |
| `platform_approval_requests` | one request per exact action: canonical JSONB action, SHA-256 `action_hash`, required permission, expiry, single use. |
| `platform_action_executions` | idempotency ledger (`idempotency_key` unique): claimed → dispatched → verified/failed. |
| `platform_outbox` | transactional outbox of domain events, drained with `SKIP LOCKED`. |
| `users.auth_version` | bumped by `revoke_user_access`; any Principal holding an older value is dead. |

## Interfaces (`from app import platform`)

- `resolve_principal(db, *, session_token=… | channel=…+external_id=… | agent_id=…)` → `Principal`
  (user, kind `user`/`channel`/`agent`, `auth_version`, trace id).
- `authorize(db, p, permission, scope_id)` → `Decision`; `require(...)` raises **404** when the
  user has no membership on the scope (it does not exist for them) and **403** when they are a
  member without the permission. Legacy `admin` only bridges `platform.manage`,
  `org.settings.manage`, `identity.revoke` — never business duties.
- `visible_scopes(db, p, permission)` → `ScopeFilter`; `.sql(column)` gives a `WHERE` clause.
- `effective_permissions(db, user_id, scope_id)`.
- `request_approval(db, p, ActionSpec, required_permission=…, ttl=…)` (1 min – 7 days, default 1 h).
- `decide_approval(db, p, approval_id, "approve"|"reject", presented_hash=…)` — the decider must hold the
  required permission, cannot decide their own request, must pass separation of duties
  (`invoice.verify`, `payment.approve`, `funds.transfer` per subject), and the request must be
  pending and unexpired; the presented hash must equal the stored one.
- `consume_approval(db, approval_id, ActionSpec)` — single use, exact action hash, not expired;
  runs inside the caller's transaction.
- `execute_once(db, p, key, ActionSpec, duty=…, dispatch=…, verify=…, approval_id=None)` — claims
  the key with `INSERT … ON CONFLICT DO NOTHING` before dispatching, so concurrent or repeated
  calls dispatch once; a replay of a `dispatched` row re-verifies by reading back the provider
  state; a different action under the same key is a conflict. Actions in `APPROVAL_REQUIRED`
  (e.g. `funds.transfer` → `payment.approve`) need an approved, unconsumed approval.
- `revoke_user_access(db, actor, user_id, reason)` — bumps `auth_version`, ends memberships,
  sessions and channel identities, cancels pending approvals, publishes
  `identity.session_revoked`, audits.
- `org_today(db)`, `org_tz(db)`, `org_today_sql()` (for SQL builders), `set_org_time_zone(db, name)`.
- `publish(...)`, `audit(...)`, `drain_outbox(db, handler, limit=…)`.

Design notes: approval actions are stored as canonical JSONB (not encrypted) — never put
secrets in `ActionSpec.params`; floats are rejected (use `Decimal`/strings for money).

## Step D adoption path

1. Backfill `platform_scopes` from departments/workspaces (`legacy_table`, `legacy_id`) and
   memberships from existing role/department assignments.
2. Per module, replace its local permission check with `require()` and its list queries with
   `visible_scopes().sql(...)`, keeping the old check in shadow mode first (log mismatches).
3. Route external side effects (payments, WhatsApp sends, connector writes) through
   `execute_once`, and payment-type approvals through `request/decide/consume_approval`.
4. Point WhatsApp/API callers at `resolve_principal(channel=…)`.
5. Remove the legacy checks once shadow mismatches are zero.

## Tests and benchmark

- `tests/test_platform_core.py` — acceptance tests (scope inheritance, 404/403, time-limited
  membership, principals, approval hash/expiry/single use/SoD, concurrent `execute_once`,
  crash-after-dispatch recovery, revoke, delegation active at 00:30 PKT).
- `scripts/benchmark_platform_retrieval.py` — synthetic scoped corpus; FAISS (default) or an
  isolated Qdrant (`QDRANT_TEST_URL`) with a `scope_id` payload filter; reports ingestion rate,
  p50/p95/p99, throughput, ACL leaks and hardware. Smoke test: `tests/test_benchmark_platform_retrieval.py`.
