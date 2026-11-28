# Restart the API or document worker scheduled task if the process is missing.
# Does not restart a healthy API that is already listening on 127.0.0.1:8000.
# Step W slice 6: the Python watchdog CLI (app.self_healing.watchdog) is
# consulted first when it is available. Its decision is advisory and it
# records the incident + escalation in the database (or the out-of-band
# file when the database is down). A wedged process is never killed: the
# CLI says escalate_only and the owner decides. If the CLI is unavailable
# (venv missing, database and file both down, traceback), the script falls
# back to the old blind checks below and logs that it did so.
# NOTE: PowerShell syntax cannot be tested on the Linux dev VM; validate
# with `powershell -NoProfile -Command "Get-Command -Syntax ..."` or by
# running the script once on the Windows host before relying on it.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_common.ps1')

$Root = Get-InfraRoot
$LogDir = Get-InfraLogDir $Root
$Log = Join-Path $LogDir 'watchdog.log'
Rotate-InfraLog $Log

function Test-PythonCommand([string]$Pattern) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
    foreach ($p in $procs) {
        if ($p.CommandLine -and ($p.CommandLine -match $Pattern)) { return $true }
    }
    return $false
}

function Start-InfraTask([string]$Name) {
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-InfraLog $Log "ERROR scheduled task $Name is missing"
        return
    }
    if ($task.State -eq 'Running') {
        Write-InfraLog $Log "$Name is already marked Running; leaving it alone"
        return
    }
    Write-InfraLog $Log "Starting scheduled task $Name"
    Start-ScheduledTask -TaskName $Name
}

$workerRunning = Test-PythonCommand [regex]::Escape('-m app.worker')

# Ask the watchdog CLI for a decision (best effort).
$Decision = $null
$Python = Get-InfraPython $Root
if (Test-Path -LiteralPath $Python -PathType Leaf) {
    try {
        Set-Location -LiteralPath $Root
        $cliArgs = @('-m', 'app.self_healing.watchdog')
        if ($workerRunning) { $cliArgs += '--worker-running' }
        $DecisionJson = & $Python @cliArgs 2>$null | Select-Object -Last 1
        if ($DecisionJson) {
            $Decision = $DecisionJson | ConvertFrom-Json
            Write-InfraLog $Log ("watchdog decision action={0} kind={1} reason={2}" -f $Decision.action, $Decision.kind, $Decision.reason)
        } else {
            Write-InfraLog $Log 'watchdog CLI produced no output; using blind checks'
        }
    } catch {
        Write-InfraLog $Log 'watchdog CLI unavailable; using blind checks'
        $Decision = $null
    }
} else {
    Write-InfraLog $Log 'venv python not found; using blind checks'
}

# Document worker
if ($Decision) {
    if ($Decision.action -eq 'restart_worker') {
        Write-InfraLog $Log 'watchdog: no worker heartbeat anywhere; restarting worker task'
        Start-InfraTask 'InfraSpine-IRFANIX-Worker'
    } elseif ($Decision.kind -eq 'worker_stalled') {
        Write-InfraLog $Log 'watchdog: worker wedged (heartbeat stale, process exists); escalated, not killing it'
    } else {
        Write-InfraLog $Log 'watchdog: worker ok (heartbeat fresh, or worker runs on another machine)'
    }
} elseif ($workerRunning) {
    Write-InfraLog $Log 'Document worker process is running'
} else {
    Write-InfraLog $Log 'Document worker process is not running'
    Start-InfraTask 'InfraSpine-IRFANIX-Worker'
}

# API: only start if nothing is listening on localhost:8000
if ($Decision) {
    if ($Decision.action -eq 'restart_api') {
        Write-InfraLog $Log 'watchdog: API not listening; restarting API task'
        Start-InfraTask 'InfraSpine-IRFANIX-API'
    } elseif ($Decision.kind -eq 'api_down') {
        Write-InfraLog $Log 'API port is open but /health failed; not killing the process'
    } else {
        Write-InfraLog $Log 'watchdog: API healthy'
    }
} else {
    $listen = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($listen) {
        try {
            $health = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 8
            Write-InfraLog $Log ("API listening pid={0} health={1}" -f $listen[0].OwningProcess, $health.StatusCode)
        } catch {
            Write-InfraLog $Log "API port is open but /health failed; not killing the process"
        }
    } else {
        Write-InfraLog $Log 'API is not listening on 127.0.0.1:8000'
        Start-InfraTask 'InfraSpine-IRFANIX-API'
    }
}
