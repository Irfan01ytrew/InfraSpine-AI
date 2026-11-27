# Start the InfraSpine document worker from the project venv only.
# Used by the InfraSpine-IRFANIX-Worker scheduled task.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_common.ps1')

$Root = Get-InfraRoot
$Python = Get-InfraPython $Root
$LogDir = Get-InfraLogDir $Root
$Log = Join-Path $LogDir 'worker-wrapper.log'
Rotate-InfraLog $Log
Write-InfraLog $Log "Document worker wrapper starting root=$Root"
Assert-InfraVenv $Python $Log

Set-Location -LiteralPath $Root
$env:PYTHONUNBUFFERED = '1'
& $Python -m app.worker
$code = $LASTEXITCODE
Write-InfraLog $Log "Document worker exited code=$code"
exit $code
