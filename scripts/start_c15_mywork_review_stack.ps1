param(
    [int]$PostgresPort = 55929,
    [int]$ApiPort = 63015,
    [int]$FrontendPort = 5187,
    [string]$ReviewRoot = '.verification-local-review-20261029-c15-mywork-r1'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not [IO.Path]::IsPathRooted($ReviewRoot)) { $ReviewRoot = Join-Path $repo $ReviewRoot }
$root = [IO.Path]::GetFullPath($ReviewRoot)
if (-not $root.StartsWith($repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The C15 review folder must stay inside the current repository.'
}
if (Test-Path -LiteralPath $root) { throw 'The C15 review folder already exists; preserving it.' }
foreach ($port in @($ApiPort, $FrontendPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "C15 loopback port $port is occupied; refusing to attach to an unknown service."
    }
}
if (Get-ChildItem -LiteralPath (Join-Path $repo 'frontend') -Force -File -Filter '.env*' -ErrorAction SilentlyContinue) {
    throw 'Refusing to start the review Vite process while frontend-local environment files are present.'
}

$python = Join-Path $repo '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$viteEntry = Join-Path $repo 'frontend\node_modules\vite\bin\vite.js'
$pgBin = 'C:\Program Files\PostgreSQL\18\bin'
$pgIsReady = Join-Path $pgBin 'pg_isready.exe'
$psql = Join-Path $pgBin 'psql.exe'
$createdb = Join-Path $pgBin 'createdb.exe'
foreach ($item in @($python, $viteEntry, $pgIsReady, $psql, $createdb)) {
    if (-not (Test-Path -LiteralPath $item)) { throw 'A required Python, Vite or PostgreSQL tool is unavailable.' }
}
& $pgIsReady -h 127.0.0.1 -p $PostgresPort | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The retained isolated loopback PostgreSQL cluster is unavailable.' }

$bytes = New-Object byte[] 4
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$suffix = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
$database = "infraspine_c15mywork_${suffix}_review"
$runtimePath = Join-Path $root 'runtime-state.json'
$fixturePath = Join-Path $root 'credentials.json'
$dataRoot = Join-Path $root 'data'
$apiCwd = Join-Path $root 'api-cwd'
$logs = Join-Path $root 'logs'
foreach ($directory in @($root, $dataRoot, $apiCwd, $logs, (Join-Path $root 'tmp'), (Join-Path $logs 'api'), (Join-Path $logs 'vite'), (Join-Path $root 'evidence'))) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($owner)
foreach ($sidText in @($owner.Value, 'S-1-5-18', 'S-1-5-32-544')) {
    $sid = [Security.Principal.SecurityIdentifier]::new($sidText)
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $sid, [Security.AccessControl.FileSystemRights]::FullControl,
        ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $root -AclObject $acl

$masterBytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($masterBytes) } finally { $rng.Dispose() }
$masterKey = [Convert]::ToBase64String($masterBytes)
$runtime = @{ database = $database; postgres_port = $PostgresPort; api_port = $ApiPort; frontend_port = $FrontendPort; master_key = $masterKey }
$runtime | ConvertTo-Json -Compress | Set-Content -LiteralPath $runtimePath -Encoding utf8
icacls.exe $runtimePath /inheritance:r /grant:r "*$($owner.Value):F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect the isolated C15 runtime configuration.' }

$environmentNames = @('DATABASE_URL','APP_ENV','LLM_MOCK_MODE','LICENSE_ENFORCEMENT_ENABLED','MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID','SESSION_COOKIE_SECURE','RAG_DATA_DIR','RAG_MODELS_DIR','RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_INGESTION_MODE','RAG_WORKER_ENABLED','AGENT_QUEUE_WORKER_ENABLED','RAG_INDEX_SYNC_SECONDS','RAG_USE_DOCLING','RAG_PARSE_ISOLATION','LOG_LEVEL','LOG_DIR','PYTHONPATH','TEMP','TMP','C15_REVIEW_ROOT','INFRASPINE_DEV_API_PROXY_TARGET')
$previous = @{}
foreach ($name in $environmentNames) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$started = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$databaseCreated = $false
$ready = $false
try {
    $count = & $psql -w -h 127.0.0.1 -p $PostgresPort -U postgres -d postgres -At -c "SELECT count(*) FROM pg_database WHERE datname='$database'"
    if ($LASTEXITCODE -ne 0 -or $count.Trim() -ne '0') { throw 'The randomly named C15 database is not new.' }
    & $createdb -h 127.0.0.1 -p $PostgresPort -U postgres -O postgres $database
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated C15 review database.' }
    $databaseCreated = $true

    $env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$database"
    $env:APP_ENV = 'testing'
    $env:LLM_MOCK_MODE = 'true'
    $env:LICENSE_ENFORCEMENT_ENABLED = 'false'
    $env:MASTER_ENCRYPTION_KEY = $masterKey
    $env:MASTER_ENCRYPTION_KEY_ID = 'c15qa'
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:RAG_DATA_DIR = $dataRoot
    $env:RAG_MODELS_DIR = Join-Path $repo 'data\models'
    $env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
    $env:RAG_INGESTION_MODE = 'worker'
    $env:RAG_WORKER_ENABLED = 'false'
    $env:AGENT_QUEUE_WORKER_ENABLED = 'false'
    $env:RAG_INDEX_SYNC_SECONDS = '0'
    $env:RAG_USE_DOCLING = 'false'
    $env:RAG_PARSE_ISOLATION = 'false'
    $env:LOG_LEVEL = 'INFO'
    $env:LOG_DIR = Join-Path $logs 'api'
    $env:PYTHONPATH = $repo
    $env:TEMP = Join-Path $root 'tmp'
    $env:TMP = $env:TEMP
    $env:C15_REVIEW_ROOT = $root
    Push-Location $repo
    try {
        $migration = Start-Process -FilePath $python -ArgumentList @('-m','alembic','upgrade','head') -WorkingDirectory $repo `
            -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $logs 'migration-stdout.log') `
            -RedirectStandardError (Join-Path $logs 'migration-stderr.log')
        if ($migration.ExitCode -ne 0) { throw 'C15 migration to current head failed; inspect the protected local logs.' }
        $seedScript = '"' + (Join-Path $PSScriptRoot 'seed_c15_mywork_review.py') + '"'
        $seed = Start-Process -FilePath $python -ArgumentList $seedScript -WorkingDirectory $repo `
            -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $logs 'seed-stdout.log') `
            -RedirectStandardError (Join-Path $logs 'seed-stderr.log')
        if ($seed.ExitCode -ne 0) { throw 'C15 synthetic account seeding failed; inspect the protected local logs.' }
    } finally { Pop-Location }

    $api = Start-Process $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$ApiPort",'--no-access-log') `
        -WorkingDirectory $apiCwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs 'api\stdout.log') -RedirectStandardError (Join-Path $logs 'api\stderr.log')
    $started.Add($api)
    $env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"
    $vite = Start-Process $node -ArgumentList @(('"' + $viteEntry + '"'),'--host','127.0.0.1','--strictPort','--port',"$FrontendPort") `
        -WorkingDirectory (Join-Path $repo 'frontend') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs 'vite\stdout.log') -RedirectStandardError (Join-Path $logs 'vite\stderr.log')
    $started.Add($vite)

    $apiReady = $false; $uiReady = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $deadline -and -not ($apiReady -and $uiReady)) {
        foreach ($process in $started) { if ($process.HasExited) { throw 'An isolated C15 review process exited during startup.' } }
        try { $null = Invoke-WebRequest "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 3; $apiReady = $true } catch {}
        try { $null = Invoke-WebRequest "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 3; $uiReady = $true } catch {}
        if (-not ($apiReady -and $uiReady)) { Start-Sleep -Milliseconds 500 }
    }
    if (-not ($apiReady -and $uiReady)) { throw 'The new C15 API or Vite process did not become healthy.' }
    $state = @{
        api_pid = $api.Id; vite_pid = $vite.Id; database = $database; postgres_port = $PostgresPort; migration = 'head'
        frontend_url = "http://127.0.0.1:$FrontendPort/admin/#/automation"
        my_work_url = "http://127.0.0.1:$FrontendPort/admin/#/my-work"
        api_health_url = "http://127.0.0.1:$ApiPort/health/ready"
        credential_fixture = $fixturePath; runtime_state = $runtimePath
        source_hashes = @{}
    }
    foreach ($relative in @('migrations/versions/0065_workflow_personal_task_action.py','app/services/automation_worker.py','app/api/routes/automation.py','frontend/src/components/AutomationPage.tsx','frontend/src/components/MyWorkPage.tsx')) {
        $state.source_hashes[$relative] = (Get-FileHash -LiteralPath (Join-Path $repo $relative) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $statePath = Join-Path $root 'stack-state.json'
    $state | ConvertTo-Json -Depth 5 -Compress | Set-Content -LiteralPath $statePath -Encoding utf8
    icacls.exe $statePath /inheritance:r /grant:r "*$($owner.Value):F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not protect the C15 stack metadata.' }
    $ready = $true
    Write-Output 'C15_REVIEW_STACK=READY'
    Write-Output "FRONTEND_URL=http://127.0.0.1:$FrontendPort/admin/#/automation"
    Write-Output "MY_WORK_URL=http://127.0.0.1:$FrontendPort/admin/#/my-work"
    Write-Output "API_HEALTH_URL=http://127.0.0.1:$ApiPort/health/ready"
    Write-Output "SYNTHETIC_CREDENTIAL_VIEWER=scripts/show_local_review_credentials.ps1 -FixturePath $fixturePath"
} finally {
    foreach ($name in $environmentNames) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    if (-not $ready) {
        foreach ($process in $started) { if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } }
        if ($databaseCreated) { Write-Output "ISOLATED_C15_DATABASE_RETAINED=$database" }
    }
}
