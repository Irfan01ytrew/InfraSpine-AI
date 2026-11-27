param(
    [switch]$Stop,
    [ValidateRange(1024, 65535)]
    [int]$FrontendPort = 5173,
    [ValidateRange(1024, 65535)]
    [int]$ApiPort = 8000
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$viteEntry = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
$stateRoot = Join-Path ([IO.Path]::GetTempPath()) 'infraspine-module-live-preview'
$statePath = Join-Path $stateRoot "vite-$FrontendPort.json"
$logPath = Join-Path $stateRoot "vite-$FrontendPort.log"

if ($Stop) {
    if (!(Test-Path -LiteralPath $statePath -PathType Leaf)) {
        Write-Output 'No managed frontend preview is running.'
        exit 0
    }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $process = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq $state.node -and $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $state.started) {
        Stop-Process -Id $process.Id
    }
    Remove-Item -LiteralPath $statePath -Force
    Write-Output 'Stopped the managed frontend preview.'
    exit 0
}

if (!(Test-Path -LiteralPath $viteEntry -PathType Leaf)) {
    throw 'Vite is not installed in frontend/node_modules.'
}

$api = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 5
if ($api.status -ne 'ready') {
    throw 'The local API readiness check did not return ready.'
}

$existingListener = Get-NetTCPConnection -State Listen -LocalPort $FrontendPort -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::1', '::') } |
    Select-Object -First 1
if ($existingListener) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 3 -UseBasicParsing
        if ($response.StatusCode -eq 200) {
            Write-Output "Frontend is already available at http://127.0.0.1:$FrontendPort/admin/"
            exit 0
        }
    } catch {
        throw "Port $FrontendPort is occupied by another service. No process was changed."
    }
    throw "Port $FrontendPort is occupied by another service. No process was changed."
}

New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
$node = (Get-Command node.exe -ErrorAction Stop).Source
$oldProxy = $env:INFRASPINE_DEV_API_PROXY_TARGET
try {
    $env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$ApiPort"
    $vite = Start-Process -FilePath $node `
        -ArgumentList @(('"' + $viteEntry + '"'), '--host', '127.0.0.1', '--port', "$FrontendPort", '--strictPort') `
        -WorkingDirectory (Join-Path $repoRoot 'frontend') `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $logPath -RedirectStandardError "$logPath.err"
} finally {
    $env:INFRASPINE_DEV_API_PROXY_TARGET = $oldProxy
}

Set-Content -LiteralPath $statePath -Value (@{ pid = $vite.Id; node = $node; started = $vite.StartTime.ToUniversalTime().Ticks.ToString() } | ConvertTo-Json) -Encoding utf8
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    if ($vite.HasExited) {
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
        throw "Vite exited before becoming ready. Check $logPath."
    }
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 2 -UseBasicParsing
        if ($response.StatusCode -eq 200) {
            Write-Output "Live frontend preview: http://127.0.0.1:$FrontendPort/admin/"
            Write-Output "API proxy: http://127.0.0.1:$ApiPort"
            exit 0
        }
    } catch {
        Start-Sleep -Milliseconds 250
    }
}

throw "Vite did not become ready within 15 seconds. Check $logPath."

