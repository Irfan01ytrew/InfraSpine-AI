#!/bin/sh
# Isolated smoke test against infraspine_ai_test. Never uses live data.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
cd "$ROOT"

if [ -n "${COMPOSE_BIN:-}" ]; then
  DC="$COMPOSE_BIN"
elif docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  DC="docker compose"
elif sudo docker compose version >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  DC="sudo docker compose"
else
  echo "docker compose is not available or the Docker daemon is not running."
  exit 2
fi

SMOKE_DIR="$ROOT/docker-data/smoke"
mkdir -p "$SMOKE_DIR/certs" "$SMOKE_DIR/imports" "$SMOKE_DIR/data" "$SMOKE_DIR/logs" "$SMOKE_DIR/backups"
# The API/worker run as uid 10001; bind-mounted host dirs must be writable by that user.
if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
  sudo chown -R 10001:10001 "$SMOKE_DIR/data" "$SMOKE_DIR/logs" || true
else
  chown -R 10001:10001 "$SMOKE_DIR/data" "$SMOKE_DIR/logs" 2>/dev/null || true
fi
ENV_FILE="$SMOKE_DIR/.env.smoke"
PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
MASTER=$(python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())")
PGPASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")

cat > "$ENV_FILE" <<EOF
POSTGRES_DB=infraspine_ai_test
POSTGRES_USER=infraspine_test
POSTGRES_PASSWORD=$PGPASS
DATABASE_URL=postgresql+asyncpg://infraspine_test:$PGPASS@postgres:5432/infraspine_ai_test
APP_ENV=testing
MASTER_ENCRYPTION_KEY=$MASTER
MASTER_ENCRYPTION_KEY_ID=v1
SESSION_COOKIE_SECURE=false
INFRASPINE_IMAGE=infraspine-ai:local
INFRASPINE_FRONTEND_IMAGE=infraspine-frontend:local
INSTALL_MEDIA_RUNTIME=false
INSTALL_BAILEYS=false
API_WORKERS=1
RAG_VECTOR_STORE=faiss
RAG_EMBEDDING_ALLOW_DOWNLOAD=true
INFRASPINE_HTTP_PORT=18080
INFRASPINE_HTTPS_PORT=18443
INFRASPINE_POSTGRES_DIR=$SMOKE_DIR/postgres
INFRASPINE_DATA_DIR=$SMOKE_DIR/data
INFRASPINE_LOG_DIR=$SMOKE_DIR/logs
INFRASPINE_BACKUP_DIR=$SMOKE_DIR/backups
INFRASPINE_IMPORT_DIR=$SMOKE_DIR/imports
INFRASPINE_TLS_DIR=$SMOKE_DIR/certs
BACKUP_INTERVAL_SECONDS=864000
EOF

PROJECT=infraspine-smoke
COMPOSE="$DC -p $PROJECT -f compose.yaml --env-file $ENV_FILE"

echo "Building images..."
$COMPOSE build api frontend

echo "Starting postgres..."
$COMPOSE up -d postgres
i=0
while [ "$i" -lt 60 ]; do
  if $COMPOSE exec -T postgres pg_isready -U infraspine_test -d infraspine_ai_test >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done

echo "Migrating test database..."
$COMPOSE run --rm --no-deps api migrate

echo "Starting api, worker, frontend..."
$COMPOSE up -d api worker frontend postgres-backup

echo "Waiting for API health..."
i=0
while [ "$i" -lt 90 ]; do
  if $COMPOSE exec -T api python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3).read()' >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done

echo "Health:"
$COMPOSE exec -T api python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode())'
echo "Ready:"
$COMPOSE exec -T api python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=8).read().decode())'

echo "Creating smoke user..."
printf "%s" "$PASSWORD" | $COMPOSE exec -T api python -m app.cli create-user --email smoke@example.test --role admin --password-stdin

echo "Login + RAG smoke..."
$COMPOSE exec -T -e SMOKE_PASSWORD="$PASSWORD" api python /app/docker/smoke/smoke_rag.py

echo "SMOKE TEST PASSED"
