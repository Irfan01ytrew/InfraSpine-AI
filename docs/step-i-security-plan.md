# Step I — Security hardening and release: plan

Owner's release gate for the founder product (steps D–H). Master plan v2 estimate:
16–24 h. Step I is **preparation + first safe slice** in this branch; slices 2–8
remain for the full Step I build. Nothing here changes auth behaviour.

## 1. What Step I must deliver (master-plan-v2.md:43, 382)

Security review, observability, runbooks, upgrade/rollback, English + Roman Urdu
docs. Release of the *founder* product — not a stop on J–N. I is a release gate
for D–H: nothing ships live from F/G/H until I is green.

## 2. Audit findings (branch `step-e-gaps-wip`, HEAD `ca956c2`, 2026-11-28)

Measured by runtime introspection of the real FastAPI app (`create_app()`):
**580 API routes** registered (plus `/docs`, `/openapi.json`, which are plain
Starlette routes, not API routes).

### Already strong — do not rewrite
- Passwords: Argon2id (`app/core/security.py`), dummy-hash timing defence for
  unknown users, session tokens stored as SHA-256 only (`hash_session_token`).
- Sessions: SameSite=Strict cookies, `SESSION_COOKIE_SECURE` forced true in
  production (validator in `app/core/config.py`), CSRF custom header
  (`X-Requested-With`) on state-changing requests, 8 h TTL, login lockout
  (5 failed attempts / 15 min, `app/services/auth.py:67,358`), TOTP MFA with
  enrollment/reauthentication policy (`app/core/totp.py`, `app/api/deps.py`).
- Platform core (`app/platform/`): scope inheritance, 404 for non-members /
  403 for members without permission, approval hash + expiry + single use +
  separation of duties, `execute_once` idempotency ledger, `revoke_user_access`
  bumps `auth_version`, transactional outbox with retry/dead-letter.
- Route authentication: **558/580 routes** carry a session/token dependency
  (`get_current_user` / `require_admin` / `require_manager` /
  `require_reports_viewer` / `get_current_partner` / `verify_scim_token`) in
  their FastAPI dependency tree. The remaining 22 are all accounted for
  (see §3).
- Webhook authentication: Meta WhatsApp verify token + `X-Hub-Signature-256`
  HMAC (`app/api/routes/agent_studio_whatsapp.py:24,37`), Slack HMAC-SHA256
  with 5-minute replay window (`app/services/collaboration.py:21`), connector
  HMAC (`X-InfraSpine-Signature`, `app/api/routes/integrations.py:123`),
  SCIM bearer token (`verify_scim_token`), partner sessions on a separate
  cookie (`get_current_partner`).
- Transport/headers: CSP on `/admin`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cache-Control:
  no-store` on `/api`, no CORS permissions granted, docs/OpenAPI disabled in
  production (`app/main.py`).
- Input/SSRF: upload size caps with `Content-Length` required before parsing,
  ZIP-bomb caps, parser isolation with memory caps, SSRF-protected provider /
  webhook origins (allow-listed private origins only), metadata/link-local
  addresses always blocked (`app/core/config.py`).
- Secrets: `SecretStr` throughout, master key validated as base64 of exactly
  32 bytes, approval `ActionSpec` stores canonical JSONB — never secrets.

### Gaps — the Step I work list
1. **No API rate limiting.** No `slowapi`/custom throttle anywhere in `app/`
   (only per-provider LLM retries). Login has lockout, but nothing bounds
   webhook floods, chat/LLM spend, or repeated failed calls on other routes.
2. **Authorization is mostly legacy role-based, not platform-scoped.** Only 19
   of 565 session-guarded routes show a platform marker
   (`authorize`/`require`/`resolve_principal`/gates) in the endpoint source;
   the rest rely on `require_admin`, legacy grants, or gates buried in service
   layers. There is no per-route proof of platform coverage — that is what
   slice 1 now tests at the authentication level, and slice 3 must do at the
   authorization level.
3. **Audit coverage is unmeasured.** `platform.audit()` exists
   (`app/platform/events.py:21`) and auth revokes audit, but no inventory
   proves every privileged action emits an audit event.
4. **Secret rotation is incomplete.** `MASTER_ENCRYPTION_KEY` has a key id
   (`v1`) but no documented rotation/re-encryption flow. A DB credential was
   exposed in older docs (handover §5.9) — the owner must rotate it; no code
   can do that for them.
5. **Teams webhook auth is not implemented** — the endpoint refuses all
   activity with 503 (`app/api/routes/collaboration.py:130`). Safe, but the
   integration is dead until Bot Framework JWT validation lands.
6. **Missing headers:** no `Strict-Transport-Security`, no
   `Permissions-Policy` (admin CSP exists).
7. **Public metadata endpoints** leak deployment facts: `/api/integrations/status`
   reports whether Slack/Teams are configured (presence booleans only, no
   secrets) and `/api/deployment/profile` returns branding pre-sign-in. Low
   risk, documented in the allowlist.
8. **No dependency vulnerability scan** in the repo (no `pip-audit` /
   lockfile audit step); `dependency-license-inventory` exists but is not a
   security gate.
9. **Release runbooks / drills** (backup-restore, upgrade/rollback, failure
   drills) are documented for live-push but not exercised end-to-end as a
   release gate; Roman Urdu operator docs are partial.

## 3. Route authentication inventory (slice 1 evidence)

| Class | Count | Examples |
|---|---|---|
| Session/token-guarded (`get_current_user` / `require_admin` / `require_manager` / `require_reports_viewer` / `get_current_partner` / `verify_scim_token`) | 558 | all business, admin and partner routes |
| Public by design | 8 | `GET /`, `GET /favicon.ico`, `GET /health`, `GET /health/ready`, `GET /api/auth/sso/config`, `GET /api/deployment/profile`, SCIM `Schemas` + `ServiceProviderConfig` |
| Authenticate themselves (credentials / invite token / OIDC code / signed SAML, not session) | 7 | `POST /api/auth/login`, `POST /api/auth/mfa/challenge`, `POST /api/auth/sso/callback`, `POST /api/auth/sso/saml`, `POST /api/onboarding/accept`, `POST /api/partner/auth/accept`, `POST /api/partner/auth/login` |
| Signature-verified webhooks | 5 | WhatsApp verify + event, Slack events + command, connector webhook |
| Public status flags (presence booleans only, no secrets) | 1 | `GET /api/integrations/status` |
| Refuses all activity | 1 | Teams messages (503 until Bot Framework JWT lands) |
| **Total** | **580** | |

The test `tests/test_step_i_authz_coverage.py` encodes this inventory: any new
unauthenticated route fails until it is added to the allowlist with a reason.

## 4. Hardening checklist

- [ ] Auth/session: session TTL + idle timeout policy confirmed by owner;
  absolute session lifetime; password policy review (min length 8 today);
  MFA enrollment enforcement decision (`org_mfa_required` is off by default).
- [ ] Authorization coverage: per-route platform-gate inventory (slice 3);
  legacy-only routes migrated to `module_gate`/`org_gate`/`group_gate` or
  documented as admin-only; no route relies on role string alone for
  business data.
- [ ] Audit trail: inventory of privileged actions vs emitted audit events;
  audit events immutable/append-only; admin audit viewer.
- [ ] Secrets: master-key rotation flow + re-encryption; provider-key
  rotation; owner rotates the exposed DB credential; `.env.example`
  documents every secret and its rotation owner.
- [ ] Rate limiting/abuse: per-route + per-user + per-IP limits; webhook
  flood protection; LLM spend guardrails.
- [ ] Transport/headers: HSTS (only behind HTTPS), `Permissions-Policy`,
  keep CSP tight as frontend changes.
- [ ] Webhooks/external input: Teams Bot Framework JWT validation; webhook
  payload size + replay protection everywhere (Slack 5-min window is the
  pattern to copy).
- [ ] Data protection: encryption-at-rest review (DB + `data/`), backup
  encryption, PII redaction in logs/traces.
- [ ] Observability: security event alerting (lockouts, revokes, failed
  approvals, webhook auth failures); log retention policy.
- [ ] Dependencies: `pip-audit` (and `npm audit`) as a gate; lockfile
  freshness; license inventory kept current.
- [ ] Release: upgrade/rollback runbook exercised on a throwaway install;
  backup-restore drill; Roman Urdu operator docs; `CHANGES-step-I.md`.

## 5. Slice order (slice 1 done now; slices 2–8 listed)

1. **Authorization-coverage test** (this branch): route inventory test +
   Slack HMAC unit tests + login CSRF + production cookie guard.
   `tests/test_step_i_authz_coverage.py`.
2. Rate limiting: per-route/user/IP throttle middleware + tests; webhook
   flood protection. ✅ DONE in this branch (slice 2 below).
3. Platform-gate migration: per-route authorization inventory; migrate
   legacy-only routes to platform gates (largest slice; do it module by
   module, with negative-authorization tests per module). ✅ STARTED in this
   branch (slice 3 below: `/api/admin/*` + inventory; the remaining
   admin-only routers are the backlog).
4. Audit coverage: privileged-action inventory vs audit events; fill gaps.
5. Secret rotation: master-key rotation flow + re-encryption; owner rotates
   the exposed DB credential; rotation runbook.
6. Headers + Teams auth: HSTS / `Permissions-Policy`; Bot Framework JWT
   validation for Teams (or keep refusing, documented).
7. Dependency scan: `pip-audit` + `npm audit` gates; fix or pin.
8. Release: runbooks exercised, backup-restore + rollback drills,
   `CHANGES-step-I.md`, full suite / Alembic / frontend gates, then merge
   to main.

## 6. Test list

- `tests/test_step_i_authz_coverage.py` — every route authenticated or
  allowlisted (exact allowlist); Slack HMAC accept/reject cases; login CSRF
  dependency; production rejects `SESSION_COOKIE_SECURE=false`.
- Later slices add: audit-event tests, rotation tests, header tests,
  `pip-audit` gate.
- `tests/test_step_i_rate_limit.py` (slice 2) — pure limiter units (allow /
  deny / window reset / key independence / zero-means-unlimited), route
  classification, middleware 429 with Retry-After on a fresh app, health
  exemption, per-user wiring through `get_current_user` (non-admin limited,
  admin exempt).
- `tests/test_step_i_admin_platform_gate.py` (slice 3) — non-admin still 403,
  admin still 200 with a spy proving `org_gate("platform.manage")` is
  consulted, no shadow diff for an active admin, self-change guard intact.

## 6b. Slices 2–3 — what was implemented (this branch)

### Slice 2 — rate limiting (`app/security/rate_limit.py`, settings, middleware)
- Pure core: `RateLimiter` — thread-safe, in-memory fixed-window counters
  (stdlib only, monotonic clock, injectable `now` for tests). A limit of 0
  disables that bucket.
- Two layers. (a) Middleware (`app/main.py::request_context`, before the
  license check): per-IP buckets on `/api/` paths only — `auth` 30/min
  (sign-in fronts Argon2id hashing), `webhook` 120/min (signature-verified
  webhooks), `api` 600/min. Over the limit → 429 `{"error": {"code":
  "rate_limited", ...}}` + `Retry-After` header. (b) Per-user bucket in
  `get_current_user` (`app/api/deps.py`): 600/min per authenticated user,
  raising `RateLimitError` (new in `app/core/errors.py`, 429 `rate_limited`).
- Never locks out the owner admin: administrators are exempt from the
  per-user bucket. Health/readiness (`/health`, `/health/ready`, `/`, admin
  static UI) are never limited, so probes cannot be throttled.
- Off entirely when `APP_ENV=testing` (or `rate_limit_enabled=false`), so
  no existing suite is throttled; slice tests opt in by building an app
  with `APP_ENV=development` and tiny limits.
- Deliberate v1 limitation: counters are per-process, so a multi-worker
  deployment enforces the limit per worker. The limits still bound floods
  per worker; a shared store (Redis/DB) is later work if the owner wants it.
  Owner input still open: confirm the default limits (auth 30, webhook 120,
  api/user 600 per minute).

### Slice 3 — platform-gate migration, start (`/api/admin/*`)
- New dependency `require_platform_admin` (`app/api/deps.py`): the legacy
  admin role still decides who may administer (non-admins keep the exact
  403 `require_admin` gave), and `org_gate(db, user, "platform.manage")`
  adds the platform decision on top — principal freshness (revoked/stale
  principal refused) and membership. In enforce mode the platform can only
  take access away, never add it, so no authorized administrator loses
  access. `app/api/routes/admin_users.py` (9 endpoints: users, roles,
  invitations) now uses it via `PlatformAdminUser`.
- Honest scope note: with the admin bridge (`platform.manage` is a bridge
  permission for legacy admins), the gate today agrees with the legacy
  decision for every active administrator — verified by the tests (no
  shadow diff). Its enforcement value lands fully when the bridge is
  narrowed in later slices; the gate, the reusable dependency, and the
  negative tests are the prerequisite. No authorized user changes behaviour;
  no API contract changes (same 403/200 as before).

### Slice 3 — authorization inventory (measured 2026-11-28)
Agent 27's "19 of 565 show a platform marker in endpoint source"
undercounts: most modules gate at the *service* layer, not in the route
function. Verified gating by module:

| Module | Platform gate (where) | Status |
|---|---|---|
| Finance | `app/finance/service.py::require` → `platform.require` per department scope (`finance.read/manage`, `invoice.*`, `payment.approve`, `funds.transfer`) | gated |
| Knowledge / RAG / chat | `app/rag/access.py` — `documents.read` org gate on retrieval | gated |
| Workflows | `app/workflows/security.py` — `group_gate` | gated |
| Continuity | `app/continuity/access.py` — `module_gate` | gated |
| Company graph | `app/company_graph/access.py` — `module_gate` | gated |
| Governance | `app/governance/service.py` — `module_gate` | gated |
| Recovery | `app/recovery/access.py` — `module_gate` | gated |
| Process intelligence | `app/process_intelligence/access.py` — `module_gate` | gated |
| Agent Studio | `app/agent_studio/service.py` — `org_gate("agent.run")` | gated |
| Quality | `app/api/routes/quality.py` — `org_gate("qa.manage")` | gated |
| Admin users | `app/api/deps.py::require_platform_admin` — `org_gate("platform.manage")` | gated (slice 3) |
| Agent actions | own approval logic (no self-approve, admin/manager only) | own logic; platform mapping still open |
| Admin-only routers (backlog) | `require_admin` only, 0 platform markers: admin_embeddings, admin_media, admin_ocr, admin_parsers, admin_reports, admin_traces, admin_agent_runs, access_admin (+ deployment/company/license writes) | gated (slice 4) |

## 6c. Slices 4–5 — what was implemented (this branch)

### Slice 4 — audit coverage + platform-gate backlog migration (commit `f169f39`)
- New `app/security/audit_coverage.py`: `PRIVILEGED_ADMIN_ACTIONS`
  inventories all **70 state-changing `/api/admin/*` routes** (runtime
  introspection of `create_app()`, same walk as the slice-1 test);
  `measure_audit_coverage(app)` reports **coverage 1.0** — every listed
  action emits at least one declared audit event or is read-only by effect.
- Four actions had no audit event before this slice and now do:
  `admin.embedding_model_tested` (embedding-model test),
  `admin.ocr_engine_tested`, `admin.document_parser_tested`, and
  `admin.department_quota_updated` (the quota PUT writes only in-memory
  config, so the audit row is the only record).
- Migrated to `require_platform_admin` / `PlatformAdminUser` (11 routers +
  `get_provider_admin` in `app/api/deps.py`): admin_embeddings, admin_media,
  admin_ocr, admin_parsers, admin_reports (quota PUT only — reads stay on
  `require_reports_viewer`), admin_traces, admin_agent_runs, access_admin,
  deployment (incl. company/license/template-apply), platform_admin,
  admin_providers. Non-admins keep the exact 403 `require_admin` gave;
  admins go through `org_gate("platform.manage")` with the legacy bridge.
  The bridge is **not** narrowed here — that is a later decision, recorded
  as follow-up (its enforcement value lands fully when narrowed).
- 21 new tests (`tests/test_step_i_audit_coverage.py`), all passed:
  inventory completeness + coverage 1.0, 11 non-admin 403 probes,
  3 gate-consulted proofs, 5 behavioural audit proofs against the real
  endpoints. A standalone measurement script confirms 70/70 + 1.0.

### Slice 5 — master-key rotation support (commit `bbce448`)
- New `RotatingCredentialCipher` (`app/core/crypto.py`): decrypts with the
  current key **or any retired key**, encrypts only with the current key,
  and still refuses unknown key ids. `reencrypt()` returns an already
  current value unchanged, so a sweep never rewrites current rows. The
  `{"ct","iv","kid"}` sealed-JSON helpers (`encrypt_sealed_json` /
  `decrypt_sealed_json`) match the shape `app/integrations/security.py`
  already uses.
- **Every cipher construction site** now builds the rotating cipher via
  `rotating_cipher_from_settings(settings)` — API (`app/main.py`), both
  workers (`app/worker.py`, `app/workflows/worker.py`), CLI (`app/cli.py`),
  webhooks (`app/services/webhooks.py`), cloud connectors, Agent Studio
  (`app/agent_studio/unified.py`), RAG ingestion and the evaluation harness.
  With no previous key configured it behaves exactly like the single-key
  cipher (subclass — all `CredentialCipher` type annotations still hold).
- Settings: `MASTER_ENCRYPTION_KEY_PREVIOUS` +
  `MASTER_ENCRYPTION_KEY_PREVIOUS_ID`, validated as a pair at startup —
  previous key without id, previous id equal to the current id, or a
  non-32-byte previous key all refuse to start. Documented in
  `.env.example`.
- New `app/security/key_rotation.py`: `rotate_provider_credentials()` sweep
  (active/pending rows under a retired key, `SKIP LOCKED`, per-row audit
  `provider.credential_reencrypted`, undecryptable rows left exactly as
  they were and reported under `skipped` — never deleted or guessed at);
  `count_stale_provider_credentials()` (the runbook's verification query —
  must read 0 before the retired key is dropped);
  `reencrypt_sealed_json()` for the JSON sealed stores; `SEALED_STORES`
  inventories all five sealed stores (provider_credentials supported;
  integrations / workflows / agent-runs / Baileys manual, per payload).
- New `scripts/rotate_master_key.py`: `--dry-run` (default — counts only)
  / `--confirm` sweep CLI; refuses to run unless a previous key is
  configured. New `docs/step-i-key-rotation-runbook.md`: exact master-key
  rotation steps (backup → new key → both keys deployed → sweep → verify
  0 stale → drop retired key) with rollback, plus the database-credential
  rotation the owner must do (new role, repoint `DATABASE_URL`, revoke the
  old role). **Nothing was rotated by this slice** — rotation is an
  owner action; the slice only makes it safe.
- 13 new tests (`tests/test_step_i_key_rotation.py`): pure cipher tests
  (retired-key decrypt, unknown-id refusal, no-op re-encrypt, sealed-JSON
  round-trip + tamper refusal, Settings pair validation, builder with and
  without a previous key, store inventory) and database sweep tests
  (re-encrypt + audit + decrypt-under-new-key, second sweep is a no-op,
  undecryptable row skipped and untouched, retired rows ignored).

## 6d. Slices 6–7 — what was implemented (this branch)

### Slice 6 — security headers + Teams bot auth + status leak fix (commit `000b970`)
- `app/main.py` (`request_context` middleware) now sets
  `Permissions-Policy: camera=(), microphone=(), geolocation=(),
  payment=(), usb=()` on every response, and
  `Strict-Transport-Security: max-age=31536000; includeSubDomains` only
  when `settings.hsts_enabled` (new setting in `app/core/config.py`,
  default `false`, documented in `.env.example`). HSTS stays OFF until
  the owner approves it (§7) — enabling it on a live surface without
  that decision would be a breaking change.
- New `app/security/teams_auth.py`: Bot Framework JWT validation using
  only `cryptography` (RS256) + stdlib (no PyJWT in the lockfile).
  `verify_teams_bearer_token()` checks issuer exactly
  `https://api.botframework.com`, audience exactly `teams_app_id`,
  expiry/`nbf`, and refreshes the JWKS once on an unknown `kid`
  (cached 1h). Verification proves the caller is Bot Framework for this
  app id; the activity body is still treated as untrusted data and is
  never acted on.
- `POST /teams/messages`: Teams disabled or no app id → 503 (unchanged);
  missing/invalid token → 401; verified token → 200 acknowledge-only
  (`{"status": "received", "answer_available": false}`) — mirrors the
  verified Slack webhook behaviour.
- `GET /api/integrations/status` no longer leaks configuration: it
  returns only `enabled` flags plus
  `bot_framework_authentication_available: True`. The old
  `has_signing_secret` / `has_app_id` presence booleans are gone.
- 16 new tests (`tests/test_step_i_security_headers.py`), all passed:
  2 header tests, 10 Teams pure tests (RSA key generated in-test, no
  network), 3 route tests, 1 status test.

### Slice 7 — dependency / vulnerability scan gate (uncommitted at plan-write time)
- New `app/security/dependency_audit.py` (stdlib only): `Finding` /
  `AllowEntry` / `AuditResult` dataclasses; `parse_pip_audit()` (pip-audit
  JSON) and `parse_npm_audit()` (npm audit JSON, GHSA ids +
  `fixAvailable.version`); `load_allowlist()` (malformed allowlist raises
  `ValueError` — fail closed) and `apply_allowlist()` (exact
  ecosystem+package+id match; expired entries are reported separately
  and do NOT allow).
- New `scripts/security_dependency_audit.py`: runs `pip-audit -r
  requirements.lock --format json` and `npm audit --json` in `frontend/`.
  Exit 0 clean/all-allowlisted, 1 unallowlisted findings or expired
  entries, 2 a scanner could not run (fail closed). The script loads the
  audit module directly from its file so it runs with any Python that has
  pip-audit (the CI job installs only pip-audit, not the app's
  dependencies), and it moves the scanners' temp dir under the user's
  home when /tmp has under 1 GB free (this VM's /tmp is a 512 MB tmpfs
  and pip-audit's scratch venv otherwise fails with "No space left").
- `docs/security-audit-allowlist.json`: `{"allowlist":[]}` — empty on
  purpose. Nothing is excepted silently; an entry needs a reason and an
  expiry.
- `.github/workflows/ci.yml`: new `security-audit` job (python 3.13,
  node 22, `pip install pip-audit`, runs the script). It is a real gate:
  it fails while unallowlisted findings exist.
- 9 new pure tests (`tests/unit/test_step_i_dependency_audit.py`), all
  passed: both parsers, allowlist load/apply/expiry, and the exact
  allowlist-match rule.
- First real scan (2026-11-28, pip-audit 2.10.1 + npm audit, this VM):
  **63 findings, all pip, 0 npm.** pillow 42 (fixed in 13.0.0), pypdf
  14 (fixed in 6.21.0–6.21.4), diffusers 5 (fixed in 0.38.0),
  opentelemetry-api 1 (fixed in 1.45.2), soupsieve 1 (fixed in 4.0.0).
  The upgrades are NOT done by this slice — pillow 13 and soupsieve 4
  are major versions and diffusers 0.38 may pull newer transitive deps,
  so the lockfile upgrade is follow-up work with its own verification
  (it must not ship unverified, and the full suite cannot complete on
  this VM). The gate therefore fails today, honestly, until that
  follow-up lands or entries are allowlisted with reason + expiry.

  **Follow-up DONE (Agent 174, 2026-11-28):** pillow 12.3.0 → 13.0.0,
  pypdf 6.20.1 → 6.21.4, diffusers 0.35.2 → 0.38.0,
  opentelemetry-api 1.45.1 → 1.45.2, soupsieve 3.0.4 → 4.0 (hashes
  regenerated in `requirements.lock`; `.tools/torch-cpu-constraints.txt`
  recreated — the old lock referenced it but the repo never contained
  it). The pillow upgrade forced the docling family up as well
  (docling 2.140.0 → 2.154.0, docling-core 2.105.0 → 2.108.0,
  docling-parse 7.24.1 → 7.28.0, docling-slim 2.140.0 → 2.154.0,
  doclang 0.8.0 → 0.10.1) because docling-slim 2.140 caps pillow
  below 13. Re-scan: **0 findings, exit 0** — the gate passes with the
  allowlist still empty. Focused tests 52 passed, 0 failed (chat
  images 2, image-edit provider 1, RAG-units PDF subset 19, docling
  ingestion 13 + isolated docx 1 at `RAG_PARSER_MEMORY_MB=8192`,
  image documents 7, audit-gate file 9). Two environment findings
  carried forward: docling 2.154 needs the `docling-layout-heron`
  model (172 MB) cached on a fresh environment, and the isolated
  parser's 4096 MB default cap is below docling's measured 4.33 GB
  virtual footprint (raise `RAG_PARSER_MEMORY_MB` is a separate
  decision, not part of this upgrade).

## 7. Owner inputs needed

- Rotate the DB credential exposed in older docs (handover §5.9) — runbook:
  `docs/step-i-key-rotation-runbook.md` §3 (new role → repoint `DATABASE_URL`
  → revoke old role). No code can do this; it is an owner action.
- Rotate the master encryption key when ready — runbook §2
  (`docs/step-i-key-rotation-runbook.md`); the code support is in place
  (slice 5), the owner generates the new key and runs the sweep.
- Confirm session TTL / idle timeout and whether org-wide MFA is required.
- Confirm Teams bot auth approach (Bot Framework JWT) or accept it staying
  refused.
- Approve HSTS (requires HTTPS in front of the deployment).
- Later decision (recorded, not done in slice 4): narrow the admin bridge
  (`platform.manage` for legacy admins) so the platform gate enforces
  beyond the legacy decision.

## 8. Safety guarantees for this branch

- Slice 2 adds a throttle, not an auth change: no previously allowed
  request is denied below the published limits; health/readiness are
  exempt; administrators are exempt from the per-user bucket; limiting is
  off in `APP_ENV=testing`.
- Slice 3 changes no authorized behaviour: the platform gate can only take
  access away in enforce mode, and the admin bridge grants `platform.manage`
  to active legacy administrators, so authorized admins see identical
  responses (verified by the negative-authorization tests).
- No live folder, live DB, `.env`, uploads, or data touched. No secrets
  printed or committed. Tests only on throwaway `*_test` databases.
- Slice 4 changes no authorized behaviour: the platform gate can only take
  access away in enforce mode, and the admin bridge grants
  `platform.manage` to active legacy administrators, so authorized admins
  see identical responses (verified by the 403/gate tests). The four new
  audit events are append-only records; the quota audit is the only record
  of an in-memory change.
- Slice 5 rotates nothing by itself and changes no decryption behaviour
  without a previous key configured: the rotating cipher is a subclass of
  the single-key cipher, every construction site builds it the same way,
  and a bad previous-key pair refuses startup instead of starting half
  configured. The sweep never deletes a row, never logs a secret, and
  leaves undecryptable rows untouched.
