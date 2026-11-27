"""Read-only Windows diagnostics. User input never becomes a shell argument."""

import json
import os
import subprocess
from pathlib import Path

_STATUS_SCRIPT = """
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$cpu = @(Get-CimInstance Win32_Processor | Where-Object { $null -ne $_.LoadPercentage })
$disk = Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='" + $os.SystemDrive + "'")
[pscustomobject]@{
  cpu = ($cpu | Measure-Object LoadPercentage -Average).Average
  memoryFree = $os.FreePhysicalMemory
  memoryTotal = $os.TotalVisibleMemorySize
  diskFree = $disk.FreeSpace
  disk = $os.SystemDrive
} | ConvertTo-Json -Compress
"""


def windows_host_status() -> str:
    if os.name != "nt":
        return "Windows diagnostics are unavailable on this worker."
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-Command", _STATUS_SCRIPT],
            capture_output=True, check=True, timeout=15, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        data = json.loads(result.stdout)
        cpu = "unavailable" if data["cpu"] is None else f'{data["cpu"]:.0f}%'
        return (
            "*Windows worker diagnostics*\n"
            f"• CPU load: {cpu}\n"
            f'• Available RAM: {data["memoryFree"] / 1024**2:.1f} of {data["memoryTotal"] / 1024**2:.1f} GiB\n'
            f'• Free space on {data["disk"]}: {data["diskFree"] / 1024**3:.1f} GiB'
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return "*Diagnostics unavailable*\nThe Windows worker could not collect measurements."
