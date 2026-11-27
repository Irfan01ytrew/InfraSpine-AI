$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$reviewRoot = Join-Path $repoRoot '.verification-local-review-20261029-c11-department'
$database = 'infraspine_c11dept_20261029_test'
$postgresPort = 55929
$apiPort = 63014
$frontendPort = 5186
$pgBin = 'C:\Program Files\PostgreSQL\18\bin'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$viteScript = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
$model = Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'

if (!(Test-Path -LiteralPath (Join-Path $reviewRoot 'credentials.json') -PathType Leaf) -or !(Test-Path -LiteralPath (Join-Path $reviewRoot 'data\business\reporting.sqlite3') -PathType Leaf)) { throw 'The protected C11 fixture or synthetic reporting source is unavailable.' }
if (Test-Path -LiteralPath (Join-Path $reviewRoot 'stack-state.json')) { throw 'A C11 stack manifest already exists; preserving the recorded stack.' }
if (!(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $viteScript -PathType Leaf) -or !(Test-Path -LiteralPath (Join-Path $model 'modules.json') -PathType Leaf)) { throw 'A required local C11 runtime is unavailable.' }
foreach ($port in @($apiPort, $frontendPort)) { if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "C11 port $port is occupied; no process was changed." } }
& (Join-Path $pgBin 'pg_isready.exe') -h 127.0.0.1 -p $postgresPort | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The isolated PostgreSQL verification cluster is unavailable.' }
$databaseExists = & (Join-Path $pgBin 'psql.exe') -X -A -t -h 127.0.0.1 -p $postgresPort -U postgres -d postgres -c "SELECT count(*) FROM pg_database WHERE datname='$database'"
if ($LASTEXITCODE -ne 0 -or $databaseExists.Trim() -ne '1') { throw 'The exact isolated C11 database was not found; no other database will be used.' }

$env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$postgresPort/$database"
$env:APP_ENV = 'testing'
$keyGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
$keyBytes = New-Object byte[] 32
$keyGenerator.GetBytes($keyBytes)
$env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String($keyBytes)
[Array]::Clear($keyBytes, 0, $keyBytes.Length)
$keyGenerator.Dispose()
$env:MASTER_ENCRYPTION_KEY_ID = 'c11-dept-current'
$env:SESSION_COOKIE_SECURE = 'false'
$env:LLM_MOCK_MODE = 'true'
$env:PYTHONPATH = $repoRoot
$env:RAG_DATA_DIR = Join-Path $reviewRoot 'data'
$env:RAG_MODELS_DIR = Join-Path $repoRoot 'data\models'
$env:RAG_EMBEDDING_MODEL = $model
$env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:RAG_VECTOR_STORE = 'faiss'
$env:RAG_WORKER_ENABLED = 'false'
$env:RAG_INGESTION_MODE = 'worker'
$env:RAG_INDEX_SYNC_SECONDS = '0'
$env:RAG_DATABASE_ANALYSIS_URL = ''
$env:RAG_DATABASE_ANALYSIS_SQLITE_PATH = Join-Path $reviewRoot 'data\business\reporting.sqlite3'
$env:RAG_DATABASE_ANALYSIS_SQLITE_ROOT = Join-Path $reviewRoot 'data'
$env:RAG_DATABASE_ANALYSIS_CATALOG = '{"main.orders":{"order_id":"integer","department":"text","region":"text","amount":"numeric","customer":"text"}}'
$env:RAG_DATABASE_ANALYSIS_DEPARTMENT_GRANTS = '{"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa":[{"table":"main.orders","column":"department","value":"Finance"}]}'
$env:RAG_DATABASE_ANALYSIS_SOURCE_LABEL = 'Synthetic Finance reporting copy'
$env:LOG_DIR = Join-Path $reviewRoot 'logs\api'
$env:TEMP = Join-Path $reviewRoot 'tmp'
$env:TMP = $env:TEMP
$env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$apiPort"

$api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$apiPort", '--no-access-log') -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $reviewRoot 'logs\api.stdout.log') -RedirectStandardError (Join-Path $reviewRoot 'logs\api.stderr.log')
$vite = Start-Process -FilePath $node -ArgumentList @(('"' + $viteScript + '"'), '--host', '127.0.0.1', '--strictPort', '--port', "$frontendPort") -WorkingDirectory (Join-Path $repoRoot 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $reviewRoot 'logs\vite.stdout.log') -RedirectStandardError (Join-Path $reviewRoot 'logs\vite.stderr.log')

$deadline = (Get-Date).AddSeconds(90)
$apiReady = $false; $uiReady = $false
while ((Get-Date) -lt $deadline -and !($apiReady -and $uiReady)) {
    try { $apiReady = (Invoke-WebRequest -Uri "http://127.0.0.1:$apiPort/health/ready" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200 } catch { }
    try { $uiReady = (Invoke-WebRequest -Uri "http://127.0.0.1:$frontendPort/admin/" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200 } catch { }
    if (!($apiReady -and $uiReady)) { Start-Sleep -Milliseconds 400 }
}
if (!($apiReady -and $uiReady)) { throw 'Fresh C11 API/UI did not become healthy; keep processes intact and inspect the scoped logs.' }

$sourceFiles = @('app\api\routes\rag.py','app\services\database_analysis.py','frontend\src\components\DatabaseAnalysisPanel.tsx','frontend\src\components\KnowledgePage.tsx','scripts\browser_c11_department_analysis_acceptance.mjs')
$hashes = [ordered]@{}
foreach ($file in $sourceFiles) { $hashes[$file.Replace('\','/')] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $repoRoot $file)).Hash.ToLowerInvariant() }
[ordered]@{ apiPid=$api.Id; vitePid=$vite.Id; apiPort=$apiPort; frontendPort=$frontendPort; database=$database; postgresPort=$postgresPort; migration='0063_cloud_source_acl_intersection'; startedAt=(Get-Date).ToString('o'); sourceSha256=$hashes } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $reviewRoot 'stack-state.json') -Encoding utf8
Write-Output 'C11_CURRENT_SOURCE_FRESH_STACK=READY; API and UI returned HTTP 200.'
