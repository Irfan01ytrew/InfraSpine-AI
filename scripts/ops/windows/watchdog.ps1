# Restart the API or document worker scheduled task if the process is missing.
# Does not restart a healthy API that is already listening on 127.0.0.1:8000.
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

# Document worker
if (Test-PythonCommand [regex]::Escape('-m app.worker')) {
    Write-InfraLog $Log 'Document worker process is running'
} else {
    Write-InfraLog $Log 'Document worker process is not running'
    Start-InfraTask 'InfraSpine-IRFANIX-Worker'
}

# API: only start if nothing is listening on localhost:8000
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
