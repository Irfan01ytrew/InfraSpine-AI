param(
    [ValidateRange(1024,65535)][int]$ApiPort = 63010,
    [ValidateRange(1024,65535)][int]$FrontendPort = 5181,
    [ValidateRange(1024,65535)][int]$PostgresPort = 55888
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$reviewRoot = Join-Path $repoRoot '.verification-local-review-20261027-native\f18-current-source\f20-current-source'
$statePath = Join-Path $reviewRoot 'runtime-state.json'
$dataRoot = Join-Path $reviewRoot 'data'
$logRoot = Join-Path $reviewRoot 'logs'
$database = 'infraspine_f18voice_20261027_a91e_test'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$viteEntry = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$pgCtl = 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe'
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source

if (!(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $viteEntry -PathType Leaf)) {
    throw 'Local Python or Vite runtime is missing; no service was launched.'
}
if (!(Test-Path -LiteralPath $chrome -PathType Leaf)) { throw 'Local Chrome is missing; no service was launched.' }
if (Test-Path -LiteralPath $statePath) { throw 'F20 runtime state already exists. Inspect it before attempting another start.' }
if (!(Test-Path -LiteralPath $reviewRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $reviewRoot -Force | Out-Null
}
$runtimeAcl = Get-Acl -LiteralPath $reviewRoot
$runtimeAcl.SetAccessRuleProtection($true,$false)
foreach ($rule in @($runtimeAcl.Access)) { [void]$runtimeAcl.RemoveAccessRuleSpecific($rule) }
$runtimePrincipals = @(
    [Security.Principal.WindowsIdentity]::GetCurrent().User,
    [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
    [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
)
foreach ($principal in $runtimePrincipals) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $principal,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $runtimeAcl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $reviewRoot -AclObject $runtimeAcl
if (!(Test-Path -LiteralPath (Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41\modules.json'))) {
    throw 'Local embedding model snapshot is unavailable; no download will be attempted.'
}
if (!(Test-Path -LiteralPath $psql -PathType Leaf)) { throw 'PostgreSQL read-only client is unavailable.' }
$revision = & $psql -X -h 127.0.0.1 -p $PostgresPort -U postgres -d $database -v ON_ERROR_STOP=1 -Atc 'SELECT version_num FROM alembic_version' 2>$null
if ($LASTEXITCODE -ne 0 -or ($revision -join '').Trim() -ne '0056_legacy_browser_approval') {
    throw 'The named synthetic database is unavailable or not at migration 0056; no service was launched.'
}
$occupied = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @($ApiPort, $FrontendPort) -and $_.LocalAddress -in @('127.0.0.1','0.0.0.0','::1','::') })
if ($occupied.Count -gt 0) { throw 'A selected F20 review port is occupied; existing processes were not changed.' }
$credentialFixture = Join-Path $repoRoot '.verification-local-review-20261027-native\f18-current-source\review-credentials.json'
if (!(Test-Path -LiteralPath $credentialFixture -PathType Leaf)) {
    throw 'The protected synthetic local review fixture is unavailable.'
}
$credentialAcl = Get-Acl -LiteralPath $credentialFixture
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18','S-1-5-32-544')
$credentialRules = @($credentialAcl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]))
if (-not $credentialAcl.AreAccessRulesProtected -or -not $credentialRules -or
    @($credentialRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The synthetic review fixture is not restricted to this user, SYSTEM and local Administrators.'
}

$env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$database"
$env:APP_ENV = 'testing'
$env:DATABASE_POOL_SIZE = '1'
$env:DATABASE_POOL_MAX_OVERFLOW = '0'
$env:LLM_MOCK_MODE = 'true'
$env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$env:MASTER_ENCRYPTION_KEY_ID = 'f20-browser-review'
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
$env:RAG_LEGACY_BROWSER_ENABLED = 'true'
$env:RAG_LEGACY_BROWSER_ALLOWED_ORIGINS = 'http://127.0.0.1:63120'
$env:RAG_LEGACY_BROWSER_EXECUTABLE_PATH = $chrome
$env:RAG_LEGACY_BROWSER_TIMEOUT_SECONDS = '25'
$env:RAG_LEGACY_BROWSER_MAX_REQUESTS = '40'
$env:RAG_LEGACY_BROWSER_MAX_RESPONSE_BYTES = '8388608'
$env:RAG_LEGACY_BROWSER_MAX_RESULT_CHARS = '12000'
$env:RAG_LEGACY_BROWSER_ALLOW_PRIVATE_NETWORKS = 'false'
$env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"
$env:LOG_LEVEL = 'INFO'

New-Item -ItemType Directory -Path $dataRoot,$logRoot -Force | Out-Null
$sourceCompanyProfile = Join-Path $repoRoot '.verification-local-review-20261027-native\f18-current-source\data\company-profile.json'
$destinationCompanyProfile = Join-Path $dataRoot 'company-profile.json'
if (!(Test-Path -LiteralPath $destinationCompanyProfile) -and (Test-Path -LiteralPath $sourceCompanyProfile -PathType Leaf)) {
    Copy-Item -LiteralPath $sourceCompanyProfile -Destination $destinationCompanyProfile
}
$api = $null
$vite = $null
try {
    $api = Start-Process -FilePath $python -ArgumentList @(
        '-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$ApiPort",'--no-access-log'
    ) -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot 'api.stdout.log') `
        -RedirectStandardError (Join-Path $logRoot 'api.stderr.log')
    $vite = Start-Process -FilePath $node -ArgumentList @(
        ('"' + $viteEntry + '"'),'--host','127.0.0.1','--strictPort','--port',"$FrontendPort"
    ) -WorkingDirectory (Join-Path $repoRoot 'frontend') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot 'vite.stdout.log') `
        -RedirectStandardError (Join-Path $logRoot 'vite.stderr.log')

    $healthy = $false
    for ($i = 0; $i -lt 40; $i++) {
        if ($api.HasExited -or $vite.HasExited) { break }
        try {
            $apiHealth = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 2
            $uiHealth = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 2
            if ($apiHealth.StatusCode -eq 200 -and $uiHealth.StatusCode -eq 200) { $healthy = $true; break }
        } catch { Start-Sleep -Milliseconds 750 }
    }
    if (-not $healthy) { throw 'F20 current-source API/UI did not become healthy; only newly launched processes will be stopped.' }
    $apiListener = @(Get-NetTCPConnection -State Listen -LocalPort $ApiPort -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq '127.0.0.1' })
    if ($apiListener.Count -ne 1) { throw 'Could not identify the unique loopback F20 API process.' }
    $apiOwner = Get-Process -Id $apiListener[0].OwningProcess -ErrorAction Stop
    [ordered]@{
        apiPid = $apiOwner.Id
        apiLauncherPid = $api.Id
        frontendPid = $vite.Id
        apiPort = $ApiPort
        frontendPort = $FrontendPort
        databasePort = $PostgresPort
        database = $database
        migration = '0056_legacy_browser_approval'
        reviewUrl = "http://127.0.0.1:$FrontendPort/admin/"
        startedAt = (Get-Date).ToString('o')
        sourceCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
    } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
    Write-Output 'F20 current-source synthetic API/UI are healthy on loopback.'
    Write-Output "Review URL: http://127.0.0.1:$FrontendPort/admin/"
    Write-Output "Runtime state: $statePath"
} catch {
    foreach ($service in @($vite,$api)) {
        if ($null -ne $service -and -not $service.HasExited) { Stop-Process -Id $service.Id -Force -ErrorAction SilentlyContinue }
    }
    throw
} finally {
    foreach ($name in @(
        'DATABASE_URL','APP_ENV','DATABASE_POOL_SIZE','DATABASE_POOL_MAX_OVERFLOW','LLM_MOCK_MODE','MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID',
        'SESSION_COOKIE_SECURE','RAG_DATA_DIR','RAG_MODELS_DIR','RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_EMBEDDING_MODEL',
        'HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','RAG_VECTOR_STORE','RAG_INGESTION_MODE','RAG_WORKER_ENABLED',
        'RAG_INDEX_SYNC_SECONDS','RAG_LEGACY_BROWSER_ENABLED','RAG_LEGACY_BROWSER_ALLOWED_ORIGINS',
        'RAG_LEGACY_BROWSER_EXECUTABLE_PATH','RAG_LEGACY_BROWSER_TIMEOUT_SECONDS','RAG_LEGACY_BROWSER_MAX_REQUESTS',
        'RAG_LEGACY_BROWSER_MAX_RESPONSE_BYTES','RAG_LEGACY_BROWSER_MAX_RESULT_CHARS',
        'RAG_LEGACY_BROWSER_ALLOW_PRIVATE_NETWORKS','INFRASPINE_DEV_API_PROXY_TARGET','LOG_LEVEL'
    )) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
}
