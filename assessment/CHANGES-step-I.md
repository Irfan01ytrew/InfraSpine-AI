# Step I — Security hardening: what changed

Plain-language summary of the work in this branch (`agent/stepi-security`,
base `step-e-gaps-wip` `ca956c2`). Slices 1–3 were built earlier on this
branch; slices 4–5 by Agent 80; slices 6–8 by Agent 135 (this session).

## The problem

Before this step, InfraSpine had good security pieces but no proof they
all held together:

- Some routes were protected by a legacy "admin" check instead of the
  platform's permission system, so an administrator's rights depended on
  which door they used.
- Nobody had measured whether every privileged admin action left an audit
  trail — four actions did not.
- The master key that encrypts stored provider credentials could not be
  rotated at all: there was no second key, no sweep, no runbook.
- Responses carried no security headers, and the Teams bot endpoint
  either refused everything or trusted nothing — there was no way to
  accept a real Teams message safely.
- The integrations status page told anyone who could see it whether a
  signing secret or app id was configured — information an attacker can
  use.
- Nobody had ever run a vulnerability scan over the project's
  dependencies, so known-vulnerable libraries could sit in the lockfile
  unnoticed.

## What this step adds

**Route protection, measured (slices 1–3).** Every route is either
authenticated or on an exact allowlist, proven by a test that walks the
live app (558/580 routes carry a session/token dependency; the other 22
are accounted for one by one). Per-route, per-user and per-IP rate
limits (defaults 600/600/30/120 — see the owner decisions below) with a
429 + `Retry-After` answer, and the admin platform gate: privileged
admin routes go through `org_gate("platform.manage")` instead of the
legacy admin check.

**Audit coverage, measured (slice 4).** A machine-readable inventory of
all 70 state-changing `/api/admin/*` routes, with a coverage measurement
of **1.0** — every one of them emits at least one declared audit event.
Four actions that had no audit event before now do: testing an
embedding model, testing an OCR engine, testing a document parser, and
changing a department quota.

**Master-key rotation (slice 5).** The encryption cipher now reads with
the current key **or** a retired key and encrypts only with the current
key, so a key can be replaced without losing anything. A sweep script
re-encrypts stored provider credentials under the new key, counts what
is stale, skips anything it cannot decrypt (never deletes or guesses),
and a runbook walks the owner through backup → new key → both keys
deployed → sweep → verify zero stale → drop the retired key. **Nothing
was rotated by this step** — rotation itself is an owner action; this
step only makes it safe. A separate runbook section covers the database
credential that was exposed in older docs: the owner must rotate that
one (new role, repoint `DATABASE_URL`, revoke the old role).

**Security headers and Teams (slice 6).** Every response now carries a
`Permissions-Policy` header that switches off the camera, microphone,
geolocation, payment and USB features the app never uses. HSTS (the
header that tells browsers to only ever use HTTPS) is implemented but
**off by default** — turning it on for a live address is an owner
decision (see below). Teams messages are now accepted safely: the Bot
Framework JWT is verified (issuer, audience = this app id, expiry,
signing key), and a verified message is only acknowledged — the app
never acts on a Teams activity. A message with a missing or bad token
gets a 401; Teams turned off gets a 503. The integrations status page
no longer reveals whether secrets are configured.

**Dependency vulnerability gate (slice 7).** `pip-audit` (Python) and
`npm audit` (frontend) now run as a gate in CI and from a script
(`scripts/security_dependency_audit.py`). The first real scan found **63
vulnerable Python packages and 0 vulnerable frontend packages**:
pillow 42 findings (fixed in 13.0.0), pypdf 14 (fixed in 6.21.0–
6.21.4), diffusers 5 (fixed in 0.38.0), opentelemetry-api 1 (fixed in
1.45.2), soupsieve 1 (fixed in 4.0.0). The gate fails until those are
fixed or explicitly allowlisted with a reason and an expiry date. The
lockfile upgrades are **not** part of this step: pillow 13 and soupsieve
4 are major versions and diffusers 0.38 may pull newer libraries, so
the upgrade needs its own verified change — it must not ship
unverified. The allowlist file is deliberately empty today.

**Release drills (slice 8).** The runbooks were exercised on throwaway
databases, not on live data: the key-rotation script's dry run
(`current_key_id=v1 stale_credentials=0`, nothing changed); a
backup-restore drill (`pg_dump` → restore into a fresh database →
both databases have 198 tables and the same schema version); and a
rollback drill (schema downgrade one step, then upgrade back to the
head, `alembic check` clean).

## Files changed (whole step)

- `app/security/` — rate limiter + middleware, audit-coverage
  inventory, rotating cipher support, key-rotation sweep, Teams JWT
  validation, dependency-audit parsers.
- `app/core/config.py`, `app/main.py` — rate-limit / HSTS / key-pair
  settings; security headers in the request middleware.
- `app/api/routes/collaboration.py` — Teams endpoint auth, status
  page leak fix.
- `app/api/deps.py` and 11 admin routers — platform-gate migration.
- `scripts/rotate_master_key.py`, `scripts/security_dependency_audit.py`
  — the two operator scripts.
- `docs/step-i-security-plan.md` (§6b–6d: what each slice built),
  `docs/step-i-key-rotation-runbook.md`,
  `docs/security-audit-allowlist.json` (empty).
- `.github/workflows/ci.yml` — `security-audit` job.
- `tests/test_step_i_*.py`, `tests/test_agent_run_retention_*.py`,
  `tests/test_deployment_*.py`, `tests/unit/test_step_i_dependency_audit.py`
  — the Step I test files (see below).

No migration was added by slices 6–8. The branch head is
`0103_unified_studio_governance` (unchanged).

## Tests

New tests added by this step (slices 1–8): authorization coverage 9,
rate limiting 17, admin platform gate 3, audit coverage 21, key
rotation 13, security headers 16, dependency audit 9 — **88 new tests**.

Regression re-run owed from slice 5 (Agent 80's runs were killed three
times by VM restarts / out-of-memory, with zero code failures): the
51-test regression set plus the slice-4 file, re-run by Agent 135 on
the dedicated test cluster (see Environment notes). Results:

- `test_step_i_authz_coverage` — 9 passed (2226.54s).
- `test_step_i_admin_platform_gate` — 3 passed (1387.02s).
- `test_agent_run_retention_postgres` — 1 passed (491.66s).
- `test_agent_run_retention_unit` — 4 passed (1225.69s).
- `test_deployment_licensing` — 12 passed (2069.18s).
- `test_deployment_templates` — 5 passed (1089.20s).
- `test_step_i_audit_coverage` (slice 4 file) — 21 passed (2765.57s).
- `test_step_i_rate_limit` — 17 passed (1772.22s).

That is the full set: **72 passed, 0 failed** (the 51-test regression
set plus the 21-test slice-4 file), across three driver parts — part 1
(files 1–2 green, rate-limit file OOM-killed), part 2 (files 3–6
green, audit-coverage file killed at 3/21 by the second VM restart),
part 3 (audit-coverage 21 passed, rate-limit 17 passed).

Slice-6 file on its own run: 16 passed (2935.31s). Slice-7 pure file:
9 passed (23.33s).

## Gates

- Alembic: upgrade → downgrade one step → upgrade to head →
  `alembic check` clean (`No new upgrade operations detected`), all on
  the throwaway drill database. No migration in slices 6–8.
- Frontend: unchanged by slices 6–8 (no frontend files touched), so no
  frontend gate was run here; the frontend suite last passed on the
  Step E verification (22 tests, typecheck + build clean).
- Full backend suite: **not** run on this branch. The shared VM cannot
  complete it right now (see Environment notes) — the Step E
  verification gate owns the full-suite verdict, and this branch must
  not merge before that verdict is green.
- Dependency gate: the `security-audit` CI job currently **fails** with
  the 63 findings listed above. That is the gate working, not a false
  alarm — the findings are real and the fix is the lockfile upgrade
  follow-up.

## Environment notes (this VM)

All Step I test runs in this session used a private PostgreSQL 16
cluster at `/home/hatch/.pg-agent80/cluster`, port **55447**, role
`infraspine_agent80`, databases `infraspine_stepi` (app),
`infraspine_stepi_test` (regressions),
`infraspine_stepi5_test` (slice-5 tests) and `infraspine_stepi6_test`
(slice-6 tests). The password lives in `/home/hatch/.agent80-db-pw`
(never committed, never printed). The VM restarted twice during this session: ~05:15 UTC and ~07:15 UTC.
Each restart wiped the `agent80` system user (recreated with the same
uid, 1004, so the cluster files still belong to it), stopped the
cluster, and killed the jobs that were running. After the first
restart the cluster was restarted and the three dead jobs were
relaunched (regression driver part 2, slice-6 file run, dependency
scan); after the second restart the cluster was restarted again
(`/var/run/postgresql` was also recreated, postgres:postgres 2775)
and the driver was relaunched as part 3. The regression driver was
restarted as part 2 (files 1–2 already green in part 1 were not
repeated); the rate-limit file was OOM-killed once (rc=137) in part 1
and retried in part 3, where it passed 17/17. The VM's load average
sat at 25–65 for most of this session with ~40 agents sharing ~1.5
CPUs and 8 GB RAM, which is why single test files take 20–50 minutes
here. One environment quirk found by slice 7: this VM's /tmp is a
512 MB tmpfs, so `pip-audit`'s scratch virtualenv fails there with
"No space left on device" — the audit script moves the scanners' temp
dir under the user's home when /tmp has under 1 GB free.

## Known limits / open items

- The 63 dependency findings are unfixed (see above) — the lockfile
  upgrade is the immediate follow-up, and the `security-audit` CI job
  stays red until it lands.
- The full backend suite was not run on this branch (see Gates).
- HSTS is implemented but off by default (owner decision, below).
- The legacy admin bridge is **not** narrowed: privileged admin routes
  still accept the legacy admin check as well as the platform gate.
  Narrowing it is a later owner decision (below) — the enforcement
  value lands fully when it is narrowed.
- The exposed database credential from older docs is not rotated by
  this step (owner-only action, runbook §3).

## Owner decisions needed (proposed defaults, not yet answered)

1. **Rate-limit defaults:** 600/min per route, 600/min per user,
   30/min per IP, 120/min for webhooks (already implemented as the
   defaults; the owner only needs to confirm or change the numbers).
2. **Session lifetime + org-wide MFA:** how long a session may live,
   how long it may sit idle, and whether MFA is required for everyone
   in an organisation (not implemented yet — needs the decision first).
3. **Teams bot approach:** Bot Framework JWT validation is implemented
   and the endpoint only acknowledges messages. If the owner wants the
   bot to actually answer from the Knowledge base, that is a new slice
   (Step AE owns the WhatsApp side; Teams answering would mirror it).
4. **HSTS approval:** turn on `HSTS_ENABLED=true` on the live address
   once the site is served over HTTPS everywhere. Until then it stays
   off — turning it on for a plain-HTTP address would break the site.
5. **Exposed database credential rotation:** rotate the credential
   that appeared in older docs (runbook §3 — owner only: new role,
   repoint `DATABASE_URL`, revoke the old role).
6. **Master-key rotation timing:** when to rotate the live master key
   (runbook §2 — the owner generates the new key and runs the sweep;
   nothing was rotated by this step).
7. **Admin-bridge narrowing:** when to stop accepting the legacy admin
   check on privileged admin routes, so the platform gate is the only
   way in.
