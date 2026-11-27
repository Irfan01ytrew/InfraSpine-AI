[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$compose = Join-Path $repo 'compose.test.yaml'
$docker = Get-Command docker.exe -ErrorAction Stop
$backupVolume = 'infraspine-qa-29a24ed3_qa-backups'
$dumpPath = '/snapshots/qa-restore-20261023-bundle3/database/database.dump'
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
$project = "infraspine-migrate-0027-$suffix"
$network = "$project-network"
$networkCreated = $false
$composeStarted = $false
$keepForDiagnosis = $false

function New-RandomHex([int]$Bytes) {
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return ([BitConverter]::ToString($buffer)).Replace('-', '').ToLowerInvariant()
}

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

try {
    Push-Location $repo
    $server = & $docker.Source --context desktop-linux info --format '{{.ServerVersion}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or $server -notmatch '^\d+\.') { throw 'The authorized desktop-linux Docker Engine is unavailable.' }

    $volumeLabels = & $docker.Source --context desktop-linux volume inspect $backupVolume --format '{{json .Labels}}' | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $volumeLabels.'com.docker.compose.project' -ne 'infraspine-qa-29a24ed3' -or
        $volumeLabels.'com.docker.compose.volume' -ne 'qa-backups') {
        throw 'The expected synthetic QA backup volume was not found; refusing to mount a different source.'
    }
    $dumpList = & $docker.Source --context desktop-linux run --rm --network none `
        --mount "type=volume,src=$backupVolume,dst=/snapshots,readonly" `
        --entrypoint pg_restore postgres:18.6-alpine --list $dumpPath 2>&1
    if ($LASTEXITCODE -ne 0 -or -not ($dumpList | Where-Object { $_ -match 'TABLE DATA public alembic_version' })) {
        throw 'The read-only synthetic backup dump did not contain an Alembic revision table.'
    }

    $cacheVolumes = @(& $docker.Source --context desktop-linux volume ls --format '{{.Name}}')
    if ($LASTEXITCODE -ne 0 -or $cacheVolumes -notcontains 'infraspine-verification-model-cache') {
        throw 'The dedicated public model cache required by the verification Compose file is missing.'
    }

    for ($attempt = 0; $attempt -lt 128 -and -not $networkCreated; $attempt++) {
        $subnet = "10.242.$(Get-Random -Minimum 0 -Maximum 256).0/24"
        $null = & $docker.Source --context desktop-linux network create --driver bridge --subnet $subnet `
            --label "com.infraspine.accept.project=$project" $network 2>&1
        $networkCreated = $LASTEXITCODE -eq 0
    }
    if (-not $networkCreated) { throw 'Could not allocate a unique network for the disposable migration clone.' }

    $dbName = "restore_0027_$suffix"
    $dbUser = "ia$suffix"
    $dbPassword = New-RandomHex 32
    $qdrantKey = New-RandomHex 32
    $pgPort = Get-FreeLoopbackPort
    do { $qdrantPort = Get-FreeLoopbackPort } while ($qdrantPort -eq $pgPort)
    $env:VERIFY_DB_NAME = $dbName
    $env:VERIFY_DB_GUARD = $dbName
    $env:VERIFY_DB_USER = $dbUser
    $env:VERIFY_DB_PASSWORD = $dbPassword
    $env:VERIFY_POSTGRES_PORT = "$pgPort"
    $env:VERIFY_QDRANT_API_KEY = $qdrantKey
    $env:VERIFY_QDRANT_PORT = "$qdrantPort"
    $env:VERIFY_DOCKER_NETWORK = $network
    $env:DATABASE_URL = "postgresql+asyncpg://${dbUser}:${dbPassword}@postgres:5432/${dbName}"
    $env:TEST_DATABASE_URL = $env:DATABASE_URL
    $env:QDRANT_TEST_URL = 'http://qdrant:6333'
    $env:QDRANT_TEST_API_KEY = $qdrantKey
    $env:APP_ENV = 'testing'
    $env:INFASPINE_TEST_SCHEMA = ''

    & $docker.Source --context desktop-linux compose -p $project -f $compose up -d --wait postgres
    if ($LASTEXITCODE -ne 0) { throw 'Disposable migration-clone PostgreSQL did not become healthy.' }
    $composeStarted = $true
    $pgContainer = (& $docker.Source --context desktop-linux compose -p $project -f $compose ps -q postgres).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $pgContainer) { throw 'Could not identify the disposable clone PostgreSQL container.' }

    $restoreScript = 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --no-owner --no-privileges --exit-on-error --host postgres --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" ' + $dumpPath
    & $docker.Source --context desktop-linux compose -p $project -f $compose run --rm --no-deps `
        --entrypoint sh --volume "${backupVolume}:/snapshots:ro" postgres -c $restoreScript
    if ($LASTEXITCODE -ne 0) { throw 'Restore into the disposable 0027 clone failed; preserve its volume for diagnosis.' }

    $countQuery = 'SELECT (SELECT version_num FROM alembic_version LIMIT 1), (SELECT count(*) FROM users), (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks);'
    $beforeOutput = & $docker.Source --context desktop-linux exec $pgContainer psql -U $dbUser -d $dbName -At -F ',' -c $countQuery
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the restored clone baseline.' }
    $beforeMatch = @($beforeOutput | Where-Object { $_ -match '^0026,\d+,\d+,\d+$' } | Select-Object -Last 1)
    if (-not $beforeMatch) { throw 'The restored clone did not start at migration 0026; preserving it without upgrade.' }
    $before = $beforeMatch[0].ToString().Trim()

    & $docker.Source --context desktop-linux compose -p $project -f $compose --profile verification run --build --rm --no-deps backend-test python -m alembic current
    if ($LASTEXITCODE -ne 0) { throw 'Alembic could not read the restored 0026 clone revision.' }
    & $docker.Source --context desktop-linux compose -p $project -f $compose --profile verification run --build --rm --no-deps backend-test python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Migration 0027 failed on the restored disposable clone.' }

    $afterQuery = 'SELECT (SELECT version_num FROM alembic_version LIMIT 1), (SELECT count(*) FROM users), (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks), (SELECT count(*) FROM service_requests), (to_regclass(''public.service_request_events'') IS NOT NULL);'
    $afterOutput = & $docker.Source --context desktop-linux exec $pgContainer psql -U $dbUser -d $dbName -At -F ',' -c $afterQuery
    if ($LASTEXITCODE -ne 0) { throw 'Could not verify clone counts and new migration tables.' }
    $afterMatch = @($afterOutput | Where-Object { $_ -match '^0027,\d+,\d+,\d+,\d+,(t|f)$' } | Select-Object -Last 1)
    if (-not $afterMatch) { throw 'The restored clone did not report the expected 0027 schema.' }
    $after = $afterMatch[0].ToString().Trim()
    $beforeParts = $before -split ','
    $afterParts = $after -split ','
    if (($beforeParts[1..3] -join ',') -ne ($afterParts[1..3] -join ',') -or $afterParts[4] -ne '0' -or $afterParts[5] -ne 't') {
        throw 'The 0027 upgrade changed restored core row counts or did not create both request tables.'
    }

    Write-Output "MIGRATION_CLONE_PROJECT=$project"
    Write-Output "BACKUP_SOURCE_VOLUME=$backupVolume (read-only)"
    Write-Output "BASELINE_REVISION_AND_COUNTS=$before"
    Write-Output "FINAL_REVISION_COUNTS_REQUESTS_EVENTS=$after"
    Write-Output 'MIGRATION_0027_CLONE_ACCEPTANCE=PASS'
} catch {
    $keepForDiagnosis = $composeStarted
    throw
} finally {
    if ($composeStarted -and -not $keepForDiagnosis) {
        & $docker.Source --context desktop-linux compose -p $project -f $compose down --remove-orphans | Out-Null
    }
    if ($networkCreated -and -not $keepForDiagnosis) {
        $details = & $docker.Source --context desktop-linux network inspect $network 2>$null | ConvertFrom-Json
        $owner = if ($details) { $details[0].Labels['com.infraspine.accept.project'] } else { $null }
        if ($LASTEXITCODE -eq 0 -and $owner -eq $project) {
            & $docker.Source --context desktop-linux network rm $network | Out-Null
        }
    }
    foreach ($name in @(
        'VERIFY_DB_NAME','VERIFY_DB_GUARD','VERIFY_DB_USER','VERIFY_DB_PASSWORD','VERIFY_POSTGRES_PORT',
        'VERIFY_QDRANT_API_KEY','VERIFY_QDRANT_PORT','VERIFY_DOCKER_NETWORK','DATABASE_URL','TEST_DATABASE_URL',
        'QDRANT_TEST_URL','QDRANT_TEST_API_KEY','APP_ENV','INFASPINE_TEST_SCHEMA'
    )) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    Pop-Location
}
