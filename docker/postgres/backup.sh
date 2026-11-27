#!/bin/sh
# Periodic custom-format pg_dump into /backups, with retention.
# Usage: backup.sh            # loop (BACKUP_INTERVAL_SECONDS, default 86400)
#        backup.sh once       # single dump, then exit
set -eu
DEST=${BACKUP_DIR:-/backups}
KEEP=${BACKUP_KEEP:-14}
INTERVAL=${BACKUP_INTERVAL_SECONDS:-86400}
DB=${POSTGRES_DB:?POSTGRES_DB is required}
USER=${POSTGRES_USER:?POSTGRES_USER is required}
HOST=${POSTGRES_HOST:-postgres}
PORT=${POSTGRES_PORT:-5432}

dump_once() {
  mkdir -p "$DEST"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  tmp="$DEST/.tmp-${DB}-${stamp}.dump"
  out="$DEST/${DB}-${stamp}.dump"
  echo "starting backup $out"
  PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}" pg_dump \
    --host="$HOST" --port="$PORT" --username="$USER" --dbname="$DB" \
    --format=custom --file="$tmp" --no-owner --no-privileges
  pg_restore --list "$tmp" >/dev/null
  mv "$tmp" "$out"
  echo "wrote $out"
  # prune oldest beyond KEEP
  ls -1t "$DEST"/${DB}-*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
    echo "pruning $old"
    rm -f "$old"
  done
}

if [ "${1:-}" = "once" ]; then
  dump_once
  exit 0
fi

# Stagger the first dump slightly so postgres can finish startup on a fresh volume.
sleep 30
while true; do
  dump_once || echo "backup failed; will retry after ${INTERVAL}s"
  sleep "$INTERVAL"
done
