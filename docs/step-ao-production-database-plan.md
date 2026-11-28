# Step AO — Production-Grade Database: implementation plan

Status: **preparation + first slice** (branch `agent/database-hardening`,
base `step-e-gaps-wip` `ca956c2`). Step AO is a NEW step ordered by the
owner (2026-11-28): "best level ki top-grade database banao, strong, har
cheez best se best honi chahiye". It is the **database hardening layer
under everything else** — it does not build a second database, a second
backup tool, or a second health endpoint. It makes the one PostgreSQL
database the whole platform already runs on provably safe, fast, and
observable.

Sources: the owner's order (2026-11-28 ~13:33 PKT), `MASTER-PLAN.md`
(Step AO added by the Master Agent), and the code survey in §1 (all paths
relative to the repo root, read — not guessed).

## 1. What exists today (surveyed, 2026-11-28)

The database is not starting from zero. Slice 1 builds on this, it does
not redo it.

1. **Backups exist, restores are guarded.** `app/ops/postgres_backup.py`
   already has `create_backup` (pg_dump custom format, `--no-owner`
   `--no-privileges`, writes to a `.part` file, verifies with
   `pg_restore --list`, renames into place, returns a SHA-256 receipt)
   and `restore_into_empty_database` (verifies first, then refuses any
   database that already has user tables — it never drops or cleans
   existing data). CLI: `app/cli.py` wires `db backup`, `db verify`,
   `db restore`. `app/ops/deployment_snapshot.py` uses `verify_backup`
   before trusting a snapshot. What does NOT exist: any schedule that
   runs a backup, any record of which backups exist, any restore drill
   that proves a backup actually restores, WAL archiving / PITR, or a
   retention policy.
2. **Pooling exists, tuning is default.** `app/db/session.py`
   `create_engine` uses SQLAlchemy async pooling: `pool_size=5`,
   `max_overflow=10`, `pool_timeout=30`, `pool_recycle=1800`,
   `pool_pre_ping=True`, `pool_use_lifo=True`, `hide_parameters=True`
   (bound values stay out of logs). Settings live in
   `app/core/config.py` (`database_pool_size`,
   `database_pool_max_overflow`, `database_pool_recycle_seconds`).
   `app/main.py` passes the settings through. What does NOT exist:
   any measurement of whether 5+10 is right for the real workload, a
   PgBouncer option, a slow-query report, or an index audit.
3. **Health exists, database detail is thin.** `GET /health` is
   liveness only (no database call). `GET /health/ready`
   (`app/api/routes/health.py`) runs `SELECT 1` and reports the
   document-ingestion worker count. That is the whole database signal:
   up or down. There is no view of connections, locks, bloat, disk,
   replication, or slow queries.
4. **Migrations exist, safety is by convention.** Alembic head on this
   branch is `0103_unified_studio_governance`. There is no
   backup-before-migrate rule enforced anywhere, no zero-downtime
   migration guideline, and no downgrade-safety check beyond Alembic
   itself.
5. **Live reality.** The live demo database runs in a GitHub Codespace
   (PostgreSQL 16 main cluster, single node, no automated backup, no
   WAL archiving, no monitoring beyond `/health/ready`). The owner's
   PC live database must NEVER be touched by any of this work. All
   development and testing happens on throwaway `*_test` databases on
   this VM only.

## 2. What Step AO must deliver (acceptance)

Plain language: the database must survive the worst day — a deleted
database, a bad migration, a runaway query, a stolen credential — and
prove it can, before that day happens.

1. **A backup that was never restore-tested does not count.**
   Every backup gets a restore drill: restore into an empty throwaway
   database, count tables and rows, compare checksums. Only a drill
   that passes marks the backup "verified". A drill that fails is an
   alert, not a footnote.
2. **Point-in-time recovery is designed, not assumed.** WAL
   archiving / PITR design with a stated RPO (how much data loss is
   acceptable) and RTO (how fast a restore must finish), both chosen
   by the owner (see §9).
3. **The pool fits the workload.** Pool settings are justified by
   measured connection counts and wait times, not by defaults. A
   PgBouncer configuration is provided as an option for when one
   process is not enough.
4. **Slow queries are found, not felt.** `pg_stat_statements` reporting
   names the slowest queries with evidence; index audit names missing
   indexes and unused indexes; fixes are EXPLAIN-based.
5. **Least privilege, everywhere.** Each service connects with a role
   that can do only what that service needs. Credential rotation is
   supported (rotate without downtime). No secret appears in code,
   logs, or backups' metadata.
6. **Health is visible before it is an outage.** `/health/ready`
   grows a database section (connections, locks, bloat, disk,
   replication lag when a replica exists) with thresholds that turn
   "ready" into a warning before the database stops answering.
7. **Migrations never run unprotected.** Backup-before-migrate is the
   rule; zero-downtime guidelines (expand-then-contract) are written
   down; a downgrade is proven safe or explicitly refused.
8. **Integrity is checked, not hoped for.** Foreign keys are
   validated, orphan rows are detected, and a periodic integrity
   check runs on a schedule and reports.

## 3. Backups + verified restores (slice 2)

- **Schedule.** Daily full backup (pg_dump custom format, the existing
  `create_backup`), kept per the retention policy in §9. WAL archiving
  design (continuous archiving to a second location) for PITR; the
  Codespace demo cannot archive WAL to itself — the archive target
  must be a different place than the database (see §9: storage
  location is an owner decision).
- **Receipts.** Every backup writes a receipt: database, path, bytes,
  SHA-256, catalog entries, created time, `verified` flag. Receipts
  are the source of truth for "do we have a backup", not the file
  listing. Slice 1 builds the pure receipt-validation helpers.
- **Restore drills.** A drill restores a backup into an empty
  throwaway database (the existing
  `restore_into_empty_database` already refuses non-empty targets),
  then compares table counts and row counts against the receipt and
  the source. Verdict is pass/fail with named failures. A drill never
  touches a live database and never runs automatically against a
  restore target that holds data. Proven pattern to reuse: Agent 135
  (Step I, 2026-11-28) ran a backup-restore drill on throwaway DBs —
  pg_dump 904,741 bytes, restore rc=0, both databases 198 tables,
  Alembic 0103 — as the verified-restore template.
- **Restores are human-approved, always.** A restore is a destructive
  operation: it needs an action-bound platform approval, single-use,
  with separation of duties (the person who approves is not the
  person who executes). No scheduled job, no agent, and no drill ever
  restores production on its own.
- **Retention (safe defaults, owner to confirm).** Daily backups kept
  30 days, weekly backups kept 12 weeks, monthly backups kept 12
  months. Pruning deletes only backups older than the policy AND
  never deletes the newest verified backup for a database.

## 4. Connection pooling + performance (slice 3)

- **Measure first.** Collect connection counts (total / active / idle
  in transaction), pool wait time, and the longest-running query
  before changing any number. Slice 1 builds the collectors.
- **Pool tuning.** Justify `pool_size` / `max_overflow` /
  `pool_recycle` from the measurements; record the reasoning in the
  plan doc, not just the numbers. Keep `pool_pre_ping` and
  `hide_parameters`.
  *Measured (slice 3, throwaway cluster, 2026-11-28): a 3-query
  concurrent burst held 3 connections of the 5+10 budget (band ok);
  the tuner recommends **keep** at 5/10 for a single process — the
  peak fits with headroom and overflow was never used. The rule is now
  code (`pool_tuning_recommendation`): overflow used → increase toward
  peak + 25% headroom; peak below half the pool → decrease; otherwise
  keep. Re-measure on the live workload before changing any number.*
- **PgBouncer option.** Provide a tested PgBouncer configuration
  (transaction pooling mode, matching pool sizes, `server_reset_query`
  discipline) as an option for deployments with more than one API
  process. It is an option, not a forced change: the Codespace demo
  runs one process and does not need it today.
- **Slow queries.** `pg_stat_statements` must be enabled where the
  deployment allows it; where it cannot be (managed database without
  the extension), the collector reports "unavailable" honestly instead
  of inventing numbers. Report: top queries by total time with mean
  time and call count; a query is "slow" only against a stated
  threshold (default: mean > 100 ms warns, > 1000 ms is critical).
- **Index audit.** Two read-only reports: candidate missing indexes
  (sequential scans on large tables with a selective predicate — from
  `pg_stat_user_tables` + `pg_stat_statements`, never a guess) and
  unused indexes (zero scans since statistics reset, excluding unique
  indexes that enforce constraints). Dropping an index is a migration,
  so it follows the migration-safety rules in §6.

## 5. Security hardening (slice 4)

- **Least-privilege roles.** Separate roles: application role (read +
  write on application tables only, no DDL, no superuser), worker
  role (same as application, plus queue tables), read-only reporting
  role (SELECT only), backup role (SELECT + pg_dump rights, no
  writes), migration role (DDL, used only during a migration run).
  The application never connects as a superuser.
- **Credential rotation.** Rotation support: a new password can be set
  and the pool recycled without downtime; the old credential stops
  working after a stated overlap window. Rotation itself is a
  privileged operation (approval-bound, separation of duties).
- **Encryption at rest.** Guidance, not code: full-disk / volume
  encryption on the host, PostgreSQL data-directory permissions
  (0700, owned by the postgres user), and TLS for connections
  (`sslmode` already honoured by `app/ops/postgres_backup.py`).
  Column-level encryption for secrets already exists in the app
  (master key); this step does not re-encrypt anything.
- **No secrets in code or logs.** Existing rule, enforced by review:
  connection strings never appear in logs, error messages, backup
  receipts, or health output. `hide_parameters=True` stays on.

## 6. Health + monitoring (slice 5)

- **Collector.** Read-only queries only, against the live engine:
  database size, connection counts by state, longest-running query
  age, lock count + oldest lock age, dead-tuple ratio per table,
  replication lag when a replica exists (none today — report
  "unavailable", not zero), `pg_stat_statements` availability.
  Slice 1 builds the collector and the pure threshold classifiers.
- **Thresholds (safe defaults).** Connections used: < 70% ok, 70–90%
  warning, > 90% critical. Longest query: < 60 s ok, < 300 s warning,
  ≥ 300 s critical. Dead-tuple ratio: < 10% ok, < 20% warning,
  ≥ 20% critical. Replication lag: < 5 s ok, < 60 s warning,
  ≥ 60 s critical; no replica → "unknown", never "ok".
- **Wiring.** The collector's summary joins `/health/ready` as a
  `database` section, keeping the existing shape (status, database,
  index_role, ingestion) and adding detail — never replacing it, so
  existing monitors keep working. A critical database section makes
  readiness report degraded/unavailable; a warning is reported but
  does not take the app down.
- **Alerts.** Threshold breaches are audit-logged and surfaced to the
  admin UI (slice 6). No alert is sent to a channel the owner has
  not configured.

## 7. Migration safety (slice 6, with slice 1 helpers)

- **Backup-before-migrate.** No migration runs without a fresh
  verified backup of the target database first. The migration
  command refuses (exit non-zero) when no backup receipt newer than
  the last migration exists. This is enforced in the migration
  wrapper, not by memory.
- **Zero-downtime guidelines.** Expand-then-contract: add a nullable
  column / new table first, deploy code that writes both, backfill,
  then remove the old shape in a later migration. No migration
  renames or drops a column the running code still reads. Long
  backfills run in batches with pauses.
- **Downgrade safety.** Every migration's downgrade is tested on a
  throwaway database (upgrade → downgrade → upgrade) before merge.
  A downgrade that would destroy data refuses unless the owner
  explicitly approves it with a named decision recorded in the
  migration file.
- **Renumbering.** Migration numbers are claimed across branches
  (0104 is claimed by many steps; 0105–0110 by others). Any new
  migration in this step mints the next free number only after
  checking every worktree, and the merge note records the renumber
  map. Slice 1 claims NO migration.

## 8. Data integrity (slice 6, with slice 1 helpers)

- **Foreign keys.** Validate every foreign key (no `NOT VALID`
  constraints left unvalidated) and report any constraint that is
  not trusted.
- **Orphan detection.** Read-only queries that find child rows whose
  parent is missing, per foreign key. Orphans are reported, never
  silently deleted; deletion is a human-approved data fix.
- **Periodic checks.** An integrity check runs on a schedule (weekly
  default), writes a report (tables checked, orphans found, FK
  status), and fails loudly when anything is found. Slice 1 builds
  the pure verdict helpers the checker uses.

## 9. Slices

1. **Slice 1 — pure foundations (this task, Agent 173).** Plan doc
   (this file) + pure backup-verification helpers
   (`app/ops/backup_verify.py`: receipt validation, freshness bands,
   prune plan, restore-drill verdict) + DB health-check functions
   (`app/ops/db_health.py`: pure threshold classifiers + a read-only
   async collector against a throwaway `*_test` database). No
   migration. No API change. No scheduled job.
2. **Slice 2 — backup scheduler + restore drills.** Schedule that
   runs `create_backup`, writes receipts, runs restore drills into
   empty throwaway databases, records drill verdicts, alerts on
   failure. Restore execution stays human-approved.
3. **Slice 3 — pooling + performance (Agent 175, DONE).**
   `app/ops/db_performance.py`: pool stats from the engine's own pool,
   `pool_tuning_recommendation` (keep/increase/decrease with reasons),
   `collect_slow_queries` (pg_stat_statements when present, honest
   unavailable when not), `index_audit` (unused indexes + seq-scan
   candidates labelled "confirm with EXPLAIN"), `validate_pgbouncer_config`
   + `docs/pgbouncer.example.ini` (transaction mode, DISCARD ALL,
   20/100). Measured: 3-query burst holds 3 of 5+10 → keep. Tests: 13
   passed. Real bug fixed: `pg_stat_user_indexes` has `relname`, not
   `tablename`. Test note: pg_stat_* counters flush asynchronously —
   the audit test polls until the scans land (up to 10 s).
4. **Slice 4 — security hardening (Agent 175, DONE).** Least-privilege
   roles (`app/ops/db_roles.py`: five roles, validated names, quoted
   identifiers, measured grants, SCRAM rotation with verifier-change
   proof, `drop_roles` revokes first) + secrets audit
   (`app/ops/secrets_audit.py`: findings never record the matched
   secret). Tests: 18 passed. Real bugs fixed: audit context leaked the
   password (first 40 chars of the match); DROP ROLE fails while grants
   exist; trust-auth cluster cannot prove old-password rejection by
   login (verifier change is the proof).
5. **Slice 5 — health + monitoring in `/health/ready` (Agent 175, DONE).**
   `database_detail` from `collect_db_health` alongside the kept
   `"database": "ok"` string; critical → 503 degraded, warning reported
   but stays ready, missing signals unknown, breaches logged as
   structured lines. Collector runs defensively: when it cannot run,
   the response is byte-identical to the pre-AO shape, so the existing
   readiness contract tests pass unchanged. Tests: 4 new passed + 2
   existing readiness tests still green.
6. **Slice 6 FINAL — migration safety + integrity + UI (Agent 175, DONE).**
   Backup-before-migrate gate (`app/ops/migration_safety.py` +
   `db-migrate-safe` CLI, `--check-only`, exit 2 on refusal),
   zero-downtime guide (`docs/step-ao-zero-downtime-migrations.md`),
   integrity checker (`app/ops/db_integrity.py`: FK trust flags +
   per-FK orphan counts, read-only, orphans reported never deleted),
   admin database page (read-only: backups, drills, health, integrity,
   gate; restore stays human-approved CLI) + `GET /api/admin/database/summary`
   (admin-only) + `assessment/CHANGES-step-AO.md` with zero `[PENDING]`.
   Tests: 15 new passed; 93 passed across slices 1–6 in one run;
   Alembic check clean (single head 0114); frontend 22 passed, tsc +
   build clean.

## 10. Gates

- Pure tests: every helper has tests; 0 failed.
- DB tests: collector runs against a throwaway `*_test` database;
  read-only proof (row counts identical before/after); 0 failed.
- Alembic: `alembic check` clean; upgrade → downgrade → upgrade on
  a throwaway database when a migration is added (none in slice 1).
- No test ever touches the live Codespace database or the owner's PC
  database. No secret in any log, receipt, or commit.
- Full backend suite on a quiet VM before merge (load control — the
  Step E verification pass is running while slices are built).

## 11. Owner decisions still open

1. **Backup retention period.** Safe defaults in §3 (30 days daily /
   12 weeks weekly / 12 months monthly). Owner to confirm or change.
2. **Backup storage location.** Backups must live somewhere other
   than the database host (a backup on the same disk as the database
   is not a backup). Options: GitHub Releases / object storage /
   a second machine. Owner to choose; nothing is uploaded anywhere
   until they do.
3. **RPO / RTO.** How much data loss is acceptable (RPO) and how fast
   a restore must finish (RTO). Defaults proposed: RPO 24 hours
   (daily backup) until WAL archiving lands, RTO 1 hour for the demo
   database. Owner to confirm.
4. **Pooling mode.** Stay with SQLAlchemy pooling (default) or adopt
   PgBouncer when the deployment grows past one API process. Owner
   to decide when the measurements in slice 3 arrive.
5. **Alert channel.** Where do threshold breaches go — admin UI only
   (default), or also email / WhatsApp when those channels exist.
   Owner to choose.
6. **Restore approver.** Who may approve a restore (owner only is
   the safe default). Owner to confirm.

## 12. Merge notes

- Slice 1 claims NO migration and touches NO existing module: new
  files only (`app/ops/backup_verify.py`, `app/ops/db_health.py`,
  tests, this plan doc). Integration (Agent 161) can merge it
  without conflict.
- Later slices that add a model must mint the next free migration
  number after checking every worktree (0104–0110 are already
  claimed across branches) and record the renumber map here.
- Slice 2 minted `0114_ao_backup_records` (down `0103`), the only
  migration in this step; slices 3–6 add no migration. **0114 collides
  with the integration branch's renumbering** (integration map v5
  reserves 0104–0134 for other steps; the integration branch's head
  was 0119 at the time slice 6 finished) — Agent 161/178 owns the
  renumber map; renumber 0114 at merge.
- `app/cli.py` is mixed CRLF/LF: patch it with `newline=''`
  preservation. `app/core/permission_catalog.py` was not touched.
- The frontend `node_modules` in this worktree is a symlink to the
  frontend worktree's installed tree (lockfiles byte-identical); it is
  gitignored and must not be committed.
- The live Codespace database and the owner's PC database are never
  touched by this step — not by tests, not by drills, not by
  migrations. Drills restore into empty throwaway databases only.
- `app/ops/postgres_backup.py` is reused, not replaced: slice 2
  schedules it; slice 1 only adds verification helpers around its
  receipts.
