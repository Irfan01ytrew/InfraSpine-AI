# Start the InfraSpine document worker from the project venv only.
# Used by the InfraSpine-IRFANIX-Worker scheduled task.
# The same process runs the unified workflow engine (automations, invoices, governance actions) unless
# WORKFLOW_ENGINE_WORKER_ENABLED=false is set in .env (kill switch).
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_common.ps1')

$Root = Get-InfraRoot
$Python = Get-InfraPython $Root
$LogDir = Get-InfraLogDir $Root
$Log = Join-Path $LogDir 'worker-wrapper.log'
Rotate-InfraLog $Log
Write-InfraLog $Log "Document worker wrapper starting root=$Root"
$engineFlag = $env:WORKFLOW_ENGINE_WORKER_ENABLED
if (-not $engineFlag) {
    $envFile = Join-Path $Root '.env'
    if (Test-Path -LiteralPath $envFile) {
        $line = Select-String -LiteralPath $envFile -Pattern '^\s*WORKFLOW_ENGINE_WORKER_ENABLED\s*=\s*(\S+)' | Select-Object -Last 1
        if ($line) { $engineFlag = $line.Matches[0].Groups[1].Value }
    }
}
if ($engineFlag -and $engineFlag.Trim('"').ToLowerInvariant() -eq 'false') {
    Write-InfraLog $Log "Workflow engine: OFF (WORKFLOW_ENGINE_WORKER_ENABLED=false)"
} else {
    Write-InfraLog $Log "Workflow engine: ON (default; set WORKFLOW_ENGINE_WORKER_ENABLED=false in .env to stop it)"
}
Assert-InfraVenv $Python $Log

Set-Location -LiteralPath $Root
$env:PYTHONUNBUFFERED = '1'
& $Python -m app.worker
$code = $LASTEXITCODE
Write-InfraLog $Log "Document worker exited code=$code"
exit $code
