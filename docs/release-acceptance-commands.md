# Guarded release acceptance commands

## Latest F10 unified inbox

The Admin/Manager/Employee workspace now has a “My work” page for the signed-in user's workflow tasks, approval/status notices, service-request deadline notices and training reminders. Task completion and read receipts call the existing authorization-checked APIs. Frontend typecheck, tests (**3/3**) and production build passed; authenticated browser acceptance is pending, and freeform personal task creation remains incomplete.

## Latest F15 focused acceptance

The employee expertise directory now has its own migration, authenticated API and workspace page. Its latest isolated PostgreSQL/Qdrant check passed **2 tests, 0 failures, 0 skips**:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_isolated_backend_acceptance.ps1 -TestPaths 'tests/test_expertise.py'
```

The final full backend suite after migration 0032 and F15 changes passed **704 tests, skipped 3, failed 0, 115 warnings in 1160.04s** with Python 3.12.10 in isolated PostgreSQL/Qdrant project `infraspine-accept-8e10709503df`. Skips: two opt-in Phase 7 load cases and the private CRM PDF fixture. Frontend typecheck, tests (3/3) and build passed after the page was added; browser acceptance remains open.

Previously verified source archive v8 (369 payload files, 1,006,989 bytes) predates this command-document refresh. Rebuild a uniquely named archive from the final worktree using the commands below; it is unsigned source, not an installer. No production image was rebuilt because C:/D: free-space checks returned Access denied.

## Current A03 candidate and acceptance result - 2026-10-24

- Built `infraspine-ai:acceptance-20261024-a03-local-only` from the current dirty worktree HEAD `32919d7a6979157354ae7377ae0c15080a0e9683`; Docker image ID `sha256:4bbfdfd3a5aed96d3df6c384eb51d0e127d5dacbef79950a587d166c65f27eb9`, 3,492,407,459 bytes. The build used the pinned Python 3.12 and Node 22 base images with `INSTALL_MEDIA_RUNTIME=false`; this image is for local-only policy/restore acceptance, not media model acceptance.
- `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_combined_snapshot_restore_acceptance.ps1 -Image infraspine-ai:acceptance-20261024-a03-local-only -ApiPort 18088 -QdrantPort 16338 -LocalOnly` passed. It restored a read-only synthetic snapshot into a separate PostgreSQL/files/Qdrant clone, migrated 0026 to 0031, verified unchanged counts `3 users, 2 documents, 2 chunks, 2 workflows, 2 runs, 2 tasks`, restored the Qdrant alias with 2 points, and confirmed API/worker health plus `LLM_LOCAL_ONLY=true` in both services. Review/API URL: `http://127.0.0.1:18088/`; synthetic clone/volumes are preserved.
- `node.exe .\scripts\browser_smoke.mjs http://127.0.0.1:18088/` did **not** pass: Chromium reported `Target crashed`; its GPU process exited with code `-1073741790`. The Node computer-use helper also failed to start because its runtime path was unavailable. No current authenticated role-browser result is claimed for this candidate. Earlier r9/r10 browser results remain tied to those exact images.
- Final isolated backend suite on the current application source: **701 passed, 3 skipped, 0 failed, 115 warnings in 1162.56s**; final local-only/provider/evaluation focused suite: **44 passed, 0 skipped, 0 failed**. Skips are the private CRM fixture and two opt-in capacity benchmarks. Frontend typecheck, 3 tests and production build passed in the current worktree.

These guarded commands target the confirmed Windows host and explicit Docker context `desktop-linux`. Current source is dirty HEAD `32919d7a6979157354ae7377ae0c15080a0e9683`. The restored r9 candidate `infraspine-ai:acceptance-20261024-r9-industry-templates` (image ID `sha256:c7a3a639a2826bfcc9b3e33bfb453787900e107d89522111331f5563a907da63`) remains running at loopback `http://127.0.0.1:18087/`; the earlier synthetic review candidate on 18082 is also healthy. r9 restore reached migration 0031 with unchanged counts and passed authenticated Admin/Manager/Employee journeys. r10 image `infraspine-ai:acceptance-20261024-r10-first-run` (`sha256:5c118d368b49978e5e04a01f475d2d9e2cd76053a4fb7efcce08b255b775822c`) passed first-run browser and two-customer isolation acceptance. Final source backend suite: **701 passed, 3 skipped, 0 failed, 115 warnings in 1162.56s**; the final A03 policy/provider/evaluation modules passed **44/44**. Frontend typecheck, tests (**3 passed**) and production build pass. The newest verified source archive and current candidate image are recorded in the excluded release-readiness handoff after build. Credential-owner rotation, live Gemini, upgrade/rollback, TLS, customer production and 600 GB workload remain blockers. Never replace the protected application `.env`, point tests at the live application database, restore into the source deployment, or use `down -v`, volume rm, system prune, or a database reset.

## 1. Isolated backend acceptance

Run from the repository root after registering the supported Python 3.12 runtime and starting Docker Desktop normally:

```powershell
py -0p
docker info --format '{{.ServerVersion}}'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_host.ps1 -PythonPath .\.venv-verify-20261023\Scripts\python.exe -SkipInstall -BenchmarkVectors 0 -HostBackendTests
```

The repository runner preserves any old venv and creates a fresh Python 3.12 sibling if needed. It installs from the checked-in runtime, development, media, document-table and reporting manifests; runs dependency and source secret checks; creates random credentials and unique loopback ports for a separate `*_test` PostgreSQL database and Qdrant; validates the DB safety guard before tests; checks collection; runs actual-server Qdrant tests; then runs all backend tests. Its temporary project has separate named volumes. On exit it stops only that invocation's containers and removes only its labeled network; it does not remove volumes. `-BenchmarkVectors 0` skips load benchmarks. This is **fresh isolated test DB acceptance**, not a clone or production DB backup.

To run only the changed suites after the runner's service-independent checks, use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_host.ps1 -PythonPath .\.venv-verify-20261023\Scripts\python.exe -SkipInstall -BenchmarkVectors 0 -HostBackendTests -BackendTestSelection 'tests/test_agent_profiles.py;tests/test_agent_profile_policy.py;tests/test_automation.py;tests/test_training_assignments.py;tests/test_training_progress.py;tests/test_knowledge.py;tests/test_enterprise_people.py;tests/test_access_control.py;tests/test_governance_workflow.py'
```

Do not add `-SkipInstall` to a newly created venv. Do not repoint `DATABASE_URL` or `TEST_DATABASE_URL` to a customer database.

For local-only processing policy changes, run the isolated policy/provider/evaluation modules:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_isolated_backend_acceptance.ps1 -TestPaths 'tests/test_processing_policy.py;tests/test_provider_endpoints.py;tests/test_eval_gate.py'
```

The acceptance checks model-picker filtering, denial before credential decryption/client creation, local-only endpoint resolution, provider-verification denial and evaluation-provider denial. It uses synthetic test settings; it does not call Gemini or another live provider.

## 2. Production image build (does not start services)

Once Docker is healthy, run this from the repository root:

```powershell
docker build --pull=false --file Dockerfile --tag infraspine-ai:release-candidate .
docker image inspect infraspine-ai:release-candidate --format '{{.Id}} {{.Size}}'
```

This builds the pinned production Dockerfile and reports the local image identity. It does not start the API, worker, database, or touch deployment volumes. It is a candidate image, not a signed release archive. Create and verify a source archive after the last change:

```powershell
py -3.12 .\scripts\build_release_bundle.py --output artifacts\infraspine-source-candidate.zip
py -3.12 .\scripts\verify_release_bundle.py --path artifacts\infraspine-source-candidate.zip
```

The bundle manifest records each path, size and SHA-256. The verifier checks ZIP CRC, the complete manifest, runtime environment-file exclusion, and high-confidence credential patterns in every text payload, including tests. It never prints a matched value; it reports any binary or oversized files that could not be scanned as text. Keep the earlier exposed credential as a release blocker until its owner verifies rotation. This source archive is not a signed installer or a bundled production image.

## 3. Live Gemini, ingestion, citation and original-source preview

First rotate the previously exposed active credential with its issuing service and verify the replacement through the approved secret store. Do not send the key to the script or command line. Start an **isolated synthetic staging deployment** with the existing Gemini integration configured there. Do not use production or a shared customer account.

Local staging:

```powershell
py -3.12 .\scripts\live_gemini_acceptance.py --base-url http://127.0.0.1:18081
```

Authorized HTTPS staging:

```powershell
py -3.12 .\scripts\live_gemini_acceptance.py --base-url 'https://<authorized-staging-host>' --allow-remote
```

The script prompts for the already-created synthetic staging administrator account and hides the password. TLS verification remains enabled. It uploads one private synthetic PDF, waits up to three minutes for processing, requires Gemini to answer with the synthetic fact and an available citation to that PDF, and requests the cited page image under the same authenticated session. It leaves the synthetic document in place for review and never modifies provider configuration. Run the role/ACL denial and revocation acceptance through the isolated backend suite above; this script does not create accounts or change department memberships.

## 4. Full coordinated snapshot and clone restore rehearsal

### Latest synthetic restore evidence — 2026-10-23

The isolated QA snapshot passed integrity verification at **1,870,913,622 bytes / 744 files**. The database restored to a separate QA database at Alembic `0026` with 3 users, 2 documents, 2 chunks and 2 workflows/runs/tasks; restored app files verified at 53 files / 274,027,960 bytes; restored Qdrant storage verified at 690 files / 1,596,443,997 bytes. A separate Qdrant 1.19 server recovered four collections / 10,002 points. The original saved application backup is also restored to isolated `restore_clone`, migrated from revision 0018 through 0026, and verified with unchanged counts of 2 users, 4 documents and 73 chunks. The application has not yet been started against all recovered targets together. These were synthetic QA targets; the live database and production configuration were not modified.

The app-data snapshot required a staging copy because the snapshot utility refuses symlinks: all 12 QA HF-cache links were checked as internal and valid, then dereferenced in the isolated staging copy. The remaining symlink behavior is a utility limitation.

Use a scheduled maintenance window only for the source snapshot because API and worker writes must be quiesced while files are captured. These commands use the default FAISS deployment, so no Qdrant source directory is included. If the source actually uses Qdrant, include its stopped storage path and configure a separate Qdrant target in the restore clone.

### Capture a verified source snapshot

This reads the existing production Compose environment by path but never prints it. Choose an installation-specific Compose project invocation that matches the running source project. The output locations must be new; the backup and snapshot tools refuse to overwrite existing files.

```powershell
$ErrorActionPreference = 'Stop'
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$backup = "/var/lib/infraspine/backups/$stamp.dump"
$bundle = "/var/lib/infraspine/backups/$stamp-bundle"

docker compose --env-file .env.onprem -f compose.onprem.yaml stop api worker
if ($LASTEXITCODE -ne 0) { throw 'Could not quiesce source writers.' }
docker compose --env-file .env.onprem -f compose.onprem.yaml run --rm --no-deps api python -m app.cli db-backup --output $backup
if ($LASTEXITCODE -ne 0) { throw 'Source database backup failed; preserve all files and inspect the error.' }
docker compose --env-file .env.onprem -f compose.onprem.yaml --profile ops run --rm --no-deps snapshot python -m app.cli deployment-snapshot-create --database-backup $backup --application-data /var/lib/infraspine/data --output $bundle
if ($LASTEXITCODE -ne 0) { throw 'Coordinated snapshot failed; preserve any partial bundle for inspection.' }
docker compose --env-file .env.onprem -f compose.onprem.yaml --profile ops run --rm --no-deps snapshot python -m app.cli deployment-snapshot-verify --path $bundle
if ($LASTEXITCODE -ne 0) { throw 'Snapshot verification failed; do not use the bundle for restore.' }
docker compose --env-file .env.onprem -f compose.onprem.yaml up -d api worker
if ($LASTEXITCODE -ne 0) { throw 'Source service restart failed; preserve the backup and follow the deployment runbook.' }
```

If any operation fails, do not remove the `.part` file, partial bundle, source volumes, or data. Restore the source API/worker using the same Compose project and runbook. `db-backup` uses PostgreSQL client environment variables rather than putting passwords in process arguments. Snapshot creation records SHA-256 values and refuses an existing output directory.

### Database-only migration clone from the diagnostic backup

This uses the verified current PostgreSQL dump at `.backups\readonly-1f2a62e7b34f4257b9afa8e8e3febbfe\application-20261023T063513Z.dump`. It restores only into a brand-new compose PostgreSQL volume and does not need `CREATEDB` on the application database role. Configure `.env.restore-qa` as described below, setting `INFRASPINE_BACKUP_DIR` to the dump's parent directory and all clone paths to `C:\infraspine-restore-qa\...`. Do not point it at `.env.onprem`.

```powershell
$ErrorActionPreference = 'Stop'
$restoreRoot = 'C:\infraspine-restore-qa'
if (Test-Path -LiteralPath $restoreRoot) { throw 'Clone target already exists; preserve it and choose a new root.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml up -d postgres
if ($LASTEXITCODE -ne 0) { throw 'Isolated clone PostgreSQL did not start.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml run --rm --no-deps api python -m app.cli db-restore-empty --path /var/lib/infraspine/backups/application-20261023T063513Z.dump
if ($LASTEXITCODE -ne 0) { throw 'Empty-database restore guard refused or restore failed; leave the clone intact.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml run --rm --no-deps api alembic current
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect restored clone migration revision.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM users; SELECT count(*) FROM documents; SELECT count(*) FROM document_chunks"'
if ($LASTEXITCODE -ne 0) { throw 'Could not record restored clone baseline table counts.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml run --rm --no-deps api alembic upgrade 0025
if ($LASTEXITCODE -ne 0) { throw 'Clone migration failed; preserve it for diagnosis and do not touch the source DB.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml run --rm --no-deps api alembic current
if ($LASTEXITCODE -ne 0) { throw 'Could not verify clone is at migration 0025.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks)"'
if ($LASTEXITCODE -ne 0) { throw 'Could not verify document and chunk counts after migrations through 0025.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml run --rm --no-deps api alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Migration 0026 agent-catalog acceptance failed on the isolated clone.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml run --rm --no-deps api python -m app.worker --check
if ($LASTEXITCODE -ne 0) { throw 'Clone worker schema/readiness check failed.' }
docker compose --env-file .env.restore-qa -p infraspine-db-clone -f compose.onprem.yaml exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT (SELECT version_num FROM alembic_version), (SELECT count(*) FROM users), (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks)"'
if ($LASTEXITCODE -ne 0) { throw 'Could not verify clone revision and table counts.' }
```

This verifies pending migrations against a copy of existing database contents. It does not restore application files or vector indexes, and it does not start an API. Keep the clone for inspection; do not run `down -v` or remove its database directory.

### Prepare a completely separate recovery target

Create `.env.restore-qa` through the organization’s secret manager/approved local secret-handling procedure. It must point to a new clone database and new paths, with a random clone-only PostgreSQL password and the source encryption key loaded securely if encrypted stored Gemini credentials must be readable in the clone. Keep this file access-restricted and out of Git. Required nonsecret values include:

- `POSTGRES_DB=infraspine_restore_qa`
- `POSTGRES_USER=infraspine_restore_qa`
- `DATABASE_URL=postgresql+asyncpg://infraspine_restore_qa:<URL-encoded-clone-password>@postgres:5432/infraspine_restore_qa`
- `INFRASPINE_POSTGRES_DIR=C:\infraspine-restore-qa\postgres`
- `INFRASPINE_DATA_DIR=C:\infraspine-restore-qa\data`
- `INFRASPINE_LOG_DIR=C:\infraspine-restore-qa\logs`
- `INFRASPINE_BACKUP_DIR=<absolute path to the source snapshot parent>`
- `INFRASPINE_API_PORT=18080`
- `MASTER_ENCRYPTION_KEY` and `MASTER_ENCRYPTION_KEY_ID` from the approved secret store (do not print either value).

Before restore, confirm `.env.restore-qa` points to the clone database host `postgres`, not the source endpoint; ensure the restore project and each target path are new. The output database and paths below are deliberately different from production. Restore the files first, before Compose creates the clone's data mount:

```powershell
$ErrorActionPreference = 'Stop'
$restoreRoot = 'C:\infraspine-restore-qa'
$sourceBundle = 'C:\path\to\verified-snapshot-bundle'
if (Test-Path -LiteralPath $restoreRoot) { throw 'Restore target root already exists; preserve it and choose a new path.' }
if (-not (Test-Path -LiteralPath (Join-Path $sourceBundle 'snapshot.json'))) { throw 'Verified snapshot bundle was not found.' }
New-Item -ItemType Directory -Path $restoreRoot | Out-Null

docker run --rm --network none --mount "type=bind,source=$sourceBundle,target=/snapshot,readonly" --mount "type=bind,source=$restoreRoot,target=/restore" infraspine-ai:release-candidate python -m app.cli deployment-snapshot-verify --path /snapshot
if ($LASTEXITCODE -ne 0) { throw 'Snapshot verification failed.' }
docker run --rm --network none --mount "type=bind,source=$sourceBundle,target=/snapshot,readonly" --mount "type=bind,source=$restoreRoot,target=/restore" infraspine-ai:release-candidate python -m app.cli deployment-snapshot-restore-files --path /snapshot --application-data-target /restore/data
if ($LASTEXITCODE -ne 0) { throw 'Application file restore failed; preserve the target and inspect it.' }

docker compose --env-file .env.restore-qa -p infraspine-restore-qa -f compose.onprem.yaml up -d postgres
if ($LASTEXITCODE -ne 0) { throw 'Isolated clone PostgreSQL did not start.' }
docker compose --env-file .env.restore-qa -p infraspine-restore-qa -f compose.onprem.yaml run --rm --no-deps api python -m app.cli db-restore-empty --path /var/lib/infraspine/backups/<stamp>-bundle/database/database.dump
if ($LASTEXITCODE -ne 0) { throw 'Restore refused or failed. Do not run cleanup; inspect the clone target.' }
docker compose --env-file .env.restore-qa -p infraspine-restore-qa -f compose.onprem.yaml run --rm --no-deps api alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Clone migration acceptance failed; leave the clone intact for diagnosis.' }
docker compose --env-file .env.restore-qa -p infraspine-restore-qa -f compose.onprem.yaml run --rm --no-deps api python -m app.worker --check
if ($LASTEXITCODE -ne 0) { throw 'Restored worker readiness check failed.' }
docker compose --env-file .env.restore-qa -p infraspine-restore-qa -f compose.onprem.yaml exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT (SELECT version_num FROM alembic_version), (SELECT count(*) FROM users), (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks)"'
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect clone revision/counts.' }
docker compose --env-file .env.restore-qa -p infraspine-restore-qa -f compose.onprem.yaml up -d api worker
if ($LASTEXITCODE -ne 0) { throw 'Restored clone API/worker did not start.' }
Invoke-RestMethod -Uri 'http://127.0.0.1:18081/health/ready' -Method Get
```

For an actual pending-migration rehearsal, capture the clone source at its current revision, record `alembic_version` before upgrade, and verify each expected step through 0022, 0023, 0024 and 0025 on this clone. The commands above check user/document/chunk counts before and after 0025, then separately apply the new agent-catalog migration 0026. Do not run `alembic downgrade` on the source. Run the guarded fresh-database suite in section 1 separately. Validate restored role/department counts, an authenticated login, a cited answer, department denial, revocation and a source preview before accepting the restore. Leave the clone available for inspection; if you pause it, use `docker compose ... stop`, never `down -v`.

`0022` backfills steward ownership and its downgrade refuses transferred ownership/authorship; `0023` separates quiz answers and refuses downgrade after assignments, scores or nonempty keys; `0024` is additive and refuses downgrade when reminder history exists; `0025` adds guarded workflow records; `0026` adds immutable, department-scoped agent profile versions and refuses downgrade if profile history exists. The saved-backup clone was migrated through `0026`; the separate restored QA snapshot was already at `0026`. The live application database was not migrated.

## Current evidence boundary

The tested source identity is the current dirty worktree at HEAD `32919d7a6979157354ae7377ae0c15080a0e9683`, not a release. Docker image `infraspine-ai:verification-20261023` (SHA-256 `eb7ab99edd0bb9a5089e72a5ae75f5948fbacf099d0c8077e62505129803ed18`) started isolated PostgreSQL/Qdrant/API/worker services; the same-origin frontend was rendered in Edge and API readiness passed. Browser acceptance passed Admin/Manager/Employee logins, upload/worker, cited answer/source preview, document ACL denial, report permissions and workflow approval/task completion. Provider mode was mock; live Gemini awaits owner-confirmed credential rotation. Full suite evidence is 655 passed/10 skipped/0 failed; 39 focused workflow/permissions tests, 155 service-independent tests, Qdrant lifecycle 1 passed, and frontend 3 tests/build passed. A current 10k-vector Qdrant benchmark is in `docs/capacity-benchmark.md`. The saved-backup clone is at 0026 with counts 2/4/73. Component-level snapshot restore passed across DB/files/Qdrant, but the app has not booted on the recovered targets as one unit. The 600 GB target remains unqualified; there is no external HTTPS staging ingress or release bundle to scan.
