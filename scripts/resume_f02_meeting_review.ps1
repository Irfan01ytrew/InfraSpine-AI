$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$root = (Resolve-Path (Join-Path $repo '.verification-local-review-20261028-f02-meetings')).Path
$fixture = (Resolve-Path (Join-Path $repo '.verification-local-review-20261028-f01-workspaces\review-credentials.json')).Path
$database = 'infraspine_f02meetai_20261028_test'
$url = "postgresql+asyncpg://postgres@127.0.0.1:55928/$database"
$python = Join-Path $repo '.venv-verify-312\Scripts\python.exe'
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'

if (Test-Path -LiteralPath (Join-Path $root 'stack-state.json')) { throw 'F02 stack state already exists; preserving the running or recorded stack.' }
$entries = @(Get-ChildItem -LiteralPath $root -Force | Select-Object -ExpandProperty Name)
if (@($entries | Where-Object { $_ -notin @('data','logs','evidence') }).Count -gt 0) { throw 'The prepared F02 review folder contains an unexpected item; inspect it before continuing.' }
foreach ($port in @(63007,5178)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "F02 review port $port is occupied; no existing process was changed." }
}
$migration = (& $psql -h 127.0.0.1 -p 55928 -U postgres -d $database -tAc 'SELECT version_num FROM alembic_version') -join ''
$users = (& $psql -h 127.0.0.1 -p 55928 -U postgres -d $database -tAc 'SELECT count(*) FROM users') -join ''
$documents = (& $psql -h 127.0.0.1 -p 55928 -U postgres -d $database -tAc 'SELECT count(*) FROM documents') -join ''
if ($LASTEXITCODE -ne 0 -or $migration.Trim() -ne '0059_workspace_hierarchy' -or $users.Trim() -ne '0' -or $documents.Trim() -ne '0') {
    throw 'Prepared F02 DB is not the empty migrated database from the interrupted setup; preserving it.'
}

$env:DATABASE_URL = $url
$env:INFRASPINE_F02_CREDENTIAL_FIXTURE = $fixture
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$keyBytes = New-Object byte[] 32
try { $rng.GetBytes($keyBytes); $env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String($keyBytes) }
finally { [Array]::Clear($keyBytes, 0, $keyBytes.Length); $rng.Dispose() }
$env:MASTER_ENCRYPTION_KEY_ID = 'f02-review-process-key'
$env:APP_ENV = 'testing'
$env:LLM_MOCK_MODE = 'true'
$env:SESSION_COOKIE_SECURE = 'false'
$env:DATABASE_POOL_SIZE = '1'
$env:DATABASE_POOL_MAX_OVERFLOW = '0'
$env:LOG_LEVEL = 'WARNING'
$env:LOG_DIR = Join-Path $root 'logs'
$env:RAG_DATA_DIR = Join-Path $root 'data'
$env:RAG_MODELS_DIR = Join-Path $repo 'data\models'
$env:RAG_EMBEDDING_MODEL = Join-Path $repo 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
$env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
$env:RAG_VECTOR_STORE = 'faiss'
$env:RAG_INGESTION_MODE = 'worker'
$env:RAG_WORKER_ENABLED = 'false'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:INFRASPINE_DEV_API_PROXY_TARGET = 'http://127.0.0.1:63007'
Push-Location $repo
try {
    & $python scripts\seed_f02_meeting_review.py
    if ($LASTEXITCODE -ne 0) { throw 'F02 seed failed; the test database is retained for diagnosis.' }
    $api = Start-Process -FilePath $python -ArgumentList @('scripts\f02_stubbed_review_api.py','--port','63007') -WorkingDirectory $repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'logs\api.stdout.log') -RedirectStandardError (Join-Path $root 'logs\api.stderr.log')
    $viteEntry = Join-Path $repo 'frontend\node_modules\vite\bin\vite.js'
    $node = (Get-Command node.exe -ErrorAction Stop).Source
    $vite = Start-Process -FilePath $node -ArgumentList @(('"' + $viteEntry + '"'),'--host','127.0.0.1','--strictPort','--port','5178') -WorkingDirectory (Join-Path $repo 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'logs\vite.stdout.log') -RedirectStandardError (Join-Path $root 'logs\vite.stderr.log')
    $healthy = $false
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -TimeoutSec 2 'http://127.0.0.1:63007/health/ready'
            $ui = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:5178/admin/'
            if ($health.status -eq 'ready' -and $ui.StatusCode -eq 200) { $healthy = $true; break }
        } catch { Start-Sleep -Milliseconds 400 }
    }
    if (-not $healthy) { throw 'F02 API/UI readiness failed; process logs and isolated data are retained.' }
    $state = [ordered]@{
        reviewUrl = 'http://127.0.0.1:5178/admin/'
        apiUrl = 'http://127.0.0.1:63007'
        database = $database
        migration = $migration.Trim()
        apiPid = $api.Id
        vitePid = $vite.Id
        startedAt = (Get-Date).ToString('o')
        llm = 'deterministic transcript-copying test double; no external provider'
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'stack-state.json') -Encoding utf8
    'F02_CURRENT_SOURCE_REVIEW_STACK=HEALTHY'
    'F02_REVIEW_URL=http://127.0.0.1:5178/admin/'
    'F02_API_HEALTH=http://127.0.0.1:63007/health/ready'
} finally {
    Pop-Location
    foreach ($name in @('DATABASE_URL','INFRASPINE_F02_CREDENTIAL_FIXTURE','MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID','APP_ENV','LLM_MOCK_MODE','SESSION_COOKIE_SECURE','DATABASE_POOL_SIZE','DATABASE_POOL_MAX_OVERFLOW','LOG_LEVEL','LOG_DIR','RAG_DATA_DIR','RAG_MODELS_DIR','RAG_EMBEDDING_MODEL','RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_VECTOR_STORE','RAG_INGESTION_MODE','RAG_WORKER_ENABLED','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','INFRASPINE_DEV_API_PROXY_TARGET')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
