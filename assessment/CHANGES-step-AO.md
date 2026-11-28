# Step AO — Production-grade database: what changed

Plain-language summary of the work in this branch (`agent/database-hardening`,
base `step-e-gaps-wip` `ca956c2`). The owner asked for "best level ki top-grade
database — strong, har cheez best se best": backups that are proven to restore,
a fast and safe database, locked-down access, health monitoring, and migrations
that cannot run without a backup.

## The problem

Before this step, the database had a backup command but no proof the backups
worked: nobody had ever restored one and compared it. There was no schedule,
no record of which backups existed, no answer to "how old is our newest good
backup", and nothing stopped a migration from running on a database with no
backup at all. Access was one shared superuser-style account, health was a
single "SELECT 1" with no detail, and there was no check for broken foreign
keys or orphan rows.

## What this step adds

**Verified backups (slice 2).** A scheduled backup cycle runs `pg_dump`,
writes a receipt for every attempt (verified, skipped, or failed), then proves
the backup by restoring it into an empty throwaway database and comparing
tables, row counts, and the SHA-256 digest. Only a drill that passes marks
its receipt drill-verified. An empty source database is recorded as a visible
SKIPPED run — never silently skipped, never counted as backed up. A failed
drill is recorded, never hidden. New tables: `ao_backup_receipts` and
`ao_restore_drills` (migration `0121_ao_backup_records`).

**Measured performance (slice 3).** Pool statistics come from the engine's own
pool, with a tuning recommendation (keep / increase / decrease) and reasons.
A 3-query concurrent burst held 3 of the 5+10 budget, so the recommendation
for a single process is "keep 5/10" — re-measure on the live workload before
changing any number. Slow queries come from `pg_stat_statements` when it
exists (honest "unavailable" when it does not). The index audit lists unused
indexes and sequential-scan candidates labelled "confirm with EXPLAIN".
A PgBouncer example config is documented as an option, not a forced change.

**Locked-down access (slice 4).** Five least-privilege roles: app (read+write,
no DDL), worker (same grants, separate credential), reporting (SELECT only),
backup (SELECT only, for pg_dump), migrator (full DDL, migration runs only).
Nobody is a superuser. Role names are validated, identifiers are quoted
(injection strings are doubled, never executed), and passwords must be at
least 16 characters with a letter and a digit. Passwords rotate with a
SCRAM-SHA-256 verifier; the old credential stops working the moment the
verifier changes (on a host that requires password authentication). A secrets
audit scans code, logs, receipts, and health output for database URLs,
passwords, API keys, Bearer tokens, AWS keys, GitHub tokens, and the master
key — and a finding never records the secret it found.

**Health in /health/ready (slice 5).** The readiness endpoint now carries a
`database_detail` section: connections, longest-running query, locks, dead
tuples (bloat), replication, and the pg_stat_statements flag, each with its
band (ok / warning / critical / unknown). A critical overall band returns
503 "degraded"; a warning is reported but does not take the app down. Missing
signals report "unknown", never fake ok. Threshold breaches are logged as
structured lines. The old `"database": "ok"` string is kept, and when the
collector cannot run the response is byte-identical to the old shape.

**Migration safety and integrity (slice 6).** `db-migrate-safe` is the only
way migrations run: it refuses (exit code 2) when no drill-verified backup
newer than the last migration exists, or when the newest verified backup is
older than the RPO (24 hours by default). `--check-only` evaluates the gate
without migrating. The zero-downtime guide documents expand-then-contract:
add nullable columns first, dual-write, backfill in batches, contract later;
never rename or drop a column the running code still reads. The integrity
checker validates every foreign key (no `NOT VALID` constraint left
unvalidated) and counts orphan rows per foreign key, read-only. Orphans are
reported, never silently deleted.

**Admin database page (slice 6).** Administration → Database operations
shows health, the newest backup, the latest restore drill, integrity, and
the migration gate. It is read-only: there is no restore button and no
migrate button. Restores stay a human-approved CLI action
(`db-restore-empty`); migrations stay behind the `db-migrate-safe` gate.

## What was fixed along the way

- SQLAlchemy 2.1.4 `AsyncConnection.execution_options` is a coroutine and
  must be awaited (three sites).
- A raw `AsyncConnection` returns raw column rows for `select(Model)` in
  this SQLAlchemy version; ORM reads need a session.
- Drill database names must be lowercased (PostgreSQL folds unquoted
  identifiers; a timestamp stamp broke the lowercase-only name check).
- Async engines must be disposed in the loop that created them
  ("Event loop is closed").
- `ScalarResult` has `.one()`, not `.scalar_one()`.
- `pg_stat_user_indexes` has `relname`, not `tablename`.
- A secrets-audit finding used to include the first 40 characters of the
  matched text — which contained the password in a database URL. Findings
  now record no matched text at all.
- `DROP ROLE` fails while the role still holds privileges; decommissioning
  a role now revokes first, then drops (never `DROP OWNED BY`, which would
  also delete objects the role owns).
- The throwaway test cluster uses trust authentication, so an old password
  still connects there; rotation is proven by the stored SCRAM verifier
  changing, and old-password rejection is enforced only on a host whose
  `pg_hba` requires password authentication.
- An orphan row cannot be inserted while its foreign key is enforced; the
  test plants one by adding the constraint `NOT VALID`, which also proves
  the checker reports unvalidated constraints.

## Tests and gates

- Backend (focused, small batches — the Step E verification pass was
  running on this VM, so no full suite): **93 passed, 0 failed** in one
  run across slices 1–6 (slice 1: 30; slice 2: 41; slice 3: 13; slice 4:
  18; slice 5: 4 new + 2 existing readiness contract tests; slice 6: 15).
- Alembic: single head `0121_ao_backup_records`; `alembic check` clean;
  upgrade → downgrade → upgrade roundtrip green on a scratch database
  (slice 2).
- Frontend: 22 passed, 0 failed; `tsc --noEmit` clean; `vite build`
  exit 0.
- No test touches the live Codespace database or the owner's PC database.
  No secret in any log, receipt, or commit.

## Still owed (not hidden)

- Full backend suite on a quiet VM before merge (load control — the Step E
  verification pass was running while these slices were built).
- Migration `0114` collides with the integration branch's renumbering
  (Agent 161/178 owns the renumber map; the integration branch's map v5
  already reserves 0104–0134 for other steps).

## Owner decisions still open

1. Backup retention: safe defaults 30 days daily / 12 weeks weekly /
   12 months monthly. Owner to confirm or change.
2. Backup storage location: backups must live off the database host
   (a backup on the same disk as the database is not a backup). Owner
   to choose; nothing is uploaded anywhere until they do.
3. RPO / RTO: proposed 24 hours / 1 hour for the demo database. Owner
   to confirm.
4. Pooling mode: stay with SQLAlchemy pooling, or adopt PgBouncer when
   the deployment grows past one API process. Owner to decide when the
   slice-3 measurements arrive.
5. Alert channel: admin UI only (default), or also email / WhatsApp when
   those channels exist. Owner to choose.
6. Restore approver: owner only is the safe default. Owner to confirm.
