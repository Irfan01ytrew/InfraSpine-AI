# Zero-downtime migrations (Step AO)

Migrations change the database while the old code is still running.
A migration that renames or drops a column the running code still
reads takes the app down. These rules keep deploys boring.

## 1. Expand, then contract

Never change a shape in one step. Do it in two (or three) migrations,
with a deploy between them:

1. **Expand** — add the new shape without removing the old one: a
   nullable column, a new table, a new enum value. The old code keeps
   working because nothing it reads has changed.
2. **Migrate the code** — deploy code that writes both shapes (dual
   write) and reads the new one, falling back to the old one.
3. **Backfill** — copy old rows into the new shape in batches (see §3).
4. **Contract** — in a *later* migration, drop the old column or
   table, once no running code reads it.

## 2. Never do this in a migration

- Rename or drop a column the running code still reads.
- Drop a table the running code still reads.
- Add a `NOT NULL` column with no default to a table that has rows
  (Postgres rewrites the table; on a big table it locks it for the
  whole rewrite). Add it nullable, backfill, then add the constraint.
- Add a foreign key as `VALID` on a big table in one step: the
  validation scan locks the table. Add it `NOT VALID`, then validate
  in a second step (`ALTER TABLE ... VALIDATE CONSTRAINT`), which
  takes a lighter lock. Step AO's integrity checker reports any
  `NOT VALID` constraint left unvalidated — that is a finding, not
  a state to ship.
- Change a column type in place (`USING` casts) on a table the app
  writes to: it takes an exclusive lock for the rewrite.

## 3. Long backfills run in batches

A backfill that touches every row in one transaction holds its locks
until the end and bloats the WAL. Batch it (a few thousand rows per
batch), commit between batches, pause briefly so replicas and other
queries can breathe, and make the batch re-runnable (skip rows that
already have the new value) so a killed backfill resumes instead of
restarting.

## 4. Backup before migrate

`db-migrate-safe` is the only way migrations run on a deployment.
It refuses (exit code 2) when no drill-verified backup newer than
the last migration exists, or when the newest verified backup is
older than the RPO (24h by default). See `app/ops/migration_safety.py`.
`--check-only` evaluates the gate without migrating. A downgrade
that would destroy data needs a named owner decision recorded in the
migration file itself — the wrapper does not invent one.

## 5. Downgrades are tested, not assumed

Every migration's downgrade is tested on a throwaway database
(upgrade → downgrade → upgrade) before merge. A migration whose
downgrade cannot run — a dropped column with data in it, a type
change that loses values — says so in its file, and the deploy
plan treats the migration as one-way.
