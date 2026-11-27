param(
    [ValidateRange(1024, 65535)][int]$PostgresPort = 55888,
    [ValidateRange(1024, 65535)][int]$ApiPort = 63006,
    [ValidateRange(1024, 65535)][int]$FrontendPort = 5177,
    [string]$ApplicationDatabase = 'infraspine_f06roles_a2ae10a9_test',
    [string]$SourceDatabase = 'infraspine_f07report_20261027_test',
    [string]$ReviewRoot = '.verification-local-review-20261027-native\f07-current-source-r3',
    [string]$CredentialFixture = '.verification-local-review-20261027-native\f07-synthetic-role-users.json',
    [string]$DepartmentGroupId = '',
    [switch]$StartPostgresCopy,
    [switch]$KeepAlive
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($PostgresPort -ne 55888 -or $ApiPort -ne 63006 -or $FrontendPort -ne 5177 -or
    $ApplicationDatabase -ne 'infraspine_f06roles_a2ae10a9_test' -or $SourceDatabase -ne 'infraspine_f07report_20261027_test') {
    throw 'The F07 review runner is pinned to the existing isolated application test DB and new synthetic reporting source.'
}
if ($DepartmentGroupId -and $DepartmentGroupId -notmatch '^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$') {
    throw 'DepartmentGroupId must be an existing synthetic department UUID in the disposable application database.'
}
if (-not [IO.Path]::IsPathRooted($ReviewRoot)) { $ReviewRoot = Join-Path $repoRoot $ReviewRoot }
if (-not [IO.Path]::IsPathRooted($CredentialFixture)) { $CredentialFixture = Join-Path $repoRoot $CredentialFixture }
$reviewRoot = [IO.Path]::GetFullPath($ReviewRoot)
$credentialFixture = [IO.Path]::GetFullPath($CredentialFixture)
if (Test-Path -LiteralPath $reviewRoot) { throw 'The F07 review folder already exists; inspect it before starting another stack.' }

$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$viteEntry = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
$localModel = Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
$pgCtl = 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe'
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$postgresData = Join-Path $repoRoot '.verification-local-review-20261027-native\f07-pg-synthetic-clone-20261027'
$required = @($python, $viteEntry, $localModel, $pgCtl, $psql, $postgresData, $credentialFixture)
foreach ($path in $required) { if (!(Test-Path -LiteralPath $path)) { throw 'An F07 source, model, local PostgreSQL or protected synthetic fixture is unavailable.' } }
$frontendEnvFiles = @(Get-ChildItem -LiteralPath (Join-Path $repoRoot 'frontend') -Force -File -Filter '.env*')
if ($frontendEnvFiles.Count -gt 0) { throw 'Refusing to start Vite while frontend-local environment files are present.' }

$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureAcl = Get-Acl -LiteralPath $credentialFixture
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureAcl.AreAccessRulesProtected -or -not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The F07 synthetic Admin fixture ACL is not restricted to this user, SYSTEM and local Administrators.'
}
if ((Get-Content -LiteralPath $credentialFixture -Raw | ConvertFrom-Json).admin.email -ne 'admin@example.com') {
    throw 'The protected F07 fixture does not match the existing synthetic application test accounts.'
}

& $pgCtl status -D $postgresData *> $null
$postgresStarted = $false
if ($LASTEXITCODE -ne 0) {
    if (-not $StartPostgresCopy) { throw 'The disposable PostgreSQL copy is not running; pass StartPostgresCopy to start only that copy.' }
    $pgLog = Join-Path $repoRoot '.verification-local-review-20261027-native\f07-pg-synthetic-clone-20261027.log'
    & $pgCtl start -D $postgresData -l $pgLog -o "-h 127.0.0.1 -p $PostgresPort -c listen_addresses=127.0.0.1" -w -t 90
    if ($LASTEXITCODE -ne 0) { throw 'The copied synthetic PostgreSQL cluster failed recovery/start; the retained source cluster was not touched.' }
    $postgresStarted = $true
}
foreach ($port in @($ApiPort, $FrontendPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "F07 loopback port $port is already occupied." }
}

$applicationUrl = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$ApplicationDatabase"
$reportingUrl = "postgresql+asyncpg://infraspine_f07_reader_20261027@127.0.0.1:$PostgresPort/$SourceDatabase"
$catalog = '{"erp.orders":{"order_id":"integer","department_code":"text","region":"text","amount":"numeric","ordered_on":"date"}}'
$departmentGrants = if ($DepartmentGroupId) {
    '{"' + $DepartmentGroupId.ToLowerInvariant() + '":[{"table":"erp.orders","column":"department_code","value":"finance"}]}'
} else { '' }
$appRevision = & $psql "postgresql://postgres@127.0.0.1:$PostgresPort/$ApplicationDatabase" -X -v ON_ERROR_STOP=1 -A -t -c 'SELECT version_num FROM alembic_version'
if ($LASTEXITCODE -ne 0 -or $appRevision.Trim() -ne '0052_user_totp_mfa') { throw 'The application test DB is not at the expected migration head; this runner does not migrate or reset it.' }
$sourceRead = & $psql "postgresql://infraspine_f07_reader_20261027@127.0.0.1:$PostgresPort/$SourceDatabase" -X -v ON_ERROR_STOP=1 -A -t -c "SELECT count(*) || '|' || sum(amount) FROM erp.orders"
if ($LASTEXITCODE -ne 0 -or $sourceRead.Trim() -ne '3|38.25') { throw 'The dedicated F07 reporting source failed its read-only preflight.' }

New-Item -ItemType Directory -Path $reviewRoot | Out-Null
$sid = [Security.Principal.SecurityIdentifier]::new([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
$acl = [System.Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($sid)
foreach ($sidText in $allowedSids) {
    $targetSid = [Security.Principal.SecurityIdentifier]::new($sidText)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $targetSid, [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $reviewRoot -AclObject $acl
$dataRoot = Join-Path $reviewRoot 'data'
$apiCwd = Join-Path $reviewRoot 'api-cwd'
$logs = Join-Path $reviewRoot 'logs'
$apiLogs = Join-Path $logs 'api'
$workerLogs = Join-Path $logs 'worker'
foreach ($directory in @($dataRoot, $apiCwd, $logs, $apiLogs, $workerLogs)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }

$environmentNames = @(
    'DATABASE_URL', 'APP_ENV', 'LLM_MOCK_MODE', 'MASTER_ENCRYPTION_KEY', 'MASTER_ENCRYPTION_KEY_ID',
    'SESSION_COOKIE_SECURE', 'RAG_DATA_DIR', 'RAG_MODELS_DIR', 'RAG_EMBEDDING_ALLOW_DOWNLOAD',
    'RAG_EMBEDDING_MODEL', 'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'RAG_VECTOR_STORE', 'RAG_INGESTION_MODE',
    'RAG_WORKER_ENABLED', 'RAG_INDEX_SYNC_SECONDS', 'RAG_USE_DOCLING', 'RAG_PARSE_ISOLATION', 'RAG_MIN_SIMILARITY',
    'RAG_DATABASE_ANALYSIS_URL', 'RAG_DATABASE_ANALYSIS_CATALOG', 'RAG_DATABASE_ANALYSIS_DEPARTMENT_GRANTS', 'RAG_DATABASE_ANALYSIS_SOURCE_LABEL',
    'RAG_DATABASE_ANALYSIS_STATEMENT_TIMEOUT_MS', 'RAG_DATABASE_ANALYSIS_MAX_ROWS',
    'LOG_LEVEL', 'LOG_DIR', 'PYTHONPATH', 'INFRASPINE_DEV_API_PROXY_TARGET'
)
$previous = @{}
foreach ($name in $environmentNames) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$started = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
try {
    $env:DATABASE_URL = $applicationUrl
    $env:APP_ENV = 'testing'
    $env:LLM_MOCK_MODE = 'true'
    $env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String([byte[]](1..32 | ForEach-Object { 107 }))
    $env:MASTER_ENCRYPTION_KEY_ID = 'v1'
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:RAG_DATA_DIR = $dataRoot
    $env:RAG_MODELS_DIR = Join-Path $repoRoot 'data\models'
    $env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
    $env:RAG_EMBEDDING_MODEL = $localModel
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    $env:RAG_VECTOR_STORE = 'faiss'
    $env:RAG_INGESTION_MODE = 'worker'
    $env:RAG_WORKER_ENABLED = 'false'
    $env:RAG_INDEX_SYNC_SECONDS = '0'
    $env:RAG_USE_DOCLING = 'false'
    $env:RAG_PARSE_ISOLATION = 'true'
    $env:RAG_MIN_SIMILARITY = '0.15'
    $env:RAG_DATABASE_ANALYSIS_URL = $reportingUrl
    $env:RAG_DATABASE_ANALYSIS_CATALOG = $catalog
    $env:RAG_DATABASE_ANALYSIS_DEPARTMENT_GRANTS = $departmentGrants
    $env:RAG_DATABASE_ANALYSIS_SOURCE_LABEL = 'Synthetic F07 ERP replica'
    $env:RAG_DATABASE_ANALYSIS_STATEMENT_TIMEOUT_MS = '5000'
    $env:RAG_DATABASE_ANALYSIS_MAX_ROWS = '100'
    $env:LOG_LEVEL = 'INFO'
    $env:LOG_DIR = $apiLogs
    $env:PYTHONPATH = $repoRoot

    $api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$ApiPort", '--no-access-log') `
        -WorkingDirectory $apiCwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $apiLogs 'stdout.log') -RedirectStandardError (Join-Path $apiLogs 'stderr.log')
    $started.Add($api)

    foreach ($name in @('RAG_DATABASE_ANALYSIS_URL', 'RAG_DATABASE_ANALYSIS_CATALOG', 'RAG_DATABASE_ANALYSIS_DEPARTMENT_GRANTS', 'RAG_DATABASE_ANALYSIS_SOURCE_LABEL')) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
    $env:RAG_WORKER_ENABLED = 'true'
    $env:LOG_DIR = $workerLogs
    $worker = Start-Process -FilePath $python -ArgumentList @('-m', 'app.worker', '--concurrency', '1') `
        -WorkingDirectory $apiCwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $workerLogs 'stdout.log') -RedirectStandardError (Join-Path $workerLogs 'stderr.log')
    $started.Add($worker)
    $env:RAG_WORKER_ENABLED = 'false'
    $env:LOG_DIR = $apiLogs
    Remove-Item -LiteralPath 'Env:DATABASE_URL' -ErrorAction SilentlyContinue
    $env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"
    $vite = Start-Process -FilePath $node -ArgumentList @(
        ('"' + $viteEntry + '"'), '--config', ('"' + (Join-Path $repoRoot 'frontend\vite.config.ts') + '"'),
        '--host', '127.0.0.1', '--strictPort', '--port', "$FrontendPort"
    ) -WorkingDirectory (Join-Path $repoRoot 'frontend') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs 'vite.stdout.log') -RedirectStandardError (Join-Path $logs 'vite.stderr.log')
    $started.Add($vite)

    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    $apiReady = $false
    $uiReady = $false
    while ([DateTime]::UtcNow -lt $deadline -and (-not $apiReady -or -not $uiReady)) {
        if (-not $api.HasExited) { try { $apiReady = (Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 2).status -eq 'ready' } catch {} }
        if (-not $vite.HasExited) { try { $uiReady = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 2).StatusCode -eq 200 } catch {} }
        if ($worker.HasExited) { throw 'The isolated F07 worker exited during startup. Inspect protected worker logs.' }
        if (-not $apiReady -or -not $uiReady) { Start-Sleep -Milliseconds 250 }
    }
    if (-not $apiReady -or -not $uiReady) { throw 'The isolated F07 current-source API or UI did not become ready. Inspect protected logs.' }

    $head = (& git -C $repoRoot rev-parse HEAD).Trim()
    $state = [ordered]@{
        apiPid = $api.Id; workerPid = $worker.Id; vitePid = $vite.Id; apiPort = $ApiPort; frontendPort = $FrontendPort; postgresPort = $PostgresPort
        applicationDatabase = $ApplicationDatabase; sourceDatabase = $SourceDatabase; migration = $appRevision.Trim()
        gitHead = $head; reviewUrl = "http://127.0.0.1:$FrontendPort/admin/"; dataRoot = $dataRoot; postgresData = $postgresData
        sourceRole = 'infraspine_f07_reader_20261027'; sourceLabel = 'Synthetic F07 ERP replica';
        departmentGrantConfigured = [bool]$DepartmentGroupId; startedAtUtc = [DateTime]::UtcNow.ToString('o')
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $reviewRoot 'stack-state.json') -Encoding utf8
    Write-Output 'F07_CURRENT_SOURCE_STACK=READY'
    Write-Output "REVIEW_URL=http://127.0.0.1:$FrontendPort/admin/"
    Write-Output "API_READY=http://127.0.0.1:$ApiPort/health/ready"
    Write-Output "APP_DATABASE=$ApplicationDatabase SOURCE_DATABASE=$SourceDatabase MIGRATION=$($appRevision.Trim())"
    Write-Output "STACK_STATE=$(Join-Path $reviewRoot 'stack-state.json')"
    if ($KeepAlive) {
        Write-Output 'STACK_SUPERVISOR=RUNNING (Ctrl+C stops only the API, worker and UI started by this runner.)'
        while ($true) { Start-Sleep -Seconds 5 }
    }
}
catch {
    foreach ($process in $started) { try { if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction Stop } } catch {} }
    if ($postgresStarted) { try { & $pgCtl stop -D $postgresData -m fast -w -t 30 *> $null } catch {} }
    throw
}
finally {
    foreach ($name in $environmentNames) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
}
