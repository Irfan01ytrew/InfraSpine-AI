#!/bin/sh
set -eu
# Bind-mounted data/log dirs are often root-owned on the host. When the
# container is started as root, fix ownership and drop to uid 10001.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /var/lib/infraspine/data /var/lib/infraspine/logs
  chown -R infraspine:infraspine /var/lib/infraspine/data /var/lib/infraspine/logs || true
  if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid=10001 --regid=10001 --init-groups -- "$0" "$@"
  fi
  echo "start the container as uid 10001, or install setpriv, so it can drop privileges after chown."
  exit 2
fi
role=${1:-api}
shift || true
case "$role" in
  api)
    workers=${API_WORKERS:-1}
    server=${API_SERVER:-uvicorn}
    if [ "$server" = "gunicorn" ]; then
      exec gunicorn app.main:app         --worker-class uvicorn.workers.UvicornWorker         --bind 0.0.0.0:8000         --workers "$workers"         --timeout "${API_TIMEOUT:-300}"         --access-logfile -         --error-logfile -         "$@"
    fi
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "$workers" "$@"
    ;;
  worker)
    exec python -m app.worker "$@"
    ;;
  migrate)
    exec alembic upgrade head "$@"
    ;;
  cli)
    exec python -m app.cli "$@"
    ;;
  *)
    exec "$role" "$@"
    ;;
esac
