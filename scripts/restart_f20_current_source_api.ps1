$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = Join-Path $repoRoot '.verification-local-review-20261027-native\f18-current-source\f20-current-source'
$statePath = Join-Path $runtimeRoot 'runtime-state.json'
$database = 'infraspine_f18voice_20261027_a91e_test'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if ($state.apiPort -ne 63010 -or $state.frontendPort -ne 5181 -or $state.database -ne $database -or
    $state.databasePort -ne 55888) { throw 'Runtime identity differs from the F20 synthetic stack; refusing to restart.' }
if (!(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $psql -PathType Leaf)) {
    throw 'F20 runtime prerequisites are missing.'
}
$allListeners = @(Get-NetTCPConnection -State Listen -LocalPort 63010 -ErrorAction SilentlyContinue)
$listeners = @($allListeners | Where-Object { $_.LocalAddress -eq '127.0.0.1' })
if ($listeners.Count -gt 1 -or ($listeners.Count -eq 0 -and $allListeners.Count -gt 0)) {
    throw 'Port 63010 has an unexpected listener; refusing to change any process.'
}
$apiProcess = $null
if ($listeners.Count -eq 1) {
    $apiProcess = Get-Process -Id $listeners[0].OwningProcess -ErrorAction Stop
    $pythonConfig = Get-Content -LiteralPath (Join-Path (Split-Path -Parent $python) '..\pyvenv.cfg')
    $pythonHomeLine = $pythonConfig | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
    $pythonHome = if ($pythonHomeLine) { ($pythonHomeLine -split '=',2)[1].Trim() } else { '' }
    $expectedProcessPath = if ($pythonHome) { Join-Path $pythonHome 'python.exe' } else { '' }
    $processPath = $apiProcess.Path
    if (-not $processPath -or -not $expectedProcessPath -or
        [IO.Path]::GetFullPath($processPath) -ne [IO.Path]::GetFullPath($expectedProcessPath)) {
        throw 'The port listener is not the project Python executable; refusing to stop it.'
    }
    $runtimeStarted = [DateTimeOffset]::Parse($state.startedAt).LocalDateTime
    if ($apiProcess.StartTime -lt $runtimeStarted.AddMinutes(-1)) {
        throw 'The port listener predates this F20 runtime; refusing to stop it.'
    }
    # Only the loopback listener matching this F20 runtime and its isolated Python executable is stopped.
    Stop-Process -Id $apiProcess.Id -Force
    for ($i = 0; $i -lt 20 -and (Get-Process -Id $apiProcess.Id -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 200 }
} else {
    Write-Output 'F20 API was already stopped; starting only the recorded current-source service.'
}

$env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:55888/$database"
$env:APP_ENV = 'testing'
$env:DATABASE_POOL_SIZE = '1'
$env:DATABASE_POOL_MAX_OVERFLOW = '0'
$env:LLM_MOCK_MODE = 'true'
$env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$env:MASTER_ENCRYPTION_KEY_ID = 'f20-browser-review'
$env:SESSION_COOKIE_SECURE = 'false'
$env:RAG_DATA_DIR = Join-Path $runtimeRoot 'data'
$env:RAG_MODELS_DIR = Join-Path $repoRoot 'data\models'
$env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
$env:RAG_EMBEDDING_MODEL = Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
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
$env:LOG_LEVEL = 'INFO'
$log = Join-Path $runtimeRoot ('logs\api-pool-limited-' + (Get-Date -Format 'yyyyMMddTHHmmssfff'))
New-Item -ItemType Directory -Path $log -Force | Out-Null
try {
    $revision = & $psql -X -h 127.0.0.1 -p 55888 -U postgres -d $database -v ON_ERROR_STOP=1 -Atc 'SELECT version_num FROM alembic_version'
    if ($LASTEXITCODE -ne 0 -or ($revision -join '').Trim() -ne '0056_legacy_browser_approval') {
        throw 'F20 synthetic DB is not at expected migration head; API was not restarted.'
    }
    $api = Start-Process -FilePath $python -ArgumentList @(
        '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','63010','--no-access-log'
    ) -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $log 'api.stdout.log') `
        -RedirectStandardError (Join-Path $log 'api.stderr.log')
    $healthy = $false
    for ($i = 0; $i -lt 40; $i++) {
        if ($api.HasExited) { break }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:63010/health/ready' -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $healthy = $true; break }
        } catch { Start-Sleep -Milliseconds 750 }
    }
    if (-not $healthy) { throw 'F20 API did not become healthy with the bounded DB pool.' }
    $newListener = @(Get-NetTCPConnection -State Listen -LocalPort 63010 -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq '127.0.0.1' })
    if ($newListener.Count -ne 1) { throw 'Could not identify the restarted F20 API listener.' }
    $state.apiPid = $newListener[0].OwningProcess
    $state | Add-Member -NotePropertyName apiLauncherPid -NotePropertyValue $api.Id -Force
    $state | Add-Member -NotePropertyName apiPoolSize -NotePropertyValue 1 -Force
    $state | Add-Member -NotePropertyName apiPoolMaxOverflow -NotePropertyValue 0 -Force
    $state | Add-Member -NotePropertyName apiRestartedAt -NotePropertyValue (Get-Date).ToString('o') -Force
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
    Write-Output 'F20 API restarted with database pool size 1 and zero overflow; Vite 5181 remains running.'
} catch {
    if ($null -ne $api -and -not $api.HasExited) { Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue }
    throw
} finally {
    foreach ($name in @(
        'DATABASE_URL','APP_ENV','DATABASE_POOL_SIZE','DATABASE_POOL_MAX_OVERFLOW','LLM_MOCK_MODE',
        'MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID','SESSION_COOKIE_SECURE','RAG_DATA_DIR','RAG_MODELS_DIR',
        'RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_EMBEDDING_MODEL','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE',
        'RAG_VECTOR_STORE','RAG_INGESTION_MODE','RAG_WORKER_ENABLED','RAG_INDEX_SYNC_SECONDS',
        'RAG_LEGACY_BROWSER_ENABLED','RAG_LEGACY_BROWSER_ALLOWED_ORIGINS','RAG_LEGACY_BROWSER_EXECUTABLE_PATH',
        'RAG_LEGACY_BROWSER_TIMEOUT_SECONDS','RAG_LEGACY_BROWSER_MAX_REQUESTS','RAG_LEGACY_BROWSER_MAX_RESPONSE_BYTES',
        'RAG_LEGACY_BROWSER_MAX_RESULT_CHARS','RAG_LEGACY_BROWSER_ALLOW_PRIVATE_NETWORKS','LOG_LEVEL'
    )) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
}
