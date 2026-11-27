$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$reviewRoot = Join-Path $repoRoot '.verification-local-review-20261029-c11-department'
$manifestPath = Join-Path $reviewRoot 'stack-state.json'
$database = 'infraspine_c11dept_20261029_test'
$postgresPort = 55929
$apiPort = 63014
$frontendPort = 5186
$pgBin = 'C:\Program Files\PostgreSQL\18\bin'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$viteScript = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
$model = Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'

if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'The owned C11 review manifest is unavailable.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.database -ne $database -or $manifest.apiPort -ne $apiPort -or $manifest.frontendPort -ne $frontendPort) { throw 'The C11 stack manifest does not match this exact isolated review target.' }
$recordedApiPid = [int]$manifest.apiPid # recorded listening uvicorn PID
$recordedApiLauncherPid = if ($manifest.PSObject.Properties.Name -contains 'apiLauncherPid') { [int]$manifest.apiLauncherPid } else { $recordedApiPid }
$vitePid = [int]$manifest.vitePid
$apiProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $recordedApiLauncherPid" -ErrorAction Stop
$viteProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $vitePid" -ErrorAction Stop
if (!$apiProcess -or !$viteProcess -or $apiProcess.ExecutablePath -ne $python -or $apiProcess.CommandLine -notmatch 'app\.main:app' -or $apiProcess.CommandLine -notmatch "--port\s+$apiPort") { throw 'The recorded API PID no longer identifies only this owned C11 review launcher.' }
if ($viteProcess.ExecutablePath -ne $node -or $viteProcess.CommandLine -notmatch 'vite' -or $viteProcess.CommandLine -notmatch "--port\s+$frontendPort") { throw 'The recorded Vite PID no longer identifies only this owned C11 UI process.' }
$apiListener = Get-NetTCPConnection -State Listen -LocalPort $apiPort -ErrorAction Stop | Select-Object -First 1
if (!$apiListener) { throw 'The C11 API port has no listener.' }
$apiServerPid = [int]$apiListener.OwningProcess
$apiServerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $apiServerPid" -ErrorAction Stop
$listenerIsExactChild = $apiServerProcess -and
    $apiServerProcess.ParentProcessId -eq $recordedApiLauncherPid -and
    $apiServerProcess.CommandLine -match 'python(?:\.exe)?"?\s+-m\s+uvicorn\s+app\.main:app' -and
    $apiServerProcess.CommandLine -match "--port\s+$apiPort(?:\s|$)"
if ($apiServerPid -ne $recordedApiPid -or ($apiServerPid -ne $recordedApiLauncherPid -and !$listenerIsExactChild)) { throw 'The C11 listener is not the recorded API PID or its exact venv-launched uvicorn child; preserving both.' }
$viteListener = Get-NetTCPConnection -State Listen -LocalPort $frontendPort -ErrorAction Stop | Where-Object OwningProcess -eq $vitePid
if (!$viteListener) { throw 'The C11 UI port is not owned by the recorded isolated Vite process.' }
if ((Invoke-WebRequest -Uri "http://127.0.0.1:$apiPort/health/ready" -TimeoutSec 5 -UseBasicParsing).StatusCode -ne 200) { throw 'The owned C11 API was not healthy before the planned source reload.' }
if ((Invoke-WebRequest -Uri "http://127.0.0.1:$frontendPort/admin/" -TimeoutSec 5 -UseBasicParsing).StatusCode -ne 200) { throw 'The owned C11 UI was not healthy before the planned API source reload.' }
& (Join-Path $pgBin 'pg_isready.exe') -h 127.0.0.1 -p $postgresPort | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The isolated PostgreSQL cluster is unavailable.' }
$dbExists = & (Join-Path $pgBin 'psql.exe') -X -A -t -h 127.0.0.1 -p $postgresPort -U postgres -d postgres -c "SELECT count(*) FROM pg_database WHERE datname='$database'"
if ($LASTEXITCODE -ne 0 -or $dbExists.Trim() -ne '1') { throw 'The exact C11 synthetic database is unavailable.' }
$pending = & (Join-Path $pgBin 'psql.exe') -X -A -t -h 127.0.0.1 -p $postgresPort -U postgres -d $database -c "SELECT count(*) FROM documents WHERE status IN ('pending','processing')"
if ($LASTEXITCODE -ne 0 -or $pending.Trim() -ne '0') { throw 'An isolated C11 document job is pending; preserving the API process.' }
$agentRuns = & (Join-Path $pgBin 'psql.exe') -X -A -t -h 127.0.0.1 -p $postgresPort -U postgres -d $database -c 'SELECT count(*) FROM agent_profile_runs'
if ($LASTEXITCODE -ne 0 -or $agentRuns.Trim() -ne '0') { throw 'C11 agent run history exists and its synthetic encryption key must be preserved; keeping the API process.' }

# Stop only the verified socket-owning uvicorn process and its recorded venv launcher.
Stop-Process -Id $apiServerPid -Force
try { Wait-Process -Id $apiServerPid -Timeout 15 -ErrorAction Stop } catch { }
if (Get-Process -Id $apiServerPid -ErrorAction SilentlyContinue) { throw 'The C11 API server process did not stop cleanly; no replacement was started.' }
if ($recordedApiLauncherPid -ne $apiServerPid) {
    if (Get-Process -Id $recordedApiLauncherPid -ErrorAction SilentlyContinue) { Stop-Process -Id $recordedApiLauncherPid -Force }
    try { Wait-Process -Id $recordedApiLauncherPid -Timeout 10 -ErrorAction Stop } catch { }
    if (Get-Process -Id $recordedApiLauncherPid -ErrorAction SilentlyContinue) { throw 'The C11 API launcher did not stop cleanly; no replacement was started.' }
}

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
$env:AGENT_QUEUE_WORKER_ENABLED = 'true'
$env:RAG_INGESTION_MODE = 'worker'
$env:RAG_INDEX_SYNC_SECONDS = '0'
$env:RAG_DATABASE_ANALYSIS_URL = ''
$env:RAG_DATABASE_ANALYSIS_SQLITE_PATH = Join-Path $reviewRoot 'data\business\reporting.sqlite3'
$env:RAG_DATABASE_ANALYSIS_SQLITE_ROOT = Join-Path $reviewRoot 'data'
$env:RAG_DATABASE_ANALYSIS_CATALOG = '{"main.orders":{"order_id":"integer","department":"text","region":"text","amount":"numeric","customer":"text"}}'
$env:RAG_DATABASE_ANALYSIS_DEPARTMENT_GRANTS = '{"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa":[{"table":"main.orders","column":"department","value":"Finance"}]}'
$env:RAG_DATABASE_ANALYSIS_SOURCE_LABEL = 'Synthetic Finance reporting copy'
$env:LOG_DIR = Join-Path $reviewRoot 'logs\api-c13-current'
$env:TEMP = Join-Path $reviewRoot 'tmp'
$env:TMP = $env:TEMP
$env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$apiPort"

$api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$apiPort", '--no-access-log') -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $reviewRoot 'logs\api-c13-current.stdout.log') -RedirectStandardError (Join-Path $reviewRoot 'logs\api-c13-current.stderr.log')
$deadline = (Get-Date).AddSeconds(90); $apiReady = $false; $uiReady = $false
while ((Get-Date) -lt $deadline -and !($apiReady -and $uiReady)) {
    try { $apiReady = (Invoke-WebRequest -Uri "http://127.0.0.1:$apiPort/health/ready" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200 } catch { }
    try { $uiReady = (Invoke-WebRequest -Uri "http://127.0.0.1:$frontendPort/admin/" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200 } catch { }
    if (!($apiReady -and $uiReady)) { Start-Sleep -Milliseconds 400 }
}
if (!($apiReady -and $uiReady)) { throw 'The current-source C11 API/UI pair did not become healthy; keep the isolated DB and Vite process intact and inspect scoped logs.' }
$newListener = Get-NetTCPConnection -State Listen -LocalPort $apiPort -ErrorAction Stop | Select-Object -First 1
$newServer = Get-CimInstance Win32_Process -Filter "ProcessId = $($newListener.OwningProcess)" -ErrorAction Stop
if ($newServer.CommandLine -notmatch 'app\.main:app' -or $newServer.CommandLine -notmatch "--port\s+$apiPort") { throw 'The new API port listener did not resolve to the current-source uvicorn process.' }
$manifest.apiPid = [int]$newListener.OwningProcess
$manifest | Add-Member -NotePropertyName apiLauncherPid -NotePropertyValue ([int]$api.Id) -Force
$manifest.migration = '0064_durable_agent_run_queue'
$manifest.sourceSha256.'app/api/routes/rag.py' = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $repoRoot 'app\api\routes\rag.py')).Hash.ToLowerInvariant()
$sourceFiles = @('app/api/routes/agents.py','app/services/agent_worker.py','app/services/agents.py','app/db/models.py','app/core/config.py','app/main.py','app/cli.py','migrations/versions/0064_durable_agent_run_queue.py','frontend/src/components/AgentCatalogPage.tsx','frontend/src/api.ts','frontend/src/types.ts')
foreach ($sourceFile in $sourceFiles) {
    $manifest.sourceSha256 | Add-Member -NotePropertyName $sourceFile -NotePropertyValue (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $repoRoot ($sourceFile -replace '/', '\'))).Hash.ToLowerInvariant() -Force
}
$manifest | Add-Member -NotePropertyName apiRestartedAt -NotePropertyValue (Get-Date).ToString('o') -Force
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding utf8
Write-Output 'C11_API_CURRENT_SOURCE_RELOADED=READY; only the owned synthetic API PID was replaced; Vite, DB and other review stacks remain available.'
