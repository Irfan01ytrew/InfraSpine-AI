# Start the InfraSpine API from the project venv only, bound to localhost.
# Used by the InfraSpine-IRFANIX-API scheduled task. On Windows the child
# process path may show the base CPython exe; sys.prefix still points at .venv.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_common.ps1')

$Root = Get-InfraRoot
$Python = Get-InfraPython $Root
$LogDir = Get-InfraLogDir $Root
$Log = Join-Path $LogDir 'api-wrapper.log'
Rotate-InfraLog $Log
Write-InfraLog $Log "API wrapper starting root=$Root"
Assert-InfraVenv $Python $Log

Set-Location -LiteralPath $Root
$env:PYTHONUNBUFFERED = '1'
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
$code = $LASTEXITCODE
Write-InfraLog $Log "API exited code=$code"
exit $code
