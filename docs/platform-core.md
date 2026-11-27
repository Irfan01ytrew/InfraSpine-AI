# Platform core (`app/platform/`)

Step C of the architecture plan. One shared core for identity, scope, permission, approval,
exactly-once execution, events and the organisation's clock. Step D has moved the existing modules onto this core. Each module still computes its legacy
member/role decision and combines it with `authorize()` (`PLATFORM_ACCESS_MODE=enforce` by default:
both must allow; `shadow` keeps the legacy decision and only logs differences). Organisation
"today" (delegation, decision authority, budget dates) uses `org_today()` (default `Asia/Karachi`).

## Tables (migrations `0097_platform_core`, `0098_org_settings`, `0099_platform_outbox_retry`, `0100_platform_membership_backfill`)

| Table | Purpose |
|---|---|
| `org_settings` | single row (`id = 1`): `time_zone` (default `Asia/Karachi`), `currency` (`PKR`). No row ⇒ defaults. |
| `platform_scopes` | tree: one `org` root → `department` → `workspace` → `module_space`. `legacy_table`/`legacy_id` map an existing department or workspace for step D. |
| `platform_roles`, `platform_role_permissions` | named permission bundles (`*` = all permissions on that scope). |
| `platform_memberships` | user + role + scope, optional `valid_from`/`valid_to`, `revoked_at`. Inherited down the tree. |
| `platform_channel_identities` | verified WhatsApp/API identity → user. Unverified or revoked ones never resolve. |
| `platform_approval_requests` | one request per exact action: canonical JSONB action, SHA-256 `action_hash`, required permission, expiry, single use. |
| `platform_action_executions` | idempotency ledger (`idempotency_key` unique): claimed → dispatched → verified/failed. |
| `platform_outbox` | transactional outbox of domain events, drained with `SKIP LOCKED`, retry/backoff and dead-letter (`next_attempt_at`, `dead_at`). |
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

## Step D adoption (done)

1. Migration `0100` backfills `platform_scopes` / `platform_memberships` from users, access groups
   and the five module tenant/member tables (idempotent, reversible). The ORM also mirrors later
   changes, and access-time `reconcile_*` covers rows written without the ORM. A membership revoked
   by the platform (`revoke_reason` `platform:*`) is never re-mirrored implicitly.
2. Modules 03–08, 12, Agent Studio, RAG and WhatsApp/connectors call `module_gate` / `org_gate` /
   `group_gate` (or `readable_by`'s live `documents.read` check). Default mode is `enforce`.
3. Payments (workflow adapter dispatch), WhatsApp sends (Cloud API and Baileys), connector writes
   and recovery commands go through `execute_once`.
4. WhatsApp senders resolve through `resolve_principal` (`app.platform.channels`). Unmapped numbers
   get no data. Allow-list numbers are mirrored to the connection owner as
   `legacy:allowed_senders` until the step F verification flow; an explicit identity for a different
   user always wins.
5. The outbox worker (`run_outbox_worker`, also `python -m app.platform.outbox`) delivers events
   with retry, jittered backoff and dead-letter. `document.acl_changed` / `document.deleted` clear
   derived caches; retrieval and saved answers are authorised live in SQL, so a permission change is
   visible on the next request and cached answers are withheld.

Set `PLATFORM_ACCESS_MODE=shadow` only as a rollback switch if a platform defect ever blocks
legitimate work. Dropping the legacy member tables is step P10 (after a release with zero legacy reads).

## Tests and benchmark

- `tests/test_platform_core.py` — core acceptance tests (scope inheritance, 404/403, time-limited
  membership, principals, approval hash/expiry/single use/SoD, concurrent `execute_once`,
  crash-after-dispatch recovery, revoke, delegation active at 00:30 PKT).
- `tests/test_platform_outbox.py` — dispatcher retry/backoff, dead-letter, SKIP LOCKED, worker loop.
- `tests/test_platform_step_d.py` — backfill, one revoke cuts every module and RAG, document ACL
  change withholds cached answers, untrusted text cannot grant permissions, WhatsApp identity.
- `scripts/benchmark_platform_retrieval.py` — synthetic scoped corpus; FAISS (default) or an
  isolated Qdrant (`QDRANT_TEST_URL`) with a `scope_id` payload filter; reports ingestion rate,
  p50/p95/p99, throughput, ACL leaks and hardware. Smoke test: `tests/test_benchmark_platform_retrieval.py`.

Step E builds the unified workflow runtime and the invoice/payment flow on this core; see
[`unified-workflows-finance.md`](unified-workflows-finance.md) (migrations `0101`, `0102`).
