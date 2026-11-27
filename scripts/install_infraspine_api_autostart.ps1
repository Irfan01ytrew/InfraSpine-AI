$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$TaskName = 'InfraSpine-IRFANIX-API'
$RunAs = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python environment was not found: $Python"
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument '-m uvicorn app.main:app --host 127.0.0.1 --port 8000' `
    -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $RunAs
$Trigger.Delay = 'PT30S'
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 20 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId $RunAs -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description 'Starts the InfraSpine API from its Downloads checkout at user logon and retries failed starts.' `
    -Force | Out-Null

Write-Output "Registered $TaskName for $RunAs at logon with a 30-second delay and bounded failure retries."
