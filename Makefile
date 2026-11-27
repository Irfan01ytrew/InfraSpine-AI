# InfraSpine Docker operations. Requires Docker Compose v2.
COMPOSE    ?= docker compose -f compose.yaml --env-file .env

.PHONY: help build up down ps logs migrate backup restore smoke

help:
	@echo "Targets: build up down ps logs migrate backup restore smoke"
	@echo "Optional: docker compose -f compose.yaml -f compose.debug.yaml --env-file .env up -d"
	@echo "Profiles: COMPOSE_PROFILES=qdrant,ollama,baileys-sidecar"

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=200

migrate:
	$(COMPOSE) run --rm --no-deps api migrate

backup:
	$(COMPOSE) exec postgres-backup /bin/sh /backup.sh once

restore:
	@test -n "$(FILE)" || (echo "Usage: make restore FILE=docker-data/backups/<file>.dump" && exit 2)
	$(COMPOSE) exec postgres-backup /bin/sh /restore.sh /backups/$(notdir $(FILE))

smoke:
	./docker/smoke/smoke.sh
