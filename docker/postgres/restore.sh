#!/bin/sh
# Restore a custom-format dump into POSTGRES_DB only when the target is empty
# (no user tables in public). Usage: restore.sh /backups/file.dump
set -eu
FILE=${1:?Usage: restore.sh /backups/<file>.dump}
DB=${POSTGRES_DB:?POSTGRES_DB is required}
USER=${POSTGRES_USER:?POSTGRES_USER is required}
HOST=${POSTGRES_HOST:-postgres}
PORT=${POSTGRES_PORT:-5432}
export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

count=$(psql --host="$HOST" --port="$PORT" --username="$USER" --dbname="$DB" -Atc \
  "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'")
if [ "$count" != "0" ]; then
  echo "Refusing to restore: public schema already has $count tables. Use an empty database."
  exit 2
fi
pg_restore --host="$HOST" --port="$PORT" --username="$USER" --dbname="$DB" \
  --no-owner --no-privileges "$FILE"
echo "restore complete"
