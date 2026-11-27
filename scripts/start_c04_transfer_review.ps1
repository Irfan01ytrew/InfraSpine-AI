param(
    [ValidateRange(1024,65535)][int]$ApiPort = 63012,
    [ValidateRange(1024,65535)][int]$FrontendPort = 5183,
    [ValidateRange(1024,65535)][int]$PostgresPort = 55888
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$reviewRoot = Join-Path $repoRoot '.verification-local-review-20261027-native\c04-catalog-transfer-current-20261027'
$database = 'infraspine_c04catalog_20261027_test'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$viteEntry = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
$pgBin = 'C:\Program Files\PostgreSQL\18\bin'
$createdb = Join-Path $pgBin 'createdb.exe'
$psql = Join-Path $pgBin 'psql.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source

function Protect-ReviewPath([string]$Path, [bool]$Directory) {
    $targetAcl = Get-Acl -LiteralPath $Path
    $targetAcl.SetAccessRuleProtection($true,$false)
    foreach ($rule in @($targetAcl.Access)) { [void]$targetAcl.RemoveAccessRuleSpecific($rule) }
    $inheritance = if ($Directory) { [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit } else { [Security.AccessControl.InheritanceFlags]::None }
    foreach ($principal in @(
        [Security.Principal.WindowsIdentity]::GetCurrent().User,
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
        [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    )) {
        $targetAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $principal,[Security.AccessControl.FileSystemRights]::FullControl,$inheritance,
            [Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow
        ))
    }
    Set-Acl -LiteralPath $Path -AclObject $targetAcl
}

if (!(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $viteEntry -PathType Leaf)) { throw 'Local Python or Vite runtime is unavailable.' }
if (!(Test-Path -LiteralPath $createdb -PathType Leaf) -or !(Test-Path -LiteralPath $psql -PathType Leaf)) { throw 'PostgreSQL client tools are unavailable.' }
$reviewRootExists = Test-Path -LiteralPath $reviewRoot

$occupied = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
    $_.LocalPort -in @($ApiPort,$FrontendPort) -and $_.LocalAddress -in @('127.0.0.1','0.0.0.0','::1','::')
})
if ($occupied.Count) { throw 'A requested C04 loopback port is already occupied; no existing listener will be changed.' }

$existing = & $psql -X -h 127.0.0.1 -p $PostgresPort -U postgres -d postgres -v ON_ERROR_STOP=1 -Atc "SELECT 1 FROM pg_database WHERE datname = '$database'" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'The retained loopback PostgreSQL test cluster is unavailable; no database was created.' }
 $databaseExists = (($existing -join '').Trim() -eq '1')
if ($databaseExists -ne $reviewRootExists) { throw 'C04 database and verification folder do not have the matching expected state; neither will be overwritten.' }

$resumeKnownEmpty = $false
if ($databaseExists) {
    foreach ($leaf in @('review-credentials.json','state.json','runtime-state.json')) {
        if (Test-Path -LiteralPath (Join-Path $reviewRoot $leaf)) { throw 'C04 review fixture or runtime already exists; the existing state will not be overwritten.' }
    }
    $emptyState = & $psql -X -h 127.0.0.1 -p $PostgresPort -U postgres -d $database -v ON_ERROR_STOP=1 -Atc "SELECT (SELECT version_num FROM alembic_version LIMIT 1)||'|'||(SELECT count(*) FROM users)||'|'||(SELECT count(*) FROM documents)||'|'||(SELECT count(*) FROM access_groups)" 2>$null
    if ($LASTEXITCODE -ne 0 -or ($emptyState -join '').Trim() -ne '0057_org_mfa_session_proof|0|0|0') {
        throw 'Existing C04 database is not the known-empty 0057 fixture state; it will not be changed.'
    }
    $reviewAcl = Get-Acl -LiteralPath $reviewRoot
    if (!$reviewAcl.AreAccessRulesProtected) { throw 'Existing C04 verification folder ACL is not protected; no credentials will be written.' }
    $resumeKnownEmpty = $true
} else {
    $null = New-Item -ItemType Directory -Path $reviewRoot
    Protect-ReviewPath $reviewRoot $true
}

$dataRoot = Join-Path $reviewRoot 'data'
$logRoot = Join-Path $reviewRoot 'logs'
$null = New-Item -ItemType Directory -Path $dataRoot,$logRoot -Force
$sourceProfile = Join-Path $repoRoot '.verification-local-review-20261027-native\f18-current-source\data\company-profile.json'
if (Test-Path -LiteralPath $sourceProfile -PathType Leaf) {
    Copy-Item -LiteralPath $sourceProfile -Destination (Join-Path $dataRoot 'company-profile.json')
}

$api = $null
$vite = $null
try {
    if (!$resumeKnownEmpty) {
        & $createdb -h 127.0.0.1 -p $PostgresPort -U postgres --template=template0 -E UTF8 $database
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the new empty C04 synthetic database.' }
    }

    $env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$database"
    $env:C04_REVIEW_DATABASE = $database
    $env:APP_ENV = 'testing'
    $env:DATABASE_POOL_SIZE = '1'
    $env:DATABASE_POOL_MAX_OVERFLOW = '0'
    $env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    $env:MASTER_ENCRYPTION_KEY_ID = 'c04-transfer-review'
    $env:ORG_MFA_REQUIRED = 'false'
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:LLM_MOCK_MODE = 'true'
    $env:RAG_DATA_DIR = $dataRoot
    $env:RAG_MODELS_DIR = Join-Path $repoRoot 'data\models'
    $env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
    $env:RAG_VECTOR_STORE = 'faiss'
    $env:RAG_WORKER_ENABLED = 'false'
    $env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"
    $env:LOG_LEVEL = 'INFO'

    if (!$resumeKnownEmpty) {
        & $python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw 'Current source migrations failed on the new C04 database; it is retained for inspection.' }
    }
    $credentialPath = Join-Path $reviewRoot 'review-credentials.json'
    $statePath = Join-Path $reviewRoot 'state.json'
    & $python (Join-Path $PSScriptRoot 'seed_c04_transfer_review.py') --credentials $credentialPath --state $statePath
    if ($LASTEXITCODE -ne 0) { throw 'C04 synthetic fixture seeding failed; the isolated DB is retained.' }
    Protect-ReviewPath $credentialPath $false
    Protect-ReviewPath $statePath $false

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
    if (!$healthy) { throw 'C04 API/UI did not become healthy; its isolated DB and files are retained.' }
    $apiOwner = Get-NetTCPConnection -State Listen -LocalPort $ApiPort -ErrorAction Stop | Where-Object LocalAddress -eq '127.0.0.1' | Select-Object -First 1
    if (!$apiOwner) { throw 'Could not identify the C04 loopback API listener.' }
    [ordered]@{
        apiPid = $apiOwner.OwningProcess; apiLauncherPid = $api.Id; frontendPid = $vite.Id
        apiPort = $ApiPort; frontendPort = $FrontendPort; databasePort = $PostgresPort
        database = $database; migration = '0057_org_mfa_session_proof'
        reviewUrl = "http://127.0.0.1:$FrontendPort/admin/"; startedAt = (Get-Date).ToString('o')
        sourceCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reviewRoot 'runtime-state.json') -Encoding utf8
    Write-Output 'C04 current-source synthetic API/UI are healthy on a new isolated PostgreSQL database.'
    Write-Output "Review URL: http://127.0.0.1:$FrontendPort/admin/"
    Write-Output "Migration: 0057_org_mfa_session_proof; org MFA policy disabled for this synthetic role-management test."
} catch {
    foreach ($service in @($vite,$api)) {
        if ($null -ne $service -and -not $service.HasExited) { Stop-Process -Id $service.Id -Force -ErrorAction SilentlyContinue }
    }
    throw
} finally {
    foreach ($name in @('DATABASE_URL','C04_REVIEW_DATABASE','APP_ENV','DATABASE_POOL_SIZE','DATABASE_POOL_MAX_OVERFLOW','MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID','ORG_MFA_REQUIRED','SESSION_COOKIE_SECURE','LLM_MOCK_MODE','RAG_DATA_DIR','RAG_MODELS_DIR','RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_VECTOR_STORE','RAG_WORKER_ENABLED','INFRASPINE_DEV_API_PROXY_TARGET','LOG_LEVEL')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
