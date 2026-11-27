# On-premises deployment

The repository includes a CPU Docker deployment for one API process, PostgreSQL, and one or more document workers. PostgreSQL and optional Qdrant are not published to the host. Each deployment may select one read-only reporting source: the existing allowlisted PostgreSQL connector or a separately managed, rooted SQLite reporting file. This reporting source is separate from the application database and is not mounted into document workers. The API is bound to localhost and should sit behind an organization-controlled TLS reverse proxy. Keep one index-owning API process per data directory. For more simultaneous users, set `API_WORKERS` above 1 together with `RAG_INDEX_ROLE=auto` (FAISS): the first API process owns the index files and runs background jobs, and the others serve requests from in-memory index replicas that follow PostgreSQL (`/health/ready` reports `index_role`). Each replica holds its own copy of the vectors in RAM, and `API_WORKERS × (DATABASE_POOL_SIZE + DATABASE_POOL_MAX_OVERFLOW)` plus document workers must stay below PostgreSQL's `max_connections`. Workers can be replicated when they share the same PostgreSQL database and document storage.

## Configure and start

1. Copy `.env.onprem.example` to `.env.onprem`; keep the file out of source control. Set a long random PostgreSQL password, URL-encode it in `DATABASE_URL`, and generate `MASTER_ENCRYPTION_KEY` with `python -m app.cli generate-master-key`. Store that key in the deployment secret manager. Provider keys remain managed in the UI and encrypted in PostgreSQL. Do not reuse or replace the development `.env`.
   `ORG_MFA_REQUIRED=true` is an operator-owned policy that forces TOTP enrollment before access and requires a fresh second-factor proof for existing sessions. Roll it out after administrators have working authenticators and recovery procedures. Application users cannot disable this policy; turn it off only through a reviewed deployment configuration change.
2. Set `INFRASPINE_IMPORT_DIR` to the host folder that the API container may import. It is mounted read-only at `/imports`. Review the document permissions before importing.
3. Build the tagged image, then start PostgreSQL. Compose uses the same `INFRASPINE_IMAGE` for the API, worker, and snapshot tool, so deployments can run one reviewed image artifact:

   ```powershell
   docker compose --env-file .env.onprem -f compose.onprem.yaml build api worker
   docker compose --env-file .env.onprem -f compose.onprem.yaml up -d postgres
   ```

4. Apply migrations as a one-shot command. This does not start an API server:

   ```powershell
   docker compose --env-file .env.onprem -f compose.onprem.yaml run --rm --no-deps api alembic upgrade head
   ```

5. Start the single API and the worker:

   ```powershell
   docker compose --env-file .env.onprem -f compose.onprem.yaml up -d api worker
   ```

Assignment deadline email is optional and disabled by default. To enable it, configure `PERSONAL_TASK_EMAIL_ENABLED`,
`PERSONAL_TASK_SMTP_HOST`, `PERSONAL_TASK_EMAIL_FROM`, and `PERSONAL_TASK_SMTP_SECURITY` (`starttls` or `ssl`) in
the protected `.env.onprem` file; set username and password together when the relay requires authentication. The
worker uses certificate-verified TLS, retries transient failures with bounded backoff, and stores only delivery
state and an error class. Messages identify the private My Work inbox without including the task title or body.
Upgrade to migration 0053 before starting a worker with email enabled. SMS delivery is not implemented.

For the optional disk-oriented Qdrant provider, set `RAG_VECTOR_STORE=qdrant` and
`RAG_QDRANT_URL=http://qdrant:6333` in `.env.onprem`, and set a high-entropy `RAG_QDRANT_API_KEY` in the
deployment secret store. The value configures both Qdrant and the API client. Start its isolated persistent
service before the API:

```powershell
docker compose --env-file .env.onprem -f compose.onprem.yaml --profile qdrant up -d qdrant
docker compose --env-file .env.onprem -f compose.onprem.yaml --profile qdrant up -d api worker
```

Qdrant is on the private Compose network and has no host-published port. Its collection vectors, HNSW graph and
payloads use Qdrant's cold/on-disk storage configuration; its indexed `chunk_id` payload supports authorized ID
filtering. Use SSD/NVMe and budget RAM for Qdrant's filter indexes, metadata, operating-system cache, API, and
embedding model. The API still confirms permission in PostgreSQL before loading text and before returning results.

The API health check is `GET /health/ready`; the worker uses `python -m app.worker --check`. PostgreSQL and the application data, logs, backups and Qdrant storage persist in the host directories selected by `INFRASPINE_POSTGRES_DIR`, `INFRASPINE_DATA_DIR`, `INFRASPINE_LOG_DIR`, `INFRASPINE_BACKUP_DIR` and `INFRASPINE_QDRANT_DIR`. Defaults remain `onprem-postgres`, `onprem-data`, `onprem-logs`, `onprem-backups` and `onprem-qdrant`; existing single-install data stays at those paths. Restrict access to those directories and protect host disks with the organization’s at-rest encryption policy.

The on-prem Compose template uses Docker’s rotating `local` log driver for each service, capped at three 10 MB files per container. This bounds future container log growth to about 30 MB per service; it does not cap database, document, vector, model, backup or build storage. Docker applies log-driver changes when containers are created, so existing running services keep their current logging configuration until a planned recreation. The template change does not modify a running installation.

For a new installation, create the first administrator explicitly with `python -m app.cli create-user` in a one-shot API container. After sign-in, an Admin is guided to Company setup while `setup_complete=false`; non-admin users see a setup-pending page until the Admin saves the company profile. The saved brand name and color apply immediately and persist across reloads. Existing databases and accounts should be upgraded in place and retained. Do not run a database reset or initialize over an existing database.

## Customer isolation, company setup and licensing

Deploy one installation, PostgreSQL database, and private `RAG_DATA_DIR` per customer. The product does not
implement shared-database multi-tenancy; this deployment boundary keeps accounts, documents, indexes, audit
records, backups and model configuration isolated. Give each customer a distinct Compose project (`-p`), separate
secret file, database credentials, `INFRASPINE_API_PORT`, all five data paths above, import path and network policy.
For installations on one host, choose different unused API ports and absolute/private paths outside the source tree.
Do not reuse the same `.env` file or any storage path between customer projects. The config-only isolation check is
`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check_deployment_isolation.ps1`; it does not start
containers. A live synthetic isolation rehearsal is available as
`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_two_customer_install_acceptance.ps1 -Image infraspine-ai:acceptance-20261024-aclfix-r2`;
it creates fresh test-only Compose projects, credentials and paths, checks API login, setup, signed licenses and
database/vector/file isolation, preserves generated volumes, then stops only its generated containers. Supply the
exact image that will be evaluated. Each customer project uses
`INFRASPINE_NETWORK_SUBNET`; the default is `10.243.0.0/24`. If projects share one Docker host, select a distinct,
unused, non-overlapping private subnet for every project after checking existing Docker networks. Do not copy the
same subnet into multiple project files. `INFRASPINE_IMAGE` selects the shared application artifact; rebuild and
retest after changing its digest.

After creating the first administrator, sign in and open **Company setup**. The profile supports the customer's
company name, industry, display brand and a validated hex accent color. It is stored in
`RAG_DATA_DIR/company-profile.json`, applied to the sign-in page and application shell, and included with the
rest of `RAG_DATA_DIR` in deployment backups. It contains no credentials.

The Company setup page offers versioned starter profiles for General, Consulting, Real Estate, Healthcare,
Education, Manufacturing and Financial Services, plus a free-text custom-industry choice. A profile saves only the
selected template identifier/version and company branding. **Create missing department groups** is a separate,
Admin-only action; it creates empty local department groups and is idempotent. It never assigns members, documents
or access grants, and it leaves existing groups unchanged. Review the proposed department and knowledge-area names,
then assign people and document access through the normal Departments and Knowledge controls. Template presets are
operational starting points, not compliance certifications or legal guidance. API: `GET
/api/admin/deployment/templates` and `POST /api/admin/deployment/templates/{template_id}/apply`.

Offline license enforcement is opt-in for backward compatibility. The publisher supplies a base64-encoded raw
Ed25519 public key in `LICENSE_PUBLIC_KEY`; keep its private signing key offline and outside every customer
installation. Set `LICENSE_ENFORCEMENT_ENABLED=true` only after the deployment is configured and a matching
signed license is available. The administrator's **Company setup** page displays the persistent deployment ID
and accepts the signed JSON envelope. The signed payload has `product`, `license_id`, `customer`,
`deployment_id`, timezone-aware `issued_at` and `expires_at`, `seat_limit`, and a list of unique feature names.
Sign the UTF-8 JSON encoding of the payload with sorted keys and compact separators (`(',', ':')`); the envelope's
`signature` is standard base64. The server checks the signature, exact deployment ID, issue/expiry dates and
seat limit. Active accounts plus unexpired invitations reserve seats; invitation creation is serialized to avoid
concurrent over-allocation. The `research`, `training`, `market_research`, and `reporting` feature names gate those routes when enforcement is
enabled. Health checks, sign-in/out, and the admin deployment page stay available so an administrator can renew
an expired license. Feature names for other product areas are currently informational and do not gate routes.

The customer receives only the public key and signed license. Never enter a private signing key or provider
credential into Company setup. Back up `company-profile.json`, `deployment-id` and `license.json` with the
application data, and preserve the deployment ID on restore; issuing a new deployment ID requires a replacement
license.

## Local models and secure automation

On-prem Compose disables embedding-model downloads by default with `RAG_EMBEDDING_ALLOW_DOWNLOAD=false` and sets the Hugging Face/Transformers clients to offline mode. Stage the embedding snapshot under `onprem-data/models` and Docling model weights under `onprem-data/models/huggingface` before first use; set `RAG_EMBEDDING_MODEL` to the local snapshot path and `RAG_EMBEDDING_DIMENSION` to its dimension. If a connected installation explicitly opts into downloads, set `RAG_EMBEDDING_ALLOW_DOWNLOAD=true`, `HF_HUB_OFFLINE=false` and `TRANSFORMERS_OFFLINE=false`; ensure outbound model-hub access is approved. Changing models requires re-embedding and rebuilding the vector indexes; plan the temporary disk and database space first.

The on-premises template sets `LLM_LOCAL_ONLY=true`. In this mode, built-in/cloud providers (including Gemini) are omitted from the user model picker and refused before credentials are decrypted. Admin connection verification and the evaluation CLI's environment-configured provider are blocked too, so the application makes no provider request to those cloud services. A custom OpenAI-compatible model is allowed only when its exact origin is listed in `PROVIDER_TRUSTED_PRIVATE_ORIGINS`; each connection re-resolves DNS and verifies that all resolved addresses are private before connecting. To use an external cloud provider such as Gemini, the deployment operator must deliberately set `LLM_LOCAL_ONLY=false` and apply the organization's external-processing approval policy. For an on-premises model, configure its exact internal origin in the trusted-origin list, then add its base URL and model in the UI. For example, `http://ollama:11434` is permitted only if that is the model service this deployment controls. The application does not include a model server or download a model at startup.

Public market and competitor research is separately disabled by default. Set `RAG_EXTERNAL_RESEARCH_ENABLED=true` only when the deployment is allowed to send the user's research topic to Gemini's Google Search grounding service; the UI requires explicit consent for each run and does not send internal documents or the company profile. The audit record is committed before transmission and includes provider/model identifiers and topic length, not topic content. This feature needs outbound access to Google's Gemini API and requires the `market_research` license feature when enforcement is enabled. Keep it disabled in disconnected deployments.

Local folder imports are administrator-only and copy files into private application storage. To import or synchronize the mounted folder into a department:

```powershell
docker compose --env-file .env.onprem -f compose.onprem.yaml run --rm --no-deps api python -m app.cli rag-import-folder --path /imports --admin-email admin@example.com --visibility restricted --department-id DEPARTMENT_UUID --source-id contracts
```

Run the same command with the same `--source-id` to resume or synchronize. Unchanged path/hash pairs are skipped, changed files create an archived source version, and exact duplicate content is reported while records remain separate to preserve ACL boundaries. Imports never delete source files or documents that disappeared from the folder. Track queue and worker status in the UI under Settings → Folder import progress, using the same source ID. Keep one worker per available processing slot; worker jobs use PostgreSQL leases and retry after interruptions.

## Read-only business database analysis

This optional Admin-only feature connects to one operator-configured PostgreSQL reporting source. Keep its
credential separate from the InfraSpine application database, and provision a dedicated `LOGIN NOINHERIT`
account without superuser, database-creation, role-creation, replication or row-security-bypass rights. Grant it
`CONNECT`, schema `USAGE`, and table `SELECT` only for the approved business tables. The API independently checks
those privileges and rejects write access in business schemas before each query; each query also runs inside a
PostgreSQL `READ ONLY` transaction with a statement timeout.

In `.env.onprem`, set `RAG_DATABASE_ANALYSIS_URL` and a JSON `RAG_DATABASE_ANALYSIS_CATALOG`. The catalog maps
`schema.table` to explicitly approved columns and types, for example:

```dotenv
RAG_DATABASE_ANALYSIS_URL=postgresql+asyncpg://report_reader:REPLACE_ME@erp-db.example.test:5432/reporting
RAG_DATABASE_ANALYSIS_CATALOG={"erp.orders":{"order_id":"integer","department_code":"text","region":"text","amount":"numeric","ordered_on":"date"}}
RAG_DATABASE_ANALYSIS_DEPARTMENT_GRANTS={"00000000-0000-0000-0000-000000000001":[{"table":"erp.orders","column":"department_code","value":"finance"}]}
RAG_DATABASE_ANALYSIS_SOURCE_LABEL=ERP reporting replica
RAG_DATABASE_ANALYSIS_STATEMENT_TIMEOUT_MS=5000
RAG_DATABASE_ANALYSIS_MAX_ROWS=100
```

Choose a database name different from the InfraSpine application database. The Knowledge page accepts only structured
row, count, aggregate, grouping and filter requests; it does not accept SQL text. Results show the configured source,
table and query time. Audit details record query shape and counts without filter values or result content. Compose passes
the source URL to the API service only, not the ingestion worker. The catalog and URL are deployment
secrets/configuration and must not be pasted into chat or diagnostic output. By default, only Admins can query. To
grant a Manager or Employee access, map the internal `access_groups.id` UUID for a department to one or more allowlisted
`table`, `column` and exact `value` filters in `RAG_DATABASE_ANALYSIS_DEPARTMENT_GRANTS`. Membership is resolved from
current department membership (or `department_managers` for managers); each server-owned predicate is ANDed with the
user's submitted filters. Users without an applicable grant receive no table catalog and query requests are denied.
Keep grant values aligned with the source system's department identifiers and review them when either system's
membership changes.

For a file-backed SQLite reporting database, configure `RAG_DATABASE_ANALYSIS_SQLITE_PATH` and
`RAG_DATABASE_ANALYSIS_SQLITE_ROOT` instead of `RAG_DATABASE_ANALYSIS_URL`. The file must already exist beneath the
configured root; do not point this feature at the application database or upload directory. For example, with the
on-prem Compose data volume mounted at `/var/lib/infraspine/data`, set the root to
`/var/lib/infraspine/data/business` and the source to `/var/lib/infraspine/data/business/reporting.sqlite3`. Keep the
SQLite file read-only to the API service account at the filesystem layer. The connector additionally opens SQLite
with URI `mode=ro`, confirms `query_only`, permits only generated read statements, enforces the same bounded query
catalog and department predicates, and interrupts queries at the configured execution budget. Compose passes this
path only to the API; the worker receives an empty path. Configure one source type per deployment. Department grants,
catalog scope, audit privacy and result bounds apply to both connectors. SQLite mode does not replace deployment,
backup, or customer acceptance.

## Audio and video indexing

Transcription is optional, disabled by default, and runs entirely in the ingestion worker. It supports the listed audio
formats and extracts speech from a video's audio track. Video on-screen text is OCRed locally from sampled frames
when `RAG_MEDIA_VIDEO_FRAME_OCR_ENABLED=true`; no frame or transcript is sent to Gemini or another LLM provider.
The Whisper model is loaded from disk. Install the optional runtime when
building the on-prem image by setting `INSTALL_MEDIA_RUNTIME=true` in `.env.onprem`; stage a compatible
faster-whisper/CTranslate2 model under `onprem-data/models`, then set `RAG_MEDIA_MODEL_PATH` to its path inside the
container (the example points to `/var/lib/infraspine/data/models/faster-whisper-small`). Set
`RAG_MEDIA_ENABLED=true` only after the model is present. The settings page reports whether the optional runtime and
model path are available without exposing the path. RapidOCR and ONNX Runtime are included in the base image for
local scanned-document and video-frame OCR; missing OCR runtime or local weights make video ingestion fail safely.

The media extra pins `faster-whisper==1.2.1` with `av==18.1.0`. This is intentional: faster-whisper 1.2.1 passes `metadata_errors` to `av.open`, which PyAV 19 removed. Keep the compatible PyAV pin until a newer faster-whisper release has passed the local media suite ([upstream compatibility issue](https://github.com/SYSTRAN/faster-whisper/issues/1589), [PyAV release notes](https://github.com/PyAV-Org/PyAV/releases)).

Folder imports allow files up to `RAG_IMPORT_MAX_FILE_MB`; audio/video have the additional
`RAG_MEDIA_MAX_FILE_MB` and `RAG_MEDIA_MAX_DURATION_SECONDS` caps. The default processing timeout may be too short for
long recordings or CPU-only models, so set `RAG_PROCESSING_TIMEOUT_SECONDS` to match the expected file duration and
hardware. Video frame sampling defaults to at least `RAG_MEDIA_VIDEO_FRAME_INTERVAL_SECONDS=30` seconds and at most
`RAG_MEDIA_VIDEO_MAX_FRAMES=300` frames per file; the parser spreads those frames over long recordings and honors
`RAG_MAX_PAGES` as an additional unit cap. Each file loads the local models in its isolated parser process.
Throughput, model memory, transcription quality across supported languages, and mixed media indexing have not been
benchmarked on enterprise hardware. Citations identify either transcript time ranges or the specific OCRed frame
timestamp. Frame OCR extracts visible text; it does not describe non-text visual content.

For scheduled folder synchronization, set `RAG_IMPORT_ROOT` to the read-only import mount (the Compose default is
`/imports`) and place source folders beneath it. Admins register a relative path and an interval of at least five
minutes; absolute paths and paths escaping the configured root are refused. Both API and worker mount
`INFRASPINE_IMPORT_DIR` read-only. The worker records count-only create/update/removal diffs, retries partial or
failed scans with capped backoff, and quarantines removed files from retrieval while preserving their stored bytes
and version history. Change alerts are in-app and each recipient can disable them in My work. The import mount is an
external source and is not part of InfraSpine's coordinated backup; keep the source share backed up independently.

Chat and agent citations link to the original audio/video at the cited start time. The source endpoint rechecks the
requester's current document ACL and supports single HTTP byte ranges for native playback and seeking. A revoked
user therefore receives the same not-found response as any other inaccessible source.

## Backup and restore

Partner document originals are stored under `RAG_DATA_DIR/partner_uploads`, and their workspace, ingestion state, chunks and embeddings are stored in PostgreSQL. Include that directory in the same coordinated snapshot as PostgreSQL. The `partner_index` vector index is derived from the authorized PostgreSQL chunks and must be restored or rebuilt with the same embedding model and dimension; the original files and database remain necessary for reprocessing and verified downloads. Protect the partner files and index with the same storage controls as other customer data.

When Qdrant is selected, its collection data is separate from the PostgreSQL dump. Include the `onprem-qdrant`
volume and the `onprem-data/*.qdrant.json` manifests in the same encrypted recovery set as PostgreSQL and uploads.
For a filesystem-level Qdrant copy, quiesce API/worker writes and stop Qdrant before snapshotting its storage
directory. The vector collection can be rebuilt from PostgreSQL's stored embeddings, but retain the Qdrant
snapshot through cutover validation so rollback stays available.

Create a verified custom-format database backup without overwriting an existing file:

```powershell
docker compose --env-file .env.onprem -f compose.onprem.yaml run --rm --no-deps api python -m app.cli db-backup --output /var/lib/infraspine/backups/infraspine.dump
docker compose --env-file .env.onprem -f compose.onprem.yaml run --rm --no-deps api python -m app.cli db-verify-backup --path /var/lib/infraspine/backups/infraspine.dump
```

The backup command obtains connection settings from the process environment, never puts the database URL or password in the PostgreSQL client command line, and prints only the database name, backup path, byte count, catalog count and SHA-256. The production image includes PostgreSQL 18 client tools matching the on-prem database image. Backups contain sensitive business metadata. Restrict their ACLs and store them on encrypted, access-controlled storage. Keep `.env.onprem` and its encryption/license secrets in the secret manager; they are deliberately excluded from snapshot bundles.

For a coordinated deployment snapshot, stop all application writers and stop Qdrant when it is enabled. PostgreSQL stays up for its logical dump. This captures the PostgreSQL archive, original uploads, FAISS index, application manifests and profile, plus the Qdrant storage directory when selected. The snapshot command copies files into a new bundle and records SHA-256 for each file; it refuses an existing output path and verifies the dump catalog. It preserves partial output if interrupted so an operator can inspect it.

```powershell
$stamp = Get-Date -Format 'yyyyMMddTHHmmssZ'
docker compose --env-file .env.onprem -f compose.onprem.yaml stop api worker
# Run this only when the Qdrant profile is enabled:
docker compose --env-file .env.onprem -f compose.onprem.yaml --profile qdrant stop qdrant
docker compose --env-file .env.onprem -f compose.onprem.yaml run --rm --no-deps api python -m app.cli db-backup --output "/var/lib/infraspine/backups/$stamp.dump"
docker compose --env-file .env.onprem -f compose.onprem.yaml --profile ops run --rm --no-deps snapshot python -m app.cli deployment-snapshot-create --database-backup "/var/lib/infraspine/backups/$stamp.dump" --application-data /var/lib/infraspine/data --qdrant-data /var/lib/infraspine/qdrant-snapshot --output "/var/lib/infraspine/backups/$stamp-bundle"
docker compose --env-file .env.onprem -f compose.onprem.yaml --profile ops run --rm --no-deps snapshot python -m app.cli deployment-snapshot-verify --path "/var/lib/infraspine/backups/$stamp-bundle"
# Restart the original installation after verification:
docker compose --env-file .env.onprem -f compose.onprem.yaml --profile qdrant up -d qdrant api worker
```

When Qdrant is disabled, omit `--qdrant-data` from snapshot creation and restart with `docker compose ... up -d api worker` (without the Qdrant profile). Keep the services stopped until the snapshot verifies. On restore, copy the bundle to an isolated recovery installation, restore its file roots to new, nonexistent data paths with `deployment-snapshot-restore-files`, and restore `database/database.dump` into a separately provisioned empty PostgreSQL database with `db-restore-empty`. Never point restore at the live database or populated storage directories. Start services only after the restored DB and files have been checked. The restore tools refuse populated targets and do not drop or clean any path.

The `database/database.dump` inside a verified bundle is the backup for database restore. For a restore, provision a new empty PostgreSQL database and point `DATABASE_URL` at it, then run:

```powershell
docker compose --env-file .env.onprem -f compose.onprem.yaml run --rm --no-deps api python -m app.cli db-restore-empty --path "/var/lib/infraspine/backups/$stamp-bundle/database/database.dump"
```

Restore the application files from a trusted project checkout on the recovery host; use new paths that do not exist yet (include the Qdrant target only if the bundle contains that root):

```powershell
python -m app.cli deployment-snapshot-restore-files --path ".\onprem-backups\$stamp-bundle" --application-data-target ".\recovery-data" --qdrant-data-target ".\recovery-qdrant"
```

Restore refuses any database with user tables or sequences and any file target that already exists; it never drops, truncates, or cleans an existing database or path. Validate the restored database and stored-file snapshot before starting the API and worker.

## Capacity and current limits

FAISS remains the default: its persistent exact `IndexFlatIP` keeps every 384-dimensional float32 embedding in API memory. Each vector consumes 1,536 bytes before FAISS IDs and overhead; PostgreSQL also stores embedding bytes and text chunks. The optional Qdrant provider uses cold/on-disk vectors, HNSW and payloads and streams database reconciliation in bounded batches. Qdrant's filter indexes and service metadata still consume RAM, so this does not prove that any particular corpus or hardware fits. Neither provider is validated or sized for a 600 GB source corpus.

Index rebuilds stream stored embeddings from PostgreSQL in keyset-ordered batches of 1,000. Qdrant persists the build checkpoint and uses atomic collection aliases to cut over after a full rebuild; the old Qdrant collection and the separate FAISS index remain available for rollback. Qdrant reconciliation streams ordered database and vector IDs in bounded batches. FAISS reconciliation still materializes and sorts its in-memory ID set. The admin vector-store status shows rebuild progress. Benchmark provider and Qdrant service RAM, disk, throughput and ACL-restricted retrieval on the target hardware.

For FAISS-to-Qdrant migration, back up PostgreSQL, uploaded files and the FAISS directory first. Stop API and ingestion workers for the cutover window, set `RAG_VECTOR_STORE=qdrant`, start Qdrant, and run `python -m app.cli rag-rebuild-index` against the existing database. The rebuild reads stored embedding bytes using the configured model identity and dimension; its manifest records these values, the checkpoint and active alias. Rerun the command after interruption to resume from the last confirmed chunk ID. Start API and worker, compare indexed and database counts, then exercise permitted and denied department queries and source citations. Keep the FAISS files and prior Qdrant collection until validation is accepted. Roll back by selecting `RAG_VECTOR_STORE=faiss` and restarting; startup reconciliation catches up database changes made since cutover. Qdrant does not replace PostgreSQL ACL checks.

The 600 GB target remains unqualified. Benchmark the actual file mix, chunk counts, embedding model, ACL distribution, query load, disk latency and service memory on deployment hardware. Do not treat the synthetic benchmark as proof of capacity.
