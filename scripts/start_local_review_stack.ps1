param(
    [ValidateRange(1024, 65535)]
    [int]$PostgresPort = 55447,
    [string]$PostgresDataDirectory = '',
    [string]$DatabaseName = 'infraspine_f12_browser_test',
    [string]$ReviewRoot = '',
    [string]$CredentialFixturePath = '',
    [switch]$UseDatabaseLlm,
    [ValidateLength(1, 32)]
    [string]$MasterEncryptionKeyId = 'local-review-live-v1',
    [ValidateRange(1024, 65535)]
    [int]$ApiPort = 63002,
    [ValidateRange(1024, 65535)]
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($ReviewRoot)) {
    $ReviewRoot = Join-Path $repoRoot '.verification-local-review-20261026'
} elseif (-not [IO.Path]::IsPathRooted($ReviewRoot)) {
    $ReviewRoot = Join-Path $repoRoot $ReviewRoot
}
$reviewRoot = [IO.Path]::GetFullPath($ReviewRoot)
if ([string]::IsNullOrWhiteSpace($CredentialFixturePath)) {
    $CredentialFixturePath = Join-Path $reviewRoot 'qa-users.json'
} elseif (-not [IO.Path]::IsPathRooted($CredentialFixturePath)) {
    $CredentialFixturePath = Join-Path $repoRoot $CredentialFixturePath
}
$credentialFixture = [IO.Path]::GetFullPath($CredentialFixturePath)
$dataRoot = Join-Path $reviewRoot 'data'
$stackRunId = (Get-Date -Format 'yyyyMMddTHHmmssfff') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$logRoot = Join-Path $reviewRoot (Join-Path 'logs' "stack-$stackRunId")
$statePath = Join-Path $reviewRoot "stack-processes-$ApiPort-$FrontendPort-$stackRunId.json"
$databaseUrl = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$DatabaseName"
if ([string]::IsNullOrWhiteSpace($PostgresDataDirectory)) {
    $PostgresDataDirectory = Join-Path $repoRoot '.verification-pg-f19-20261026\data'
} elseif (-not [IO.Path]::IsPathRooted($PostgresDataDirectory)) {
    $PostgresDataDirectory = Join-Path $repoRoot $PostgresDataDirectory
}
$databaseData = (Resolve-Path -LiteralPath $PostgresDataDirectory).Path
$pgCtl = 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$nodeCommand = Get-Command node.exe -ErrorAction Stop
$powershellCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue
if ($null -eq $powershellCommand) { $powershellCommand = Get-Command powershell.exe -ErrorAction Stop }
$powershell = $powershellCommand.Source
$workerLauncher = Join-Path $PSScriptRoot 'run_local_review_worker.ps1'
$viteEntry = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'

if (!(Test-Path -LiteralPath $reviewRoot -PathType Container)) {
    throw 'The protected synthetic QA directory has not been prepared.'
}
if (!(Test-Path -LiteralPath $credentialFixture -PathType Leaf)) {
    throw 'The protected synthetic Admin fixture is unavailable.'
}
$fixtureAcl = Get-Acl -LiteralPath $credentialFixture
if (-not $fixtureAcl.AreAccessRulesProtected) { throw 'The synthetic credentials file is not protected from inheritance.' }
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The synthetic credentials file ACL is broader than this user, SYSTEM, and local Administrators.'
}
if (!(Test-Path -LiteralPath (Join-Path $dataRoot 'models'))) {
    New-Item -ItemType Directory -Path (Join-Path $dataRoot 'models') -Force | Out-Null
}
if (!(Test-Path -LiteralPath (Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41\modules.json'))) {
    throw 'The local embedding model snapshot is incomplete; no download will be attempted.'
}
if (!(Test-Path -LiteralPath $viteEntry -PathType Leaf)) { throw 'Vite is not installed in frontend/node_modules.' }
if (!(Test-Path -LiteralPath $workerLauncher -PathType Leaf)) { throw 'The isolated worker launcher is unavailable.' }
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'The repository Python 3.12 environment is unavailable.' }
if (!(Test-Path -LiteralPath $databaseData -PathType Container)) { throw 'The isolated PostgreSQL data directory is unavailable.' }

& $pgCtl status -D $databaseData *> $null
if ($LASTEXITCODE -ne 0) { throw 'The existing isolated PostgreSQL cluster is not running; startup was not attempted.' }

$reviewPorts = @($FrontendPort, $ApiPort) | Sort-Object -Unique
$occupied = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in $reviewPorts -and $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::1', '::') })
if ($occupied.Count -gt 0) { throw 'A review-stack port is already occupied; no existing process was changed.' }
if (Test-Path -LiteralPath $statePath) { throw 'A prior local review stack state file exists; inspect it before starting another stack.' }

New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
# The parent has a protected owner/SYSTEM/Administrators-only DACL; inherit it for this non-secret PID file.
Set-Content -LiteralPath $statePath -Value '{}' -Encoding utf8

$env:DATABASE_URL = $databaseUrl
# Automated review/tests default to a mock-only testing process. A database-backed
# runtime stack explicitly opts into development mode and a live provider.
$env:APP_ENV = if ($UseDatabaseLlm) { 'development' } else { 'testing' }
if ($UseDatabaseLlm) {
    # A database-backed QA stack needs the same encryption key after a process restart. Store it only
    # beneath the already protected review root, with an explicit owner/SYSTEM/Administrators DACL.
    $reviewAcl = Get-Acl -LiteralPath $reviewRoot
    $reviewRules = @($reviewAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
    if (-not $reviewAcl.AreAccessRulesProtected -or
        @($reviewRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
        throw 'Database-backed LLM mode requires a review directory restricted to this user, SYSTEM, and local Administrators.'
    }
    $masterKeyPath = Join-Path $reviewRoot 'master-encryption-key.txt'
    if (-not (Test-Path -LiteralPath $masterKeyPath -PathType Leaf)) {
        $keyGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
        $keyBytes = New-Object byte[] 32
        try {
            $keyGenerator.GetBytes($keyBytes)
            $generatedKey = [Convert]::ToBase64String($keyBytes)
            [IO.File]::WriteAllText($masterKeyPath, $generatedKey, [Text.Encoding]::ASCII)
        } finally {
            $keyGenerator.Dispose()
            [Array]::Clear($keyBytes, 0, $keyBytes.Length)
        }
        $keyAcl = New-Object System.Security.AccessControl.FileSecurity
        $keyAcl.SetAccessRuleProtection($true, $false)
        $keyAcl.SetOwner([Security.Principal.SecurityIdentifier]::new($allowedSids[0]))
        foreach ($sidValue in $allowedSids) {
            $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
                $sid, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow
            )
            $keyAcl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $masterKeyPath -AclObject $keyAcl
    }
    $keyAcl = Get-Acl -LiteralPath $masterKeyPath
    $keyRules = @($keyAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
    if (-not $keyAcl.AreAccessRulesProtected -or
        @($keyRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
        throw 'The database-backed LLM encryption key file ACL is broader than this user, SYSTEM, and local Administrators.'
    }
    $masterKey = [IO.File]::ReadAllText($masterKeyPath, [Text.Encoding]::ASCII).Trim()
    try { $decodedKey = [Convert]::FromBase64String($masterKey) } catch { throw 'The protected review encryption key is invalid.' }
    if ($decodedKey.Length -ne 32) { throw 'The protected review encryption key must contain exactly 32 bytes.' }
    [Array]::Clear($decodedKey, 0, $decodedKey.Length)
    $env:LLM_MOCK_MODE = 'false'
    $env:MASTER_ENCRYPTION_KEY = $masterKey
    $env:MASTER_ENCRYPTION_KEY_ID = $MasterEncryptionKeyId
} else {
    # The isolated offline review mode uses a fresh process-only key and never sends chat data to a provider.
    $env:LLM_MOCK_MODE = 'true'
    $keyGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    $keyBytes = New-Object byte[] 32
    $keyGenerator.GetBytes($keyBytes)
    $keyGenerator.Dispose()
    $env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String($keyBytes)
    [Array]::Clear($keyBytes, 0, $keyBytes.Length)
    $env:MASTER_ENCRYPTION_KEY_ID = 'f12-local-review'
}
$env:SESSION_COOKIE_SECURE = 'false'
$env:RAG_DATA_DIR = $dataRoot
$env:RAG_MODELS_DIR = (Join-Path $repoRoot 'data\models')
$env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
$env:RAG_EMBEDDING_MODEL = (Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41')
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:RAG_VECTOR_STORE = 'faiss'
$env:RAG_INGESTION_MODE = 'worker'
$env:RAG_WORKER_ENABLED = 'false'
$env:RAG_INDEX_SYNC_SECONDS = '2'
$env:RAG_USE_DOCLING = 'false'
$env:RAG_PARSE_ISOLATION = 'true'
$env:LOG_LEVEL = 'INFO'
$env:LOG_DIR = Join-Path $logRoot 'api'
$env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"
$env:INFRASPINE_LOCAL_REVIEW_WORKER_LOG_DIR = Join-Path $logRoot 'worker'
$env:INFRASPINE_LOCAL_REVIEW_RUNTIME_ROOT = $reviewRoot

New-Item -ItemType Directory -Path $env:LOG_DIR -Force | Out-Null
$api = Start-Process -FilePath $python -ArgumentList @(
    '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$ApiPort", '--no-access-log'
) -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logRoot 'api.stdout.log') `
    -RedirectStandardError (Join-Path $logRoot 'api.stderr.log')

$env:LOG_DIR = Join-Path $logRoot 'worker'
$worker = Start-Process -FilePath $powershell -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $workerLauncher + '"')
) `
    -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logRoot 'worker.stdout.log') `
    -RedirectStandardError (Join-Path $logRoot 'worker.stderr.log')

$env:LOG_DIR = Join-Path $logRoot 'api'
$vite = Start-Process -FilePath $nodeCommand.Source -ArgumentList @(
    ('"' + $viteEntry + '"'), '--host', '127.0.0.1', '--strictPort', '--port', "$FrontendPort"
) -WorkingDirectory (Join-Path $repoRoot 'frontend') -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logRoot 'vite.stdout.log') `
    -RedirectStandardError (Join-Path $logRoot 'vite.stderr.log')

$state = [ordered]@{
    apiPid = $api.Id
    workerPid = $worker.Id
    vitePid = $vite.Id
    apiPort = $ApiPort
    frontendPort = $FrontendPort
    llmMode = $(if ($UseDatabaseLlm) { 'database' } else { 'mock' })
    startedAt = (Get-Date).ToString('o')
    dataPath = $dataRoot
}
$state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8

Write-Output 'Local synthetic review services launched on loopback.'
Write-Output "Frontend: http://127.0.0.1:$FrontendPort/"
Write-Output "Process IDs saved in the protected state file: $statePath"
