$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$TaskName = 'InfraSpine-IRFANIX-Worker'
$RunAs = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python environment was not found: $Python"
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument '-m app.worker' `
    -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $RunAs
$Trigger.Delay = 'PT60S'
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 20 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId $RunAs -LogonType Interactive -RunLevel Limited

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    $Current = @($Existing.Actions)
    if ($Current.Count -ne 1 -or
        $Current[0].Execute -ne $Python -or
        $Current[0].Argument -ne '-m app.worker' -or
        $Current[0].WorkingDirectory -ne $RepoRoot) {
        throw "Existing $TaskName has a different action; it was left untouched."
    }
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description 'Starts the InfraSpine document and scheduled workers from the Downloads checkout at user logon and retries failed starts.' `
    -Force | Out-Null

Write-Output "Registered $TaskName for $RunAs at logon with a 60-second delay and bounded failure retries."
