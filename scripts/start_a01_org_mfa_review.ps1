param(
    [ValidateRange(1024,65535)][int]$ApiPort = 63011,
    [ValidateRange(1024,65535)][int]$FrontendPort = 5182,
    [ValidateRange(1024,65535)][int]$PostgresPort = 55888
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$reviewRoot = Join-Path $repoRoot '.verification-local-review-20261027-native\a01-org-mfa-current-20261027'
$statePath = Join-Path $reviewRoot 'runtime-state.json'
$dataRoot = Join-Path $reviewRoot 'data'
$logRoot = Join-Path $reviewRoot 'logs'
$database = 'infraspine_a01orgmfa_20261027_test'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$viteEntry = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
$pgBin = 'C:\Program Files\PostgreSQL\18\bin'
$createdb = Join-Path $pgBin 'createdb.exe'
$psql = Join-Path $pgBin 'psql.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$credentialFixture = Join-Path $repoRoot '.verification-local-review-20261027-native\f18-current-source\review-credentials.json'
$legacySessionPath = Join-Path $reviewRoot 'legacy-session.json'

function Protect-ReviewFile([string]$Path) {
    $fileAcl = Get-Acl -LiteralPath $Path
    $fileAcl.SetAccessRuleProtection($true,$false)
    foreach ($rule in @($fileAcl.Access)) { [void]$fileAcl.RemoveAccessRuleSpecific($rule) }
    foreach ($principal in @(
        [Security.Principal.WindowsIdentity]::GetCurrent().User,
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
        [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    )) {
        $fileAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $principal,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.InheritanceFlags]::None,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    Set-Acl -LiteralPath $Path -AclObject $fileAcl
}

if (!(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $viteEntry -PathType Leaf)) { throw 'Local Python or Vite runtime is unavailable.' }
if (!(Test-Path -LiteralPath $createdb -PathType Leaf) -or !(Test-Path -LiteralPath $psql -PathType Leaf)) { throw 'PostgreSQL client tools are unavailable.' }
if (!(Test-Path -LiteralPath $credentialFixture -PathType Leaf)) { throw 'Protected synthetic account fixture is unavailable.' }
if (Test-Path -LiteralPath $reviewRoot) { throw 'A01 runtime folder already exists; inspect it before starting another stack.' }

$null = New-Item -ItemType Directory -Path $reviewRoot
$acl = Get-Acl -LiteralPath $reviewRoot
$acl.SetAccessRuleProtection($true,$false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
foreach ($principal in @(
    [Security.Principal.WindowsIdentity]::GetCurrent().User,
    [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
    [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
)) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $principal,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    ))
}
Set-Acl -LiteralPath $reviewRoot -AclObject $acl

$existingDb = & $psql -X -h 127.0.0.1 -p $PostgresPort -U postgres -d postgres -v ON_ERROR_STOP=1 -Atc "SELECT 1 FROM pg_database WHERE datname = '$database'" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'The retained loopback PostgreSQL test cluster is not available; no database was created.' }
if (($existingDb -join '').Trim() -eq '1') { throw 'The A01 test database already exists; it will not be overwritten or reset.' }
$occupied = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
    $_.LocalPort -in @($ApiPort,$FrontendPort) -and $_.LocalAddress -in @('127.0.0.1','0.0.0.0','::1','::')
})
if ($occupied.Count) { throw 'A requested A01 loopback port is already occupied; no existing listener will be changed.' }

$api = $null
$vite = $null
try {
    & $createdb -h 127.0.0.1 -p $PostgresPort -U postgres --template=template0 -E UTF8 $database
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the new, empty A01 synthetic database.' }

    $env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$database"
    $env:APP_ENV = 'testing'
    $env:DATABASE_POOL_SIZE = '1'
    $env:DATABASE_POOL_MAX_OVERFLOW = '0'
    $env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    $env:MASTER_ENCRYPTION_KEY_ID = 'a01-org-mfa-review'
    $env:ORG_MFA_REQUIRED = 'true'
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:LLM_MOCK_MODE = 'true'
    $env:RAG_DATA_DIR = $dataRoot
    $env:RAG_MODELS_DIR = Join-Path $repoRoot 'data\models'
    $env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
    $env:RAG_EMBEDDING_MODEL = Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    $env:RAG_VECTOR_STORE = 'faiss'
    $env:RAG_WORKER_ENABLED = 'false'
    $env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"
    $env:LOG_LEVEL = 'INFO'

    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Alembic did not upgrade the new A01 database; it will be retained for inspection.' }
    & $python (Join-Path $PSScriptRoot 'seed_a01_org_mfa_review.py') --credential-fixture $credentialFixture --old-session-output $legacySessionPath
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic A01 account setup failed; the database and files are retained.' }
    Protect-ReviewFile $legacySessionPath

    $sourceProfile = Join-Path $repoRoot '.verification-local-review-20261027-native\f18-current-source\data\company-profile.json'
    if (Test-Path -LiteralPath $sourceProfile -PathType Leaf) {
        New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
        Copy-Item -LiteralPath $sourceProfile -Destination (Join-Path $dataRoot 'company-profile.json')
    }
    $null = New-Item -ItemType Directory -Path $logRoot -Force
    $api = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$ApiPort",'--no-access-log') -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'api.stdout.log') -RedirectStandardError (Join-Path $logRoot 'api.stderr.log')
    $vite = Start-Process -FilePath $node -ArgumentList @(('"' + $viteEntry + '"'),'--host','127.0.0.1','--strictPort','--port',"$FrontendPort") -WorkingDirectory (Join-Path $repoRoot 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'vite.stdout.log') -RedirectStandardError (Join-Path $logRoot 'vite.stderr.log')
    $healthy = $false
    for ($i = 0; $i -lt 45; $i++) {
        if ($api.HasExited -or $vite.HasExited) { break }
        try {
            $apiHealth = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 2
            $uiHealth = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 2
            if ($apiHealth.StatusCode -eq 200 -and $uiHealth.StatusCode -eq 200) { $healthy = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (!$healthy) { throw 'A01 API/UI did not become healthy. The new database and runtime files will be retained.' }
    $listener = @(Get-NetTCPConnection -State Listen -LocalPort $ApiPort -ErrorAction Stop | Where-Object LocalAddress -eq '127.0.0.1')
    if ($listener.Count -ne 1) { throw 'Could not identify the unique A01 loopback API process.' }
    $apiOwner = Get-Process -Id $listener[0].OwningProcess -ErrorAction Stop
    [ordered]@{
        apiPid = $apiOwner.Id; apiLauncherPid = $api.Id; frontendPid = $vite.Id
        apiPort = $ApiPort; frontendPort = $FrontendPort; databasePort = $PostgresPort
        database = $database; migration = '0057_org_mfa_session_proof'
        reviewUrl = "http://127.0.0.1:$FrontendPort/admin/"; startedAt = (Get-Date).ToString('o')
        sourceCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reviewRoot 'runtime-state.json') -Encoding utf8
    Write-Output 'A01 synthetic API/UI are healthy on a new isolated PostgreSQL database.'
    Write-Output "Review URL: http://127.0.0.1:$FrontendPort/admin/"
    Write-Output "Migration: 0057_org_mfa_session_proof; policy: ORG_MFA_REQUIRED=true"
} catch {
    foreach ($service in @($vite,$api)) {
        if ($null -ne $service -and -not $service.HasExited) { Stop-Process -Id $service.Id -Force -ErrorAction SilentlyContinue }
    }
    throw
} finally {
    foreach ($name in @('DATABASE_URL','APP_ENV','DATABASE_POOL_SIZE','DATABASE_POOL_MAX_OVERFLOW','MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID','ORG_MFA_REQUIRED','SESSION_COOKIE_SECURE','LLM_MOCK_MODE','RAG_DATA_DIR','RAG_MODELS_DIR','RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_EMBEDDING_MODEL','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','RAG_VECTOR_STORE','RAG_WORKER_ENABLED','INFRASPINE_DEV_API_PROXY_TARGET','LOG_LEVEL')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
