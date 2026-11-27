# Deploy InfraSpine with Docker

This is the production Docker stack on branch `ops/docker`. It runs the API,
the document worker, an nginx frontend, PostgreSQL 17, and a backup sidecar.

The native Windows install on a workstation should **not** be switched to this
stack without a planned cutover. Use these files when you are deploying on a
Linux server or on Windows Docker Desktop as a separate environment.

## What runs by default

| Service | Role |
| --- | --- |
| `postgres` | PostgreSQL 17.6 (checksums on, volume-backed). Not published to the host. |
| `postgres-backup` | Custom-format `pg_dump` on a schedule (default daily, 14 kept). |
| `api` | Uvicorn (or Gunicorn via `API_SERVER=gunicorn`) as user `10001`. |
| `worker` | `python -m app.worker` for document ingestion. |
| `frontend` | nginx serving `/admin/` and proxying `/api` and `/health`. Bound to `127.0.0.1:80` (and `443` when certificates are present). |

Optional Compose profiles:

- `qdrant` — Qdrant vector database. The default vector store remains FAISS.
- `ollama` — local LLM runtime, reached as `http://ollama:11434`.
- `baileys-sidecar` — standalone Node image of the unofficial WhatsApp Web bridge. The supported production path is `INSTALL_BAILEYS=true` so the API process can supervise `bridge.mjs` over pipes.
- Whisper / media: rebuild with `INSTALL_MEDIA_RUNTIME=true` and set `RAG_MEDIA_*`.

`compose.onprem.yaml` is kept for existing on-prem checkouts. New deployments should use `compose.yaml`.

## Linux server

1. Install Docker Engine and the Compose v2 plugin.
2. Clone the repository and check out the release (or the `ops/docker` branch).
3. `cp .env.docker.example .env` and fill in `POSTGRES_PASSWORD`, `DATABASE_URL`, and `MASTER_ENCRYPTION_KEY`. Generate the key with `python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"`.
4. For HTTPS, place `tls.crt` and `tls.key` in `docker-data/certs/` (or the path in `INFRASPINE_TLS_DIR`). nginx will listen on 8443 inside the container, published as host 443.
5. Build and start:

       make build
       make up
       make migrate

6. Create the first admin user:

       docker compose --env-file .env run --rm api cli create-user --email admin@example.com --role admin --password-stdin

7. Open `http://127.0.0.1/` (or HTTPS) and sign in. Put a reverse proxy or firewall in front if this host is not only localhost.

Useful commands: `make logs`, `make backup`, `make restore FILE=docker-data/backups/<file>.dump` (restore refuses unless the database is empty).

## Windows Docker Desktop

Docker Desktop is a separate environment from the native Windows install.
That workstation already runs the API and document worker with scheduled tasks
against a local Postgres 18 service.

If you want a Docker copy *alongside* that install:

1. Start Docker Desktop and wait until `docker version` shows a running engine.
2. Copy the repo (or this branch) to a path that does not reuse the live data
   directory. Do not mount the live uploads folder or the live Postgres data
   directory into this stack.
3. Use `.env.docker.example` with a **new** database password and master key.
4. Bind ports that do not collide with the native API on `127.0.0.1:8000`,
   for example `INFRASPINE_HTTP_PORT=8080`.
5. `docker compose --env-file .env up -d --build` then `docker compose run --rm api migrate`.

A full cutover from the native Windows service to Docker would need a
maintenance window, a verified `python -m app.cli db-backup`, a restore into
the Compose Postgres 17 volume, and a copy of `RAG_DATA_DIR`. Ask before
doing that.

## Production database network access

On a single machine, where the API and PostgreSQL run together, bind Postgres
to localhost only (`listen_addresses = 'localhost'`, so `127.0.0.1` and `::1`).
That is the correct setting for the native Windows workstation install.

When the application and the database run on **separate servers**, do not
expose PostgreSQL on the public internet. Use controlled network access:

- Place the database on a private network (VPC, VLAN, or Docker/overlay network) that only the app servers can reach.
- Restrict port 5432 with a firewall allowlist of those app-server addresses.
- Require TLS for client connections (`ssl = on`, and `hostssl` in `pg_hba.conf`).
- Keep password encryption at `scram-sha-256` (the default in current PostgreSQL). Avoid `md5` and avoid `trust` for remote hosts.

`compose.yaml` does not publish Postgres to the host by default; the API and
worker reach it on the internal Compose network. If you publish it for
debugging, bind to `127.0.0.1` only, not `0.0.0.0`.

## Resource notes

Default memory limits are 1 GiB for Postgres, 3 GiB for the API, 4 GiB for
the worker (document parsing can be large), and 256 MiB for nginx and the
backup sidecar. Raise `WORKER_MEM_LIMIT` if you process table-heavy PDFs.

The API and worker share the same image. Changing `INSTALL_MEDIA_RUNTIME` or
`INSTALL_BAILEYS` requires a rebuild.
