# Shared helpers for InfraSpine Windows service wrappers. Dot-source only.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-InfraRoot {
    if ($env:INFRA_ROOT) {
        return (Resolve-Path -LiteralPath $env:INFRA_ROOT).Path
    }
    # scripts/ops/windows/<script>.ps1
    return (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
}

function Get-InfraPython([string]$Root) {
    return (Join-Path $Root '.venv\Scripts\python.exe')
}

function Get-InfraLogDir([string]$Root) {
    $dir = Join-Path $Root 'logs'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    return $dir
}

function Rotate-InfraLog([string]$Path, [int]$MaxBytes = 10485760, [int]$Keep = 5) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ((Get-Item -LiteralPath $Path).Length -le $MaxBytes) { return }
    for ($i = $Keep; $i -ge 1; $i--) {
        $src = if ($i -eq 1) { $Path } else { "$Path.$($i-1)" }
        $dst = "$Path.$i"
        if (Test-Path -LiteralPath $src) {
            Move-Item -LiteralPath $src -Destination $dst -Force
        }
    }
}

function Write-InfraLog([string]$LogPath, [string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss zzz} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Assert-InfraVenv([string]$Python, [string]$LogPath) {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        Write-InfraLog $LogPath "ERROR venv python was not found: $Python"
        exit 2
    }
    $probe = & $Python -c "import sys; print(sys.prefix); print(sys.base_prefix); print('OK' if sys.prefix != sys.base_prefix else 'NOT_VENV')"
    if ($LASTEXITCODE -ne 0) {
        Write-InfraLog $LogPath "ERROR venv python could not start: $Python"
        exit 2
    }
    $lines = @($probe -split '\r?\n' | Where-Object { $_ })
    $marker = $lines[-1]
    if ($marker -ne 'OK') {
        Write-InfraLog $LogPath "ERROR $Python is not a virtualenv (refusing system Python)"
        exit 2
    }
    Write-InfraLog $LogPath ("venv ok prefix={0} base={1} exe={2}" -f $lines[0], $lines[1], $Python)
}
