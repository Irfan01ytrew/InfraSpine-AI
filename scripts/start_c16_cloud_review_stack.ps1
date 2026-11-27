param(
    [int]$PostgresPort = 55928,
    [int]$ApiPort = 63012,
    [int]$FrontendPort = 5184,
    [string]$ReviewRoot = '.verification-local-review-20261029-c16-cloud-live'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not [IO.Path]::IsPathRooted($ReviewRoot)) { $ReviewRoot = Join-Path $repo $ReviewRoot }
$reviewRoot = [IO.Path]::GetFullPath($ReviewRoot)
if (-not $reviewRoot.StartsWith($repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The isolated C16 review root must stay inside the repository.'
}
if (Test-Path -LiteralPath $reviewRoot) { throw 'The C16 review folder already exists; inspect it before starting another stack.' }
foreach ($port in @($ApiPort, $FrontendPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "C16 loopback port $port is occupied; refusing to attach to an unknown service."
    }
}
$frontendEnvFiles = @(Get-ChildItem -LiteralPath (Join-Path $repo 'frontend') -Force -File -Filter '.env*' -ErrorAction SilentlyContinue)
if ($frontendEnvFiles.Count -gt 0) { throw 'Refusing to start Vite while frontend-local environment files are present.' }

$python = Join-Path $repo '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$viteEntry = Join-Path $repo 'frontend\node_modules\vite\bin\vite.js'
$model = Join-Path $repo 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
$pgBin = 'C:\Program Files\PostgreSQL\18\bin'
$pgIsReady = Join-Path $pgBin 'pg_isready.exe'
$psql = Join-Path $pgBin 'psql.exe'
$createdb = Join-Path $pgBin 'createdb.exe'
foreach ($path in @($python, $viteEntry, $model, $pgIsReady, $psql, $createdb)) {
    if (-not (Test-Path -LiteralPath $path)) { throw 'A required Python runtime, local model, Vite or PostgreSQL tool is unavailable.' }
}
& $pgIsReady -h 127.0.0.1 -p $PostgresPort | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The already-running isolated loopback verification PostgreSQL cluster is unavailable.' }

$bytes = New-Object byte[] 4
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$suffix = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
$database = "infraspine_c16live_$suffix"
$runtimePath = Join-Path $reviewRoot 'runtime-state.json'
$fixturePath = Join-Path $reviewRoot 'credentials.json'
$dataRoot = Join-Path $reviewRoot 'data'
$apiCwd = Join-Path $reviewRoot 'api-cwd'
$logs = Join-Path $reviewRoot 'logs'
$apiLogs = Join-Path $logs 'api'
$workerLogs = Join-Path $logs 'worker'
$envNames = @(
    'DATABASE_URL','APP_ENV','LLM_MOCK_MODE','LICENSE_ENFORCEMENT_ENABLED','MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID',
    'SESSION_COOKIE_SECURE','RAG_DATA_DIR','RAG_MODELS_DIR','RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_EMBEDDING_MODEL',
    'HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','RAG_VECTOR_STORE','RAG_INGESTION_MODE','RAG_WORKER_ENABLED',
    'RAG_INDEX_SYNC_SECONDS','RAG_USE_DOCLING','RAG_PARSE_ISOLATION','RAG_MIN_SIMILARITY','RAG_ADMIN_ONLY_UPLOADS',
    'LOG_LEVEL','LOG_DIR','PYTHONPATH','TEMP','TMP','C16_CREDENTIAL_FIXTURE','INFRASPINE_DEV_API_PROXY_TARGET'
)
$previous = @{}
foreach ($name in $envNames) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$started = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$databaseCreated = $false
$ready = $false

function Set-ReviewAcl([string]$Path, [bool]$Directory) {
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($Directory) {
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
    } else {
        $acl = [System.Security.AccessControl.FileSecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
    }
    $acl.SetOwner($owner)
    foreach ($sidText in @($owner.Value, 'S-1-5-18', 'S-1-5-32-544')) {
        $sid = [Security.Principal.SecurityIdentifier]::new($sidText)
        $inheritance = if ($Directory) { [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit } else { [Security.AccessControl.InheritanceFlags]::None }
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, [Security.AccessControl.FileSystemRights]::FullControl, $inheritance,
            [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

try {
    New-Item -ItemType Directory -Path $reviewRoot | Out-Null
    Set-ReviewAcl $reviewRoot $true
    foreach ($directory in @($dataRoot, $apiCwd, $logs, $apiLogs, $workerLogs, (Join-Path $reviewRoot 'tmp'))) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $masterBytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($masterBytes) } finally { $rng.Dispose() }
    $masterKey = [Convert]::ToBase64String($masterBytes)
    @{ database = $database; postgres_port = $PostgresPort; api_port = $ApiPort; frontend_port = $FrontendPort; master_key = $masterKey } |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $runtimePath -Encoding utf8
    Set-ReviewAcl $runtimePath $false

    $existing = & $psql -w -h 127.0.0.1 -p $PostgresPort -U postgres -d postgres -At -c "SELECT count(*) FROM pg_database WHERE datname='$database'"
    if ($LASTEXITCODE -ne 0 -or $existing.Trim() -ne '0') { throw 'The new C16 review database name was not unique.' }
    & $createdb -h 127.0.0.1 -p $PostgresPort -U postgres -O postgres $database
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated C16 review database.' }
    $databaseCreated = $true

    $env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$database"
    $env:APP_ENV = 'testing'
    $env:LLM_MOCK_MODE = 'true'
    $env:LICENSE_ENFORCEMENT_ENABLED = 'false'
    $env:MASTER_ENCRYPTION_KEY = $masterKey
    $env:MASTER_ENCRYPTION_KEY_ID = 'c16qa'
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:RAG_DATA_DIR = $dataRoot
    $env:RAG_MODELS_DIR = Join-Path $repo 'data\models'
    $env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
    $env:RAG_EMBEDDING_MODEL = $model
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    $env:RAG_VECTOR_STORE = 'faiss'
    $env:RAG_INGESTION_MODE = 'worker'
    $env:RAG_WORKER_ENABLED = 'false'
    $env:RAG_INDEX_SYNC_SECONDS = '2'
    $env:RAG_USE_DOCLING = 'false'
    $env:RAG_PARSE_ISOLATION = 'false'
    $env:RAG_MIN_SIMILARITY = '0.15'
    $env:RAG_ADMIN_ONLY_UPLOADS = 'true'
    $env:LOG_LEVEL = 'INFO'
    $env:LOG_DIR = $apiLogs
    $env:PYTHONPATH = $repo
    $env:TEMP = Join-Path $reviewRoot 'tmp'
    $env:TMP = $env:TEMP
    $env:C16_CREDENTIAL_FIXTURE = $fixturePath

    Push-Location $repo
    try {
        & $python -m alembic upgrade head *> (Join-Path $logs 'migration.log')
        if ($LASTEXITCODE -ne 0) { throw 'The isolated C16 review database migration failed; inspect the protected local migration log.' }
        & $python (Join-Path $PSScriptRoot 'seed_c16_cloud_review.py') *> (Join-Path $logs 'seed.log')
        if ($LASTEXITCODE -ne 0) { throw 'The synthetic C16 review account could not be seeded; inspect the protected local seed log.' }
    } finally { Pop-Location }
    Set-ReviewAcl $fixturePath $false

    $api = Start-Process $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$ApiPort",'--no-access-log') `
        -WorkingDirectory $apiCwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $apiLogs 'stdout.log') -RedirectStandardError (Join-Path $apiLogs 'stderr.log')
    $started.Add($api)

    $env:RAG_WORKER_ENABLED = 'true'
    $env:LOG_DIR = $workerLogs
    $worker = Start-Process $python -ArgumentList @('-m','app.worker','--concurrency','1') `
        -WorkingDirectory $apiCwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $workerLogs 'stdout.log') -RedirectStandardError (Join-Path $workerLogs 'stderr.log')
    $started.Add($worker)

    $env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"
    $vite = Start-Process $node -ArgumentList @(('"' + $viteEntry + '"'),'--host','127.0.0.1','--strictPort','--port',"$FrontendPort") `
        -WorkingDirectory (Join-Path $repo 'frontend') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs 'vite-stdout.log') -RedirectStandardError (Join-Path $logs 'vite-stderr.log')
    $started.Add($vite)

    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    $apiReady = $false
    $uiReady = $false
    while ([DateTime]::UtcNow -lt $deadline -and -not ($apiReady -and $uiReady)) {
        foreach ($process in $started) {
            if ($process.HasExited) { throw 'An isolated C16 review process exited during startup; inspect its protected local logs.' }
        }
        try { $null = Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 2; $apiReady = $true } catch {}
        try { $null = Invoke-WebRequest -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 2; $uiReady = $true } catch {}
        if (-not ($apiReady -and $uiReady)) { Start-Sleep -Milliseconds 500 }
    }
    if (-not ($apiReady -and $uiReady)) { throw 'The isolated C16 API or frontend did not become healthy; inspect its protected local logs.' }

    $schema = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/openapi.json" -TimeoutSec 5
    if (-not $schema.components.schemas.CloudConnectorSyncResult.properties.files_queued) {
        throw 'The API schema did not expose the current-source cloud ingestion result fields.'
    }
    $state = @{
        api_pid = $api.Id; worker_pid = $worker.Id; vite_pid = $vite.Id
        database = $database; migration = 'head';
        frontend_url = "http://127.0.0.1:$FrontendPort/admin/#/connectors"
        admin_panel_url = "http://127.0.0.1:$FrontendPort/admin/#/connectors"
        api_health_url = "http://127.0.0.1:$ApiPort/health/ready"
        credential_fixture = $fixturePath
    }
    $statePath = Join-Path $reviewRoot 'stack-state.json'
    $state | ConvertTo-Json -Compress | Set-Content -LiteralPath $statePath -Encoding utf8
    Set-ReviewAcl $statePath $false
    $ready = $true
    Write-Output 'C16_REVIEW_STACK=READY'
    Write-Output "FRONTEND_URL=http://127.0.0.1:$FrontendPort/admin/#/connectors"
    Write-Output "ADMIN_PANEL_URL=http://127.0.0.1:$FrontendPort/admin/#/connectors"
    Write-Output "API_HEALTH_URL=http://127.0.0.1:$ApiPort/health/ready"
    Write-Output "SYNTHETIC_CREDENTIAL_VIEWER=scripts/show_local_review_credentials.ps1 -FixturePath $fixturePath"
} finally {
    foreach ($name in $envNames) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    if (-not $ready) {
        foreach ($process in $started) {
            if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        }
        if ($databaseCreated) { Write-Output "ISOLATED_C16_DATABASE_RETAINED=$database" }
    }
}
