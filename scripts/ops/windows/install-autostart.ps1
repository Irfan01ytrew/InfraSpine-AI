# Backup and register the API, document worker, and watchdog scheduled tasks.
# Does not restart a healthy API. Starts the worker if it is not running.
# Run from an interactive logon of the service account (no admin required).
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_common.ps1')

$Root = Get-InfraRoot
$Python = Get-InfraPython $Root
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python environment was not found: $Python"
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$BackupRoot = Join-Path (Split-Path $Root -Parent) "infraspine-live-backups\$stamp-windows-runtime"
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null

foreach ($name in @('InfraSpine-IRFANIX-API', 'InfraSpine-IRFANIX-Worker', 'InfraSpine-IRFANIX-Watchdog')) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        $xml = Export-ScheduledTask -TaskName $name
        $out = Join-Path $BackupRoot "$name.xml"
        Set-Content -LiteralPath $out -Value $xml -Encoding UTF8
        Write-Output "Backed up $name to $out"
    }
}

$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$RunAs = "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId $RunAs -LogonType Interactive -RunLevel Limited

function New-InfraSettings([int]$RestartCount = 20) {
    return New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RestartCount $RestartCount `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
}

function Register-WrapperTask {
    param(
        [string]$Name,
        [string]$ScriptName,
        [string]$Description,
        [int]$LogonDelaySeconds,
        [int]$RestartCount = 20
    )
    $script = Join-Path $PSScriptRoot $ScriptName
    $action = New-ScheduledTaskAction `
        -Execute $PowerShell `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
        -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $RunAs
    if ($LogonDelaySeconds -gt 0) {
        $trigger.Delay = "PT${LogonDelaySeconds}S"
    }
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Settings (New-InfraSettings -RestartCount $RestartCount) `
        -Principal $Principal `
        -Description $Description `
        -Force | Out-Null
    Write-Output "Registered $Name -> $script"
}

Register-WrapperTask -Name 'InfraSpine-IRFANIX-API' -ScriptName 'run-api.ps1' `
    -Description 'Starts the InfraSpine API from the project venv at user logon, with wrapper logs and failure retries.' `
    -LogonDelaySeconds 30

Register-WrapperTask -Name 'InfraSpine-IRFANIX-Worker' -ScriptName 'run-worker.ps1' `
    -Description 'Starts the InfraSpine document worker from the project venv at user logon, with wrapper logs and failure retries.' `
    -LogonDelaySeconds 60

# Watchdog: at logon, then every 5 minutes. Do not set RestartCount=0 (invalid task XML).
$watchScript = Join-Path $PSScriptRoot 'watchdog.ps1'
$watchAction = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watchScript`"" `
    -WorkingDirectory $Root
$logon = New-ScheduledTaskTrigger -AtLogOn -User $RunAs
$logon.Delay = 'PT90S'
$repeat = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 999)
$watchSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 8) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
Register-ScheduledTask `
    -TaskName 'InfraSpine-IRFANIX-Watchdog' `
    -Action $watchAction `
    -Trigger @($logon, $repeat) `
    -Settings $watchSettings `
    -Principal $Principal `
    -Description 'Every 5 minutes, starts the API or document worker task if the process is missing. Does not restart a healthy API.' `
    -Force | Out-Null
Write-Output "Registered InfraSpine-IRFANIX-Watchdog -> $watchScript"

# Start the worker now if it is not already running. Leave a healthy API alone.
$workerRunning = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape('-m app.worker') }
if ($workerRunning) {
    Write-Output "Document worker already running PID=$($workerRunning[0].ProcessId); not restarting it."
} else {
    Write-Output "Starting InfraSpine-IRFANIX-Worker now."
    Start-ScheduledTask -TaskName 'InfraSpine-IRFANIX-Worker'
}

$apiListen = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($apiListen) {
    Write-Output "API already listening on 127.0.0.1:8000 PID=$($apiListen[0].OwningProcess); leaving it running."
} else {
    Write-Output "API is not listening; starting InfraSpine-IRFANIX-API."
    Start-ScheduledTask -TaskName 'InfraSpine-IRFANIX-API'
}

Write-Output "Backup folder: $BackupRoot"
Write-Output "Done."
