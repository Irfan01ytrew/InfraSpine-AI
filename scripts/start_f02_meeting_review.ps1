[CmdletBinding()]
param(
    [string]$ReviewRoot = '.verification-local-review-20261028-f02-meetings',
    [string]$CredentialFixturePath = '.verification-local-review-20261028-f01-workspaces/review-credentials.json',
    [int]$PostgresPort = 55928,
    [int]$ApiPort = 63007,
    [int]$FrontendPort = 5178
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not [IO.Path]::IsPathRooted($ReviewRoot)) { $ReviewRoot = Join-Path $repo $ReviewRoot }
if (-not [IO.Path]::IsPathRooted($CredentialFixturePath)) { $CredentialFixturePath = Join-Path $repo $CredentialFixturePath }
$ReviewRoot = [IO.Path]::GetFullPath($ReviewRoot)
$CredentialFixturePath = (Resolve-Path -LiteralPath $CredentialFixturePath -ErrorAction Stop).Path
$cluster = Join-Path $repo '.verification-local-review-20261027-native\f08-workerprocess-pg-isolated-20261027-r1\cluster'
$database = 'infraspine_f02meetai_20261028_test'
$python = Join-Path $repo '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$viteEntry = Join-Path $repo 'frontend\node_modules\vite\bin\vite.js'
$createdb = 'C:\Program Files\PostgreSQL\18\bin\createdb.exe'
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$url = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$database"

if (Test-Path -LiteralPath $ReviewRoot) { throw 'F02 review root already exists; inspect and preserve it before retrying.' }
foreach ($required in @($python, $node, $viteEntry, $createdb, $psql)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw 'A required local review runtime component is unavailable.' }
}
if (-not (Test-Path -LiteralPath $cluster -PathType Container)) { throw 'The retained isolated PostgreSQL cluster directory is unavailable.' }
& 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe' -D $cluster status *> $null
if ($LASTEXITCODE -ne 0) { throw 'The retained loopback PostgreSQL cluster is not running; this launcher will not start or modify it.' }
$ports = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in @($ApiPort,$FrontendPort) -and $_.LocalAddress -in @('127.0.0.1','0.0.0.0','::1','::') })
if ($ports.Count) { throw 'A requested F02 review port is occupied; no process was changed.' }
$dbExists = & $psql -h 127.0.0.1 -p $PostgresPort -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$database'"
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the isolated PostgreSQL catalog.' }
if (($dbExists -join '').Trim() -eq '1') { throw 'The F02 database already exists; preserving it without overwrite.' }

New-Item -ItemType Directory -Path $ReviewRoot | Out-Null
$ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $ReviewRoot /inheritance:r /grant:r ("*{0}:(OI)(CI)F" -f $ownerSid) '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the F02 synthetic review folder ACL.' }
New-Item -ItemType Directory -Path (Join-Path $ReviewRoot 'data'), (Join-Path $ReviewRoot 'logs'), (Join-Path $ReviewRoot 'evidence') | Out-Null

& $createdb -h 127.0.0.1 -p $PostgresPort -U postgres $database
if ($LASTEXITCODE -ne 0) { throw 'Could not create the new disposable F02 database.' }
$env:DATABASE_URL = $url
$env:INFRASPINE_F02_CREDENTIAL_FIXTURE = $CredentialFixturePath
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$keyBytes = New-Object byte[] 32
try {
    $rng.GetBytes($keyBytes)
    $env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String($keyBytes)
} finally { [Array]::Clear($keyBytes,0,$keyBytes.Length); $rng.Dispose() }
$env:MASTER_ENCRYPTION_KEY_ID = 'f02-review-process-key'
$env:APP_ENV = 'testing'
$env:LLM_MOCK_MODE = 'true'
$env:SESSION_COOKIE_SECURE = 'false'
$env:DATABASE_POOL_SIZE = '1'
$env:DATABASE_POOL_MAX_OVERFLOW = '0'
$env:LOG_LEVEL = 'WARNING'
$env:LOG_DIR = Join-Path $ReviewRoot 'logs'
$env:RAG_DATA_DIR = Join-Path $ReviewRoot 'data'
$env:RAG_MODELS_DIR = Join-Path $repo 'data\models'
$env:RAG_EMBEDDING_MODEL = Join-Path $repo 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
$env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
$env:RAG_VECTOR_STORE = 'faiss'
$env:RAG_INGESTION_MODE = 'worker'
$env:RAG_WORKER_ENABLED = 'false'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:INFRASPINE_F02_REVIEW_ROOT = $ReviewRoot
$env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"

Push-Location $repo
try {
    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Current-source migration failed; the disposable F02 DB is retained for diagnosis.' }
    & $python (Join-Path $PSScriptRoot 'seed_f02_meeting_review.py')
    if ($LASTEXITCODE -ne 0) { throw 'F02 synthetic seed failed; the disposable DB is retained for diagnosis.' }
    $api = Start-Process -FilePath $python -ArgumentList @('scripts\f02_stubbed_review_api.py','--port',"$ApiPort") -WorkingDirectory $repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $ReviewRoot 'logs\api.stdout.log') -RedirectStandardError (Join-Path $ReviewRoot 'logs\api.stderr.log')
    $vite = Start-Process -FilePath $node -ArgumentList @(('"' + $viteEntry + '"'),'--host','127.0.0.1','--strictPort','--port',"$FrontendPort") -WorkingDirectory (Join-Path $repo 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $ReviewRoot 'logs\vite.stdout.log') -RedirectStandardError (Join-Path $ReviewRoot 'logs\vite.stderr.log')
    $deadline = (Get-Date).AddSeconds(60)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $apiHealth = Invoke-RestMethod -TimeoutSec 2 "http://127.0.0.1:$ApiPort/health/ready"
            $uiHealth = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$FrontendPort/admin/"
            if ($apiHealth.status -eq 'ready' -and $uiHealth.StatusCode -eq 200) { $healthy = $true; break }
        } catch { Start-Sleep -Milliseconds 400 }
    }
    if (-not $healthy) { throw 'F02 API/UI did not become healthy; isolated data and processes are retained for inspection.' }
    $state = [ordered]@{ reviewUrl="http://127.0.0.1:$FrontendPort/admin/"; apiUrl="http://127.0.0.1:$ApiPort"; database=$database; migration='0059_workspace_hierarchy'; apiPid=$api.Id; vitePid=$vite.Id; startedAt=(Get-Date).ToString('o'); llm='deterministic transcript-copying test double; no external provider' }
    $state | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ReviewRoot 'stack-state.json') -Encoding utf8
    Write-Output 'F02_CURRENT_SOURCE_REVIEW_STACK=HEALTHY'
    Write-Output "F02_REVIEW_URL=http://127.0.0.1:$FrontendPort/admin/"
    Write-Output "F02_API_HEALTH=http://127.0.0.1:$ApiPort/health/ready"
} finally {
    Pop-Location
    foreach ($name in @('DATABASE_URL','INFRASPINE_F02_CREDENTIAL_FIXTURE','MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID','APP_ENV','LLM_MOCK_MODE','SESSION_COOKIE_SECURE','DATABASE_POOL_SIZE','DATABASE_POOL_MAX_OVERFLOW','LOG_LEVEL','LOG_DIR','RAG_DATA_DIR','RAG_MODELS_DIR','RAG_EMBEDDING_MODEL','RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_VECTOR_STORE','RAG_INGESTION_MODE','RAG_WORKER_ENABLED','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','INFRASPINE_F02_REVIEW_ROOT','INFRASPINE_DEV_API_PROXY_TARGET')) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
}
