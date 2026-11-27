$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$reviewRoot = if ($env:INFRASPINE_LOCAL_REVIEW_RUNTIME_ROOT) {
    [IO.Path]::GetFullPath($env:INFRASPINE_LOCAL_REVIEW_RUNTIME_ROOT)
} else {
    Join-Path $repoRoot '.verification-local-review-20261026'
}
$env:RAG_WORKER_ENABLED = 'true'
$env:LOG_DIR = if ($env:INFRASPINE_LOCAL_REVIEW_WORKER_LOG_DIR) {
    [IO.Path]::GetFullPath($env:INFRASPINE_LOCAL_REVIEW_WORKER_LOG_DIR)
} else {
    Join-Path $reviewRoot 'logs\worker'
}
New-Item -ItemType Directory -Path $env:LOG_DIR -Force | Out-Null
Set-Location $repoRoot
& (Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe') -m app.worker
exit $LASTEXITCODE
