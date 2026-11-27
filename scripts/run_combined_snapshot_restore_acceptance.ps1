[CmdletBinding()]
param(
    [string]$Image = 'infraspine-ai:acceptance-20261024-r5-enterprise',
    [int]$ApiPort = 18083,
    [int]$QdrantPort = 16333,
    [switch]$LocalOnly,
    [switch]$DatabaseOnly,
    [string]$LicensePublicKey = ''
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$compose = Join-Path $repo 'compose.onprem.yaml'
$docker = Get-Command docker.exe -ErrorAction Stop
$backupVolume = 'infraspine-qa-29a24ed3_qa-backups'
$backupProject = 'infraspine-qa-29a24ed3'
$snapshotName = 'qa-restore-20261023-bundle3'
$modelDir = Join-Path $env:LOCALAPPDATA 'Temp\infraspine-whisper-tiny-local\models--Systran--faster-whisper-tiny.en\snapshots\0d3d19a32d3338f10357c0889762bd8d64bbdeba'
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
$project = "infraspine-combined-restore-$suffix"
$restoreRoot = Join-Path (Join-Path $env:LOCALAPPDATA 'Temp') $project
$envFile = Join-Path $restoreRoot 'restore-test.env'
$override = Join-Path $restoreRoot 'compose.restore.override.yaml'
$baseUrl = "http://127.0.0.1:$ApiPort/"
$licenseEnforcement = 'false'
if (-not [string]::IsNullOrWhiteSpace($LicensePublicKey)) {
    try { $licensePublicKeyBytes = [Convert]::FromBase64String($LicensePublicKey) }
    catch { throw 'The test-only license public key must be base64-encoded Ed25519 bytes.' }
    if ($licensePublicKeyBytes.Length -ne 32) { throw 'The test-only license public key must decode to 32 bytes.' }
    $licenseEnforcement = 'true'
}

function New-RandomHex([int]$Bytes) {
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return ([BitConverter]::ToString($buffer)).Replace('-', '').ToLowerInvariant()
}

function New-RandomBase64([int]$Bytes) {
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($buffer)
}

function Invoke-Docker([string[]]$Arguments, [string]$Purpose) {
    $output = & $docker.Source --context desktop-linux @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Purpose failed with Docker exit code $LASTEXITCODE; preserve project $project for diagnosis." }
    return $output
}

function Invoke-Compose([string[]]$Arguments, [string]$Purpose) {
    $composeArgs = @(
        '--context', 'desktop-linux', 'compose', '--profile', 'qdrant', '--env-file', $envFile,
        '-p', $project, '-f', $compose, '-f', $override
    ) + $Arguments
    $output = & $docker.Source @composeArgs
    if ($LASTEXITCODE -ne 0) { throw "$Purpose failed with Docker exit code $LASTEXITCODE; preserve project $project for diagnosis." }
    return $output
}

function Get-FreeNetworkSubnet([string[]]$UsedSubnets) {
    for ($attempt = 0; $attempt -lt 256; $attempt++) {
        $candidate = "10.250.$(Get-Random -Minimum 0 -Maximum 256).0/24"
        if ($UsedSubnets -notcontains $candidate) { return $candidate }
    }
    throw 'Could not find an unused private Docker subnet; no stack was started.'
}

function Assert-LoopbackPortFree([int]$Port) {
    $listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try { $listener.Start() } catch { throw "Loopback port $Port is already occupied; choose a free test port before running acceptance." }
    finally { $listener.Stop() }
}

try {
    Push-Location $repo
    $server = Invoke-Docker @('info', '--format', '{{.ServerVersion}}') 'Docker Engine check'
    if ($server -notmatch '^\d+\.') { throw 'The authorized desktop-linux Docker Engine is unavailable.' }

    $backup = (Invoke-Docker @('volume', 'inspect', $backupVolume) 'Synthetic backup volume inspection' | ConvertFrom-Json)[0]
    if ($backup.Labels.'com.docker.compose.project' -ne $backupProject -or
        $backup.Labels.'com.docker.compose.volume' -ne 'qa-backups') {
        throw 'The named backup volume is not the expected synthetic QA source; refusing restore.'
    }
    $imageInfo = (Invoke-Docker @('image', 'inspect', $Image) 'Candidate image inspection' | ConvertFrom-Json)[0]
    if (-not $imageInfo.Id) { throw 'The requested candidate image is unavailable; this script never builds or pulls images.' }
    if (-not $DatabaseOnly) {
        foreach ($modelFile in @('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')) {
            if (-not (Test-Path -LiteralPath (Join-Path $modelDir $modelFile) -PathType Leaf)) {
                throw 'The configured local Whisper model is incomplete; no services were started.'
            }
        }
    }
    Assert-LoopbackPortFree $ApiPort
    Assert-LoopbackPortFree $QdrantPort

    $networkIds = Invoke-Docker @('network', 'ls', '-q') 'Docker network inventory'
    $usedSubnets = @()
    foreach ($networkId in $networkIds) {
        if (-not $networkId) { continue }
        $details = Invoke-Docker @('network', 'inspect', $networkId) 'Docker network inspection' | ConvertFrom-Json
        foreach ($config in $details[0].IPAM.Config) {
            if ($config.Subnet) { $usedSubnets += [string]$config.Subnet }
        }
    }
    $subnet = Get-FreeNetworkSubnet $usedSubnets

    New-Item -ItemType Directory -Path $restoreRoot, (Join-Path $restoreRoot 'logs'),
        (Join-Path $restoreRoot 'backups'), (Join-Path $restoreRoot 'imports') -Force | Out-Null

    $database = "restore_$suffix"
    $dbUser = "qa_$suffix"
    $localOnlyValue = if ($LocalOnly) { 'true' } else { 'false' }
    $dbPassword = New-RandomHex 32
    $qdrantKey = New-RandomHex 32
    $masterKey = New-RandomBase64 32
    $lines = @(
        "POSTGRES_DB=$database",
        "POSTGRES_USER=$dbUser",
        "POSTGRES_PASSWORD=$dbPassword",
        "DATABASE_URL=postgresql+asyncpg://${dbUser}:${dbPassword}@postgres:5432/${database}",
        "INFRASPINE_IMAGE=$Image",
        "INFRASPINE_NETWORK_SUBNET=$subnet",
        "INFRASPINE_API_PORT=$ApiPort",
        "INFRASPINE_POSTGRES_DIR=unused-postgres-bind-not-mounted",
        "INFRASPINE_DATA_DIR=unused-data-bind-not-mounted",
        "INFRASPINE_LOG_DIR=$($restoreRoot.Replace('\','/'))/logs",
        "INFRASPINE_BACKUP_DIR=$($restoreRoot.Replace('\','/'))/backups",
        "INFRASPINE_QDRANT_DIR=unused-qdrant-bind-not-mounted",
        "INFRASPINE_IMPORT_DIR=$($restoreRoot.Replace('\','/'))/imports",
        "MASTER_ENCRYPTION_KEY=`"$masterKey`"",
        'MASTER_ENCRYPTION_KEY_ID=synthetic-restore-v1',
        "LICENSE_PUBLIC_KEY=$LicensePublicKey",
        "LICENSE_ENFORCEMENT_ENABLED=$licenseEnforcement",
        'APP_ENV=testing',
        'LLM_MOCK_MODE=true',
        "LLM_LOCAL_ONLY=$localOnlyValue",
        'RAG_VECTOR_STORE=qdrant',
        "RAG_QDRANT_API_KEY=$qdrantKey",
        'RAG_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2',
        'RAG_EMBEDDING_DIMENSION=384',
        'RAG_EMBEDDING_ALLOW_DOWNLOAD=false',
        'HF_HUB_OFFLINE=true',
        'TRANSFORMERS_OFFLINE=true',
        'RAG_IMPORT_MAX_FILE_MB=4096',
        'RAG_MEDIA_ENABLED=true',
        'RAG_MEDIA_MODEL_PATH=/models/whisper-tiny.en',
        'RAG_MEDIA_DEVICE=cpu',
        'RAG_MEDIA_CPU_THREADS=2',
        'RAG_MEDIA_MAX_FILE_MB=10',
        'RAG_MEDIA_MAX_DURATION_SECONDS=60',
        'RAG_MEDIA_SEGMENT_SECONDS=30',
        'RAG_MEDIA_VIDEO_FRAME_OCR_ENABLED=true',
        'RAG_EXTERNAL_RESEARCH_ENABLED=false',
        "INFRASPINE_BACKUP_VOLUME=$backupVolume",
        "INFRASPINE_WHISPER_MODEL_SOURCE=$($modelDir.Replace('\','/'))"
    )
    [IO.File]::WriteAllLines($envFile, $lines, [Text.UTF8Encoding]::new($false))
    $fileAcl = New-Object Security.AccessControl.FileSecurity
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $fileAcl.SetOwner($currentSid)
    $fileAcl.SetAccessRuleProtection($true, $false)
    $fileAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($currentSid, 'FullControl', 'Allow'))
    Set-Acl -LiteralPath $envFile -AclObject $fileAcl

    @'
services:
  postgres:
    volumes:
      - restore-postgres:/var/lib/postgresql
      - synthetic-backup:/snapshot-source:ro
  api:
    environment:
      APP_ENV: testing
      LLM_MOCK_MODE: "true"
      LLM_LOCAL_ONLY: "__LLM_LOCAL_ONLY__"
      SESSION_COOKIE_SECURE: "false"
      RAG_DATA_DIR: /var/lib/infraspine/data/restored
      RAG_MODELS_DIR: /var/lib/infraspine/data/restored/models
      HF_HOME: /var/lib/infraspine/data/restored/models/huggingface
      RAG_MEDIA_MODEL_PATH: /models/whisper-tiny.en
    volumes:
      - restore-app:/var/lib/infraspine/data
      - synthetic-backup:/snapshot-source:ro
      - restore-qdrant:/qdrant/storage
      - ${INFRASPINE_WHISPER_MODEL_SOURCE}:/models/whisper-tiny.en:ro
  qdrant:
    environment:
      QDRANT__STORAGE__STORAGE_PATH: /qdrant/storage/restored
    ports:
      - "127.0.0.1:${INFRASPINE_QDRANT_PORT}:6333"
    volumes:
      - restore-qdrant:/qdrant/storage
  worker:
    environment:
      APP_ENV: testing
      LLM_MOCK_MODE: "true"
      LLM_LOCAL_ONLY: "__LLM_LOCAL_ONLY__"
      RAG_DATA_DIR: /var/lib/infraspine/data/restored
      RAG_MODELS_DIR: /var/lib/infraspine/data/restored/models
      HF_HOME: /var/lib/infraspine/data/restored/models/huggingface
      RAG_MEDIA_MODEL_PATH: /models/whisper-tiny.en
    volumes:
      - restore-app:/var/lib/infraspine/data
      - ${INFRASPINE_WHISPER_MODEL_SOURCE}:/models/whisper-tiny.en:ro
volumes:
  restore-postgres:
  restore-app:
  restore-qdrant:
  synthetic-backup:
    external: true
    name: ${INFRASPINE_BACKUP_VOLUME}
'@.Replace('${INFRASPINE_QDRANT_PORT}', [string]$QdrantPort).Replace('__LLM_LOCAL_ONLY__', $localOnlyValue) | Set-Content -LiteralPath $override -Encoding utf8

    $null = Invoke-Compose @('config', '--services') 'Compose configuration validation'
    if (-not $DatabaseOnly) {
        $null = Invoke-Compose @('run', '--pull', 'never', '--rm', '--no-deps', 'api',
            'python', '-m', 'app.cli', 'deployment-snapshot-verify', '--path', "/snapshot-source/$snapshotName") 'Snapshot integrity verification'
        $null = Invoke-Compose @('run', '--user', '0:0', '--pull', 'never', '--rm', '--no-deps', 'api',
            'python', '-m', 'app.cli', 'deployment-snapshot-restore-files', '--path', "/snapshot-source/$snapshotName",
            '--application-data-target', '/var/lib/infraspine/data/restored', '--qdrant-data-target', '/qdrant/storage/restored') 'Application and Qdrant snapshot restore'
        $null = Invoke-Compose @('run', '--pull', 'never', '--rm', '--no-deps', '--user', '0:0', 'api',
            'chown', '-R', '10001:10001', '/var/lib/infraspine/data/restored') 'Restored application-data ownership'
    }

    $null = Invoke-Compose @('up', '-d', '--wait', '--pull', 'never', '--no-build', 'postgres') 'Isolated PostgreSQL startup'
    $restoreCommand = 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --no-owner --no-privileges --exit-on-error --host postgres --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" /snapshot-source/' + $snapshotName + '/database/database.dump'
    $null = Invoke-Compose @('run', '--pull', 'never', '--rm', '--no-deps', 'postgres', 'sh', '-c', $restoreCommand) 'Restore to disposable PostgreSQL clone'

    $beforeQuery = "SELECT (SELECT version_num FROM alembic_version LIMIT 1),(SELECT count(*) FROM users),(SELECT count(*) FROM documents),(SELECT count(*) FROM document_chunks),(SELECT count(*) FROM automation_workflows),(SELECT count(*) FROM automation_runs),(SELECT count(*) FROM automation_tasks);"
    $before = (Invoke-Compose @('exec', '-T', 'postgres', 'psql', '-U', $dbUser, '-d', $database, '-At', '-F', ',', '-c', $beforeQuery) 'Restored baseline count verification' | Select-Object -Last 1).Trim()
    if ($before -notmatch '^0026,3,2,2,2,2,2$') {
        throw "Restored synthetic DB did not match its recorded 0026 baseline; received $before. Preserve project $project."
    }

    $null = Invoke-Compose @('run', '--pull', 'never', '--rm', '--no-deps', 'api', 'alembic', 'upgrade', 'head') 'Disposable clone Alembic upgrade'
    $headOutput = Invoke-Compose @('run', '--pull', 'never', '--rm', '--no-deps', 'api', 'alembic', 'heads') 'Candidate migration head inspection'
    $headRevisions = @($headOutput | ForEach-Object { if ($_ -match '^\s*([0-9A-Za-z_]+)\s+\(head\)\s*$') { $matches[1] } })
    if ($headRevisions.Count -ne 1) {
        throw "Expected one migration head in the candidate image, received $($headRevisions.Count). Preserve project $project."
    }
    $expectedHead = $headRevisions[0]
    $afterQuery = "SELECT (SELECT version_num FROM alembic_version LIMIT 1),(SELECT count(*) FROM users),(SELECT count(*) FROM documents),(SELECT count(*) FROM document_chunks),(SELECT count(*) FROM automation_workflows),(SELECT count(*) FROM automation_runs),(SELECT count(*) FROM automation_tasks),(to_regclass('public.service_requests') IS NOT NULL),(to_regclass('public.service_request_events') IS NOT NULL),(to_regclass('public.training_practice_scenarios') IS NOT NULL),(to_regclass('public.service_request_notifications') IS NOT NULL),(to_regclass('public.workflow_notifications') IS NOT NULL),(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='service_requests' AND column_name='response_due_at')),(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='users' AND column_name='expertise_tags')),(to_regclass('public.ix_users_expertise_tags_gin') IS NOT NULL);"
    $after = (Invoke-Compose @('exec', '-T', 'postgres', 'psql', '-U', $dbUser, '-d', $database, '-At', '-F', ',', '-c', $afterQuery) 'Post-upgrade clone count verification' | Select-Object -Last 1).Trim()
    $afterFields = $after -split ','
    if ($afterFields.Count -ne 15 -or $afterFields[0] -ne $expectedHead -or
        ($afterFields[1..6] -join ',') -ne '3,2,2,2,2,2' -or
        ($afterFields[7..14] -join ',') -ne 't,t,t,t,t,t,t,t') {
        throw "The disposable clone migration changed source counts, missed the candidate head $expectedHead, or omitted a required schema object; received $after. Preserve project $project."
    }

    $null = Invoke-Compose @('up', '-d', '--wait', '--pull', 'never', '--no-build', 'qdrant') 'Restored Qdrant startup'
    $null = Invoke-Compose @('up', '-d', '--wait', '--pull', 'never', '--no-build', 'api', 'worker') 'Restored API and worker startup'
    $ready = $false
    for ($attempt = 0; $attempt -lt 60 -and -not $ready; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri ($baseUrl + 'health/ready') -TimeoutSec 3
            $ready = $response.status -eq 'ready' -and $response.database -eq 'ok'
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $ready) { throw "The combined restored API did not become ready; preserve project $project." }

    $null = Invoke-Compose @('exec', '-T', 'worker', 'python', '-m', 'app.worker', '--check') 'Restored worker configuration check'
    if ($LocalOnly) {
        foreach ($service in @('api', 'worker')) {
            $policy = (Invoke-Compose @('exec', '-T', $service, 'python', '-c', 'from app.core.config import get_settings; print(str(get_settings().llm_local_only).lower())') "${service} local-only policy verification" | Select-Object -Last 1).Trim()
            if ($policy -ne 'true') { throw "Restored ${service} did not receive the required local-only policy; preserve project $project." }
        }
    }
    $collectionSummary = @()
    $vectorAliasSummary = 'NOT_RESTORED_DATABASE_ONLY_MODE'
    if (-not $DatabaseOnly) {
        $collectionResponse = Invoke-RestMethod -Uri "http://127.0.0.1:$QdrantPort/collections" -Headers @{ 'api-key' = $qdrantKey } -TimeoutSec 10
        foreach ($collection in $collectionResponse.result.collections) {
            $encodedName = [uri]::EscapeDataString($collection.name)
            $details = Invoke-RestMethod -Uri "http://127.0.0.1:$QdrantPort/collections/$encodedName" -Headers @{ 'api-key' = $qdrantKey } -TimeoutSec 10
            $collectionSummary += "$($collection.name):$($details.result.points_count)"
        }
        $aliases = Invoke-RestMethod -Uri "http://127.0.0.1:$QdrantPort/aliases" -Headers @{ 'api-key' = $qdrantKey } -TimeoutSec 10
        $vectorAlias = $aliases.result.aliases | Where-Object alias_name -eq 'infraspine_vector_index' | Select-Object -First 1
        if (-not $vectorAlias) {
            throw 'Restored Qdrant did not expose the active InfraSpine vector alias; preserve the stack for diagnosis.'
        }
        $vectorDetails = Invoke-RestMethod -Uri "http://127.0.0.1:$QdrantPort/collections/$([uri]::EscapeDataString($vectorAlias.collection_name))" -Headers @{ 'api-key' = $qdrantKey } -TimeoutSec 10
        if ($vectorDetails.result.points_count -lt 1) {
            throw 'Restored Qdrant vector alias resolves to an empty collection; preserve the stack for diagnosis.'
        }
        $vectorAliasSummary = "$($vectorAlias.alias_name)->$($vectorAlias.collection_name):$($vectorDetails.result.points_count)"
    }
    $ps = @(Invoke-Compose @('ps', '--format', '{{.Service}}|{{.State}}|{{.Health}}') 'Restored container health inventory')
    $serviceStates = @($ps | ForEach-Object { $_.Trim() })
    if ($ps.Count -lt 4 -or -not ($serviceStates -match '^api\|running\|healthy$') -or -not ($serviceStates -match '^worker\|running\|healthy$')) {
        throw 'The restored API/worker/PostgreSQL/Qdrant services are not all running healthy; preserve the stack.'
    }

    Write-Output "RESTORE_PROJECT=$project"
    Write-Output "RESTORE_IMAGE=$Image $($imageInfo.Id)"
    Write-Output "RESTORE_BASELINE_0026=$before"
    Write-Output "RESTORE_FINAL_${expectedHead}=$after"
    Write-Output "RESTORE_QDRANT_COLLECTIONS=$($collectionSummary -join ';')"
    Write-Output "RESTORE_QDRANT_VECTOR_ALIAS=$vectorAliasSummary"
    if ($DatabaseOnly) { Write-Output 'RESTORE_MODE=DATABASE_ONLY_FILES_AND_VECTORS_NOT_RESTORED' }
    Write-Output 'RESTORE_API_WORKER_POSTGRES_QDRANT=HEALTHY'
    Write-Output "RESTORE_LOCAL_ONLY=$localOnlyValue"
    Write-Output "RESTORE_REVIEW_URL=$baseUrl"
    Write-Output "RESTORE_SYNTHETIC_ROOT=$restoreRoot"
    Write-Output 'RESTORE_CREDENTIALS=Use the existing protected synthetic QA fixture; no password is emitted.'
    Write-Output 'RESTORE_ACCEPTANCE_STACK_PRESERVED=true'
} catch {
    Write-Error $_
    Write-Output "PRESERVE_FOR_DIAGNOSIS_PROJECT=$project"
    if (Test-Path -LiteralPath $restoreRoot) { Write-Output "PRESERVE_FOR_DIAGNOSIS_ROOT=$restoreRoot" }
    exit 1
} finally {
    if ($restoreRoot -and (Test-Path -LiteralPath $restoreRoot)) {
        $safeRoot = (Resolve-Path -LiteralPath $restoreRoot).Path
        $tempRoot = (Resolve-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'Temp')).Path + [IO.Path]::DirectorySeparatorChar
        if (-not $safeRoot.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Temporary acceptance path escaped the designated user Temp directory.'
        }
    }
    Pop-Location
}
