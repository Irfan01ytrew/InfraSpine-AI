param(
    [int]$ApiPort = 63015,
    [int]$FrontendPort = 5187,
    [string]$ReviewRoot = '.verification-local-review-20261029-c15-mywork-r3'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not [IO.Path]::IsPathRooted($ReviewRoot)) { $ReviewRoot = Join-Path $repo $ReviewRoot }
$root = (Resolve-Path -LiteralPath $ReviewRoot).Path
if (-not $root.StartsWith($repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'C15 review root is outside the repository.' }
foreach ($port in @($ApiPort, $FrontendPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "C15 port $port is already occupied." }
}
$runtimePath = Join-Path $root 'runtime-state.json'
$fixturePath = Join-Path $root 'credentials.json'
if (-not (Test-Path -LiteralPath $runtimePath) -or -not (Test-Path -LiteralPath $fixturePath)) { throw 'The retained C15 runtime or fixture is missing.' }
$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
if ($runtime.postgres_port -ne 55929 -or $runtime.database -notmatch '^infraspine_c15mywork_[a-f0-9]{8}_review$') { throw 'C15 runtime metadata is not pinned to its isolated database.' }
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$revision = & $psql -w -h 127.0.0.1 -p 55929 -U postgres -d $runtime.database -At -c 'SELECT version_num FROM alembic_version'
if ($LASTEXITCODE -ne 0 -or $revision.Trim() -ne '0065_workflow_personal_task_action') { throw 'The retained C15 review database is not at migration 0065.' }
$python = Join-Path $repo '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$viteEntry = Join-Path $repo 'frontend\node_modules\vite\bin\vite.js'
$envNames = @('DATABASE_URL','APP_ENV','LLM_MOCK_MODE','LICENSE_ENFORCEMENT_ENABLED','MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID','SESSION_COOKIE_SECURE','RAG_DATA_DIR','RAG_MODELS_DIR','RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_INGESTION_MODE','RAG_WORKER_ENABLED','AGENT_QUEUE_WORKER_ENABLED','RAG_INDEX_SYNC_SECONDS','RAG_USE_DOCLING','RAG_PARSE_ISOLATION','LOG_LEVEL','LOG_DIR','PYTHONPATH','TEMP','TMP','INFRASPINE_DEV_API_PROXY_TARGET')
$previous = @{}
foreach ($name in $envNames) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$started = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$ready = $false
try {
    $env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:55929/$($runtime.database)"
    $env:APP_ENV = 'testing'; $env:LLM_MOCK_MODE = 'true'; $env:LICENSE_ENFORCEMENT_ENABLED = 'false'
    $env:MASTER_ENCRYPTION_KEY = $runtime.master_key; $env:MASTER_ENCRYPTION_KEY_ID = 'c15qa'; $env:SESSION_COOKIE_SECURE = 'false'
    $env:RAG_DATA_DIR = Join-Path $root 'data'; $env:RAG_MODELS_DIR = Join-Path $repo 'data\models'; $env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
    $env:RAG_INGESTION_MODE = 'worker'; $env:RAG_WORKER_ENABLED = 'false'; $env:AGENT_QUEUE_WORKER_ENABLED = 'false'
    $env:RAG_INDEX_SYNC_SECONDS = '0'; $env:RAG_USE_DOCLING = 'false'; $env:RAG_PARSE_ISOLATION = 'false'; $env:LOG_LEVEL = 'INFO'
    $env:LOG_DIR = Join-Path $root 'logs\api'; $env:PYTHONPATH = $repo; $env:TEMP = Join-Path $root 'tmp'; $env:TMP = $env:TEMP
    $api = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$ApiPort",'--no-access-log') `
        -WorkingDirectory (Join-Path $root 'api-cwd') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root 'logs\api\stdout.log') -RedirectStandardError (Join-Path $root 'logs\api\stderr.log')
    $started.Add($api)
    $env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"
    $vite = Start-Process -FilePath $node -ArgumentList @(('"' + $viteEntry + '"'),'--host','127.0.0.1','--strictPort','--port',"$FrontendPort") `
        -WorkingDirectory (Join-Path $repo 'frontend') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root 'logs\vite\stdout.log') -RedirectStandardError (Join-Path $root 'logs\vite\stderr.log')
    $started.Add($vite)
    $apiReady = $false; $uiReady = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while (([DateTime]::UtcNow -lt $deadline) -and ($apiReady -eq $false -or $uiReady -eq $false)) {
        foreach ($process in $started) { if ($process.HasExited) { throw 'A new C15 API or Vite process exited during startup.' } }
        if (-not $apiReady) { try { $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 4; $apiReady = ($response.StatusCode -eq 200) } catch { } }
        if (-not $uiReady) { try { $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 4; $uiReady = ($response.StatusCode -eq 200) } catch { } }
        if (-not $apiReady -or -not $uiReady) { Start-Sleep -Milliseconds 350 }
    }
    if (-not $apiReady -or -not $uiReady) { throw 'The fresh C15 API or UI did not become healthy within 90 seconds.' }
    $apiPid = (Get-NetTCPConnection -State Listen -LocalPort $ApiPort -ErrorAction Stop | Select-Object -First 1).OwningProcess
    $state = [ordered]@{
        api_pid = [int]$apiPid; api_launcher_pid = $api.Id; vite_pid = $vite.Id; database = [string]$runtime.database
        postgres_port = 55929; migration = [string]$revision.Trim()
        frontend_url = "http://127.0.0.1:$FrontendPort/admin/#/automation"
        my_work_url = "http://127.0.0.1:$FrontendPort/admin/#/my-work"
        api_health_url = "http://127.0.0.1:$ApiPort/health/ready"
        credential_fixture = $fixturePath; runtime_state = $runtimePath; source_hashes = [ordered]@{}
    }
    foreach ($relative in @('migrations/versions/0065_workflow_personal_task_action.py','app/services/automation_worker.py','app/api/routes/automation.py','frontend/src/components/AutomationPage.tsx','frontend/src/components/MyWorkPage.tsx')) {
        $state.source_hashes[$relative] = (Get-FileHash -LiteralPath (Join-Path $repo $relative) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $statePath = Join-Path $root 'stack-state.json'
    $state | ConvertTo-Json -Depth 5 -Compress | Set-Content -LiteralPath $statePath -Encoding utf8
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    icacls.exe $statePath /inheritance:r /grant:r "*$($owner):F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not protect the C15 stack metadata.' }
    $ready = $true
    Write-Output 'C15_REVIEW_STACK=READY'
    Write-Output "FRONTEND_URL=$($state.frontend_url)"
    Write-Output "MY_WORK_URL=$($state.my_work_url)"
    Write-Output "API_HEALTH_URL=$($state.api_health_url)"
    Write-Output "SYNTHETIC_CREDENTIAL_VIEWER=scripts/show_local_review_credentials.ps1 -FixturePath $fixturePath"
} finally {
    foreach ($name in $envNames) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    if (-not $ready) { foreach ($process in $started) { if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } } }
}
