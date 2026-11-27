param(
    [ValidateRange(1024, 65535)][int]$PostgresPort = 55621,
    [ValidateRange(1024, 65535)][int]$ApiPort = 63005,
    [ValidateRange(1024, 65535)][int]$FrontendPort = 5176,
    [string]$DatabaseName = 'infraspine_f06roles_a2ae10a9_test',
    [string]$ReviewRoot = '.verification-local-review-20261027-native\f06-current-source',
    [switch]$KeepAlive,
    [switch]$StartPostgres
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($DatabaseName -ne 'infraspine_f06roles_a2ae10a9_test' -or $PostgresPort -ne 55621 -or $ApiPort -ne 63005 -or $FrontendPort -ne 5176) {
    throw 'This runner is pinned to the existing isolated F06 test database and unused current-source loopback ports.'
}
if (-not [IO.Path]::IsPathRooted($ReviewRoot)) { $ReviewRoot = Join-Path $repoRoot $ReviewRoot }
$reviewRoot = [IO.Path]::GetFullPath($ReviewRoot)
if (Test-Path -LiteralPath $reviewRoot) { throw 'The F06 current-source review folder already exists; inspect it before starting another stack.' }

$nativeRoot = Join-Path $repoRoot '.verification-local-review-20261027-native'
$reviewFixture = Join-Path $nativeRoot 'qa-users.json'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$viteEntry = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
$localModel = Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
$pgCtl = 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe'
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$postgresData = Join-Path $repoRoot '.verification-voice-native-pg-20261027\data'
foreach ($required in @($python, $viteEntry, $localModel, $pgCtl, $psql, $postgresData, $reviewFixture)) {
    if (!(Test-Path -LiteralPath $required)) { throw 'A required source, local model, protected fixture, or retained PostgreSQL runtime is unavailable.' }
}

$fixtureAcl = Get-Acl -LiteralPath $reviewFixture
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureAcl.AreAccessRulesProtected -or -not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The source account fixture ACL is not restricted to this user, SYSTEM and local Administrators.'
}

$postgresStartedByRunner = $false
& $pgCtl status -D $postgresData *> $null
if ($LASTEXITCODE -ne 0) {
    if (-not $StartPostgres) { throw 'The existing isolated PostgreSQL cluster is not running; rerun only with -StartPostgres after reviewing the local test-cluster state.' }
    $postgresLog = Join-Path $nativeRoot 'f06-current-source-postgres.log'
    & $pgCtl start -D $postgresData -l $postgresLog -o "-h 127.0.0.1 -p $PostgresPort" -w -t 20
    if ($LASTEXITCODE -ne 0) { throw 'The existing isolated PostgreSQL cluster did not start cleanly; no database reset or reinitialization was attempted.' }
    $postgresStartedByRunner = $true
}
foreach ($port in @($ApiPort, $FrontendPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Current-source review port $port is already occupied; no existing process was changed."
    }
}

$databaseUrl = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$DatabaseName"
$revision = & $psql "postgresql://postgres@127.0.0.1:$PostgresPort/$DatabaseName" -X -v ON_ERROR_STOP=1 -A -t -c "SELECT version_num FROM alembic_version"
if ($LASTEXITCODE -ne 0 -or $revision.Trim() -ne '0052_user_totp_mfa') {
    throw 'The dedicated F06 test database is not at migration 0052; this runner does not migrate or reset databases.'
}
$dbSummary = & $psql "postgresql://postgres@127.0.0.1:$PostgresPort/$DatabaseName" -X -v ON_ERROR_STOP=1 -A -t -c "SELECT 'role|' || role || '|' || count(*) FROM users GROUP BY role ORDER BY role; SELECT 'ready_docs|' || count(*) FROM documents WHERE status = 'ready'; SELECT 'review_revisions|' || count(*) FROM financial_form_review_revisions"
if ($LASTEXITCODE -ne 0 -or -not ($dbSummary -match 'role\|manager\|1') -or -not ($dbSummary -match 'role\|user\|1') -or -not ($dbSummary -match 'review_revisions\|1')) {
    throw 'The isolated F06 test database lacks the expected synthetic roles or reviewed-record fixture.'
}

$pythonCredentials = @'
import ast, base64, json
from pathlib import Path
tree = ast.parse(Path("tests/conftest.py").read_text(encoding="utf-8"))
wanted = {"ADMIN_EMAIL", "USER_EMAIL", "USER2_EMAIL", "PASSWORD"}
values = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
        if name in wanted:
            values[name] = ast.literal_eval(node.value)
if set(values) != wanted:
    raise SystemExit("Synthetic test account constants are incomplete.")
values["TEST_MASTER_KEY"] = base64.b64encode(b"k" * 32).decode("ascii")
print(json.dumps(values))
'@
$credentialsJson = $pythonCredentials | & $python -B -
if ($LASTEXITCODE -ne 0) { throw 'Could not read the synthetic test account identifiers from the test fixture source.' }
$credentials = $credentialsJson | ConvertFrom-Json

New-Item -ItemType Directory -Path $reviewRoot | Out-Null
$currentSid = [Security.Principal.SecurityIdentifier]::new([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
$dirAcl = [System.Security.AccessControl.DirectorySecurity]::new()
$dirAcl.SetAccessRuleProtection($true, $false)
$dirAcl.SetOwner($currentSid)
foreach ($sidText in $allowedSids) {
    $sid = [Security.Principal.SecurityIdentifier]::new($sidText)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$dirAcl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $reviewRoot -AclObject $dirAcl

$dataRoot = Join-Path $reviewRoot 'data'
$apiCwd = Join-Path $reviewRoot 'api-cwd'
$workerCwd = Join-Path $reviewRoot 'worker-cwd'
$viteEnvDir = Join-Path $reviewRoot 'vite-env'
$logRoot = Join-Path $reviewRoot 'logs'
$apiLog = Join-Path $logRoot 'api'
$workerLog = Join-Path $logRoot 'worker'
foreach ($directory in @($dataRoot, $apiCwd, $workerCwd, $viteEnvDir, $apiLog, $workerLog)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$fixturePath = Join-Path $reviewRoot 'f06-role-users.json'
$fixture = [ordered]@{
    admin = [ordered]@{ email = [string]$credentials.ADMIN_EMAIL; password = [string]$credentials.PASSWORD; role = 'admin' }
    manager = [ordered]@{ email = [string]$credentials.USER2_EMAIL; password = [string]$credentials.PASSWORD; role = 'manager' }
    employee = [ordered]@{ email = [string]$credentials.USER_EMAIL; password = [string]$credentials.PASSWORD; role = 'employee' }
}
$fixture | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $fixturePath -Encoding utf8
$fileAcl = [System.Security.AccessControl.FileSecurity]::new()
$fileAcl.SetAccessRuleProtection($true, $false)
$fileAcl.SetOwner($currentSid)
foreach ($sidText in $allowedSids) {
    $sid = [Security.Principal.SecurityIdentifier]::new($sidText)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid, [System.Security.AccessControl.FileSystemRights]::FullControl, [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$fileAcl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $fixturePath -AclObject $fileAcl
$fixture = $null

$envNames = @(
    'DATABASE_URL', 'APP_ENV', 'LLM_MOCK_MODE', 'MASTER_ENCRYPTION_KEY', 'MASTER_ENCRYPTION_KEY_ID',
    'SESSION_COOKIE_SECURE', 'RAG_DATA_DIR', 'RAG_MODELS_DIR', 'RAG_EMBEDDING_ALLOW_DOWNLOAD',
    'RAG_EMBEDDING_MODEL', 'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'RAG_VECTOR_STORE', 'RAG_INGESTION_MODE',
    'RAG_WORKER_ENABLED', 'RAG_INDEX_SYNC_SECONDS', 'RAG_USE_DOCLING', 'RAG_PARSE_ISOLATION',
    'LOG_LEVEL', 'LOG_DIR', 'PYTHONPATH', 'INFRASPINE_DEV_API_PROXY_TARGET'
)
$previous = @{}
foreach ($name in $envNames) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$started = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
try {
    $env:DATABASE_URL = $databaseUrl
    $env:APP_ENV = 'testing'
    $env:LLM_MOCK_MODE = 'true'
    $env:MASTER_ENCRYPTION_KEY = [string]$credentials.TEST_MASTER_KEY
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
    $env:RAG_INDEX_SYNC_SECONDS = '2'
    $env:RAG_USE_DOCLING = 'false'
    $env:RAG_PARSE_ISOLATION = 'true'
    $env:LOG_LEVEL = 'INFO'
    $env:LOG_DIR = $apiLog
    $env:PYTHONPATH = $repoRoot
    $env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"

    $api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$ApiPort", '--no-access-log') `
        -WorkingDirectory $apiCwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $apiLog 'stdout.log') -RedirectStandardError (Join-Path $apiLog 'stderr.log')
    $started.Add($api)

    $env:RAG_WORKER_ENABLED = 'true'
    $env:LOG_DIR = $workerLog
    $worker = Start-Process -FilePath $python -ArgumentList @('-m', 'app.worker') -WorkingDirectory $workerCwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $workerLog 'stdout.log') -RedirectStandardError (Join-Path $workerLog 'stderr.log')
    $started.Add($worker)

    $env:RAG_WORKER_ENABLED = 'false'
    $env:LOG_DIR = $apiLog
    $vite = Start-Process -FilePath $node -ArgumentList @(
        ('"' + $viteEntry + '"'), '--config', ('"' + (Join-Path $repoRoot 'frontend\vite.config.ts') + '"'),
        '--host', '127.0.0.1', '--strictPort', '--port', "$FrontendPort"
    ) -WorkingDirectory (Join-Path $repoRoot 'frontend') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot 'vite.stdout.log') -RedirectStandardError (Join-Path $logRoot 'vite.stderr.log')
    $started.Add($vite)

    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    $apiReady = $false
    $uiReady = $false
    while ([DateTime]::UtcNow -lt $deadline -and (-not $apiReady -or -not $uiReady)) {
        if (-not $api.HasExited) {
            try { $apiReady = (Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 2).status -eq 'ready' } catch {}
        }
        if (-not $vite.HasExited) {
            try { $uiReady = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 2).StatusCode -eq 200 } catch {}
        }
        if (-not $apiReady -or -not $uiReady) { Start-Sleep -Milliseconds 250 }
    }
    if (-not $apiReady -or -not $uiReady -or $worker.HasExited) {
        throw 'The isolated current-source API, worker, or UI did not become ready within 45 seconds. Inspect the protected log directory.'
    }

    $head = (& git -C $repoRoot rev-parse HEAD).Trim()
    $state = [ordered]@{
        apiPid = $api.Id; workerPid = $worker.Id; vitePid = $vite.Id
        apiPort = $ApiPort; frontendPort = $FrontendPort; postgresPort = $PostgresPort
        database = $DatabaseName; migration = $revision.Trim(); gitHead = $head
        reviewUrl = "http://127.0.0.1:$FrontendPort/admin/"; startedAtUtc = [DateTime]::UtcNow.ToString('o')
        dataRoot = $dataRoot; fixturePath = $fixturePath; logRoot = $logRoot; postgresStartedByRunner = $postgresStartedByRunner
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $reviewRoot 'stack-state.json') -Encoding utf8
    Write-Output 'F06_CURRENT_SOURCE_STACK=READY'
    Write-Output "REVIEW_URL=http://127.0.0.1:$FrontendPort/admin/"
    Write-Output "API_READY=http://127.0.0.1:$ApiPort/health/ready"
    Write-Output "DATABASE=$DatabaseName MIGRATION=$($revision.Trim())"
    Write-Output "STACK_STATE=$(Join-Path $reviewRoot 'stack-state.json')"
    Write-Output "PROTECTED_FIXTURE=$fixturePath"
    if ($KeepAlive) {
        Write-Output 'STACK_SUPERVISOR=RUNNING (Ctrl+C stops only the processes started by this runner.)'
        while ($true) { Start-Sleep -Seconds 5 }
    }
}
catch {
    foreach ($process in $started) {
        try { if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction Stop } } catch {}
    }
    if ($postgresStartedByRunner) {
        try { & $pgCtl stop -D $postgresData -m fast -w -t 20 *> $null } catch {}
    }
    throw
}
finally {
    foreach ($name in $envNames) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    if ($null -ne $credentials) {
        foreach ($name in @('ADMIN_EMAIL', 'USER_EMAIL', 'USER2_EMAIL', 'PASSWORD', 'TEST_MASTER_KEY')) { $credentials.$name = $null }
    }
    $credentials = $null
    $credentialsJson = $null
}
