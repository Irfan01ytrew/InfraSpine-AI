# Docker Desktop WSL VHDX: data-preserving recovery plan

**Status: preparation only. Do not run until an independent external backup destination is available.** This plan preserves the configured Docker Desktop WSL data disk before any vendor-directed repair, update, or reinstall. It does not reset Docker or modify the VHDX.

## Known source and gates

- Configured source recorded in the 2026-10-26 checkpoint: `D:\DockerDesktopData\DockerDesktopWSL\disk\docker_data.vhdx`.
- Last recorded size: **68,976,377,856 bytes**. Re-read the current path and size before acting; Docker Desktop Settings > Resources > Advanced is authoritative if the configured location changed.
- Use a physically separate external disk, preferably NTFS, with at least **80 GB free**. A second partition on the same physical disk, a cloud-sync folder, or a network mapping without confirmed capacity and stability is not an adequate copy target.
- Stop if Docker Desktop or WSL cannot be fully stopped, if the source path is uncertain, the external disk is not physically independent, or any copy/hash check fails.
- Docker's documented WSL recovery preserves the `.vhdx` at the same configured location after reinstall. Follow Docker Support's case-specific instructions for this missing guest-component failure; do not assume a particular release fixes it. See [Docker's backup and restore guide](https://docs.docker.com/desktop/settings-and-maintenance/backup-and-restore/).

## 1. Select and verify the external destination

Replace `X` with the external disk's actual drive letter. Confirm physically in Disk Management that it is a separate device from the disk containing `D:`. The commands below only inspect the disks and volumes.

```powershell
$ErrorActionPreference = 'Stop'
$sourcePath = 'D:\DockerDesktopData\DockerDesktopWSL\disk\docker_data.vhdx'
$sourceDrive = 'D'
$backupDrive = 'X' # Replace with the verified external drive letter.
$requiredFreeBytes = 80GB

$sourceDisk = Get-Partition -DriveLetter $sourceDrive | Get-Disk
$backupDisk = Get-Partition -DriveLetter $backupDrive | Get-Disk
$backupVolume = Get-Volume -DriveLetter $backupDrive

if ($sourceDisk.Number -eq $backupDisk.Number) { throw 'Backup disk is not physically separate from the source disk.' }
if ($backupVolume.FileSystem -ne 'NTFS') { throw 'Use a verified NTFS external destination; do not format a disk containing data.' }
if ($backupVolume.SizeRemaining -lt $requiredFreeBytes) { throw 'External destination needs at least 80 GB free.' }
if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) { throw 'Configured Docker VHDX path was not found; stop and verify Docker Desktop settings.' }

$sourceInfo = Get-Item -LiteralPath $sourcePath
$sourceInfo | Select-Object FullName, Length, LastWriteTime
$sourceDisk | Select-Object Number, FriendlyName, BusType, Size
$backupDisk | Select-Object Number, FriendlyName, BusType, Size
$backupVolume | Select-Object DriveLetter, FileSystem, SizeRemaining
```

If disk identity cannot be verified, do not copy yet. Do not create a new destination on `C:` or `D:`; the checkpoint's available space is below the VHDX size.

## 2. Fully stop Docker Desktop and confirm the VHDX is detached from WSL

Save work in Ubuntu and every other WSL distribution first. `wsl --shutdown` stops **all** running WSL distributions, not only Docker's. Docker's guide requires Desktop to be fully stopped before copying the VM disk; Microsoft documents `wsl --shutdown` as the command that shuts down all distributions.

```powershell
docker desktop stop --timeout 120
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop did not report a clean stop; do not copy the VHDX.' }
docker desktop status
if ($LASTEXITCODE -ne 0) { throw 'Could not verify Docker Desktop is stopped; do not copy the VHDX.' }

# Quit Docker Desktop from its tray menu if its UI remains open.
wsl.exe --shutdown
Start-Sleep -Seconds 10
wsl.exe --list --verbose
$sourceDiskImage = Get-DiskImage -ImagePath $sourcePath -ErrorAction Stop
if ($sourceDiskImage.Attached) { throw 'The Docker VHDX is still attached; do not copy it.' }
```

Continue only when Docker Desktop reports stopped, every listed WSL distribution including `docker-desktop` is `Stopped`, and `Get-DiskImage` reports the configured VHDX as unattached. If the command cannot verify attachment state, the stop command times out, a distribution returns to `Running`, or the Docker Desktop UI still shows it starting, **stop here** and request Docker's supported shutdown procedure. Do not copy a live VHDX and do not unregister a WSL distribution.

## 3. Copy the stopped VHDX to a new, empty folder

Use a new timestamped folder on the verified external disk. Never use `/MIR`, `/MOVE`, `/PURGE`, or an existing destination. The robocopy options below copy one file and preserve data, attributes, and timestamps; exit codes below 8 are non-failure codes, but hash verification is still mandatory.

```powershell
$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
$backupRoot = Join-Path ('{0}:\' -f $backupDrive) ('InfraSpineDockerRecovery\' + $stamp)
if (Test-Path -LiteralPath $backupRoot) { throw 'Destination already exists; choose a new timestamped folder.' }
New-Item -ItemType Directory -Path $backupRoot | Out-Null

$sourceName = Split-Path -Leaf $sourcePath
$sourceDirectory = Split-Path -Parent $sourcePath
$sourceBytesBefore = (Get-Item -LiteralPath $sourcePath).Length
$sourceHashBefore = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash

& robocopy.exe $sourceDirectory $backupRoot $sourceName /J /COPY:DAT /R:2 /W:5 /NP
$copyExitCode = $LASTEXITCODE
if ($copyExitCode -ge 8) { throw "Robocopy failed with exit code $copyExitCode; preserve both files and inspect the copy." }
```

Keep the machine on reliable power and do not launch Docker Desktop or WSL during the copy and verification.

## 4. Verify byte-for-byte identity

Re-hash the source after the copy as well as the external copy. All three size/hash values must match. A mismatch means the backup is not verified: preserve both files, do not retry over either one, and ask Docker Support for guidance.

```powershell
$backupPath = Join-Path $backupRoot $sourceName
$sourceBytesAfter = (Get-Item -LiteralPath $sourcePath).Length
$backupBytes = (Get-Item -LiteralPath $backupPath).Length
$sourceHashAfter = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash
$backupHash = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash

if ($sourceBytesBefore -ne $sourceBytesAfter) { throw 'Source size changed during backup; backup is not verified.' }
if ($sourceBytesAfter -ne $backupBytes) { throw 'Source and backup sizes differ; backup is not verified.' }
if ($sourceHashBefore -ne $sourceHashAfter -or $sourceHashAfter -ne $backupHash) { throw 'SHA-256 mismatch; backup is not verified.' }

[pscustomobject]@{
    SourcePath = $sourcePath
    BackupPath = $backupPath
    SizeBytes = $backupBytes
    SHA256 = $backupHash
    VerifiedAt = (Get-Date).ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupRoot 'verification.json') -Encoding UTF8
```

Keep the verified copy unchanged and disconnected when practical. Do not send the VHDX to Docker unless Docker Support explicitly requests it through an approved secure channel; it contains the container/image/volume data. If Ubuntu or another WSL distribution contains user data, back that distribution up separately using Microsoft's [WSL backup guidance](https://learn.microsoft.com/en-us/windows/wsl/basic-commands).

## 5. Obtain and follow vendor-supported recovery instructions

1. Review the existing local diagnostic bundle and its contents before sharing; Docker notes that diagnostics can contain personal data such as usernames and IP addresses. The existing no-upload bundle is recorded in `docs/completion-checkpoint.md`.
2. Give Docker Support the sanitized bootstrap error (`wsl-bootstrap.version` could not open `/opt/docker-desktop/componentsVersion.json`), installed Desktop/WSL versions, configured VHDX path, and verified source/backup sizes and SHA-256. Ask for a written procedure that preserves the configured WSL data disk and its path.
3. Treat Docker Desktop 4.97.0 as a candidate version only, never as a guaranteed fix. Its release notes mention a Windows hang fix after disk exhaustion or backend console-host exit, but do not name this missing guest-component error. Update only after the external copy verifies and according to the vendor/owner's chosen procedure.
4. Use only the vendor-directed repair/update/reinstall steps. Preserve the original VHDX and the verified external copy. Do not select **Clean up data**, **Reset to factory defaults**, unregister `docker-desktop`, change the backend, or move the disk image. If vendor instructions require modifying/replacing the VHDX, first make a second verified working copy and keep the first external copy untouched.
5. If a reinstall is directed, Docker's guide says to restore the `.vhdx` to the same configured location after reinstall. Do not overwrite or relocate the original unless the vendor's procedure explicitly requires it and the preserved copy has re-verified successfully immediately beforehand.

## 6. Validate Docker before restarting application stacks

After the vendor procedure, first run read-only checks: `docker desktop status`, `docker info`, `docker ps --all`, `docker image ls --digests`, `docker volume ls`, and `docker system df`. Confirm the Engine responds and that expected container/image/volume metadata is present. Compare against the pre-outage records in the checkpoint; historical counts are not a complete inventory, so do not use counts alone. Then verify the preserved project labels, database/vector volumes, advisory services, and 18108 readiness before starting or recreating any application stack. Do not prune, clean up data, or run `compose down -v`.

## References

- [Docker Desktop: Back up and restore data](https://docs.docker.com/desktop/settings-and-maintenance/backup-and-restore/)
- [Microsoft: Basic WSL commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)
- [Microsoft PowerShell: Get-DiskImage](https://learn.microsoft.com/en-us/powershell/module/storage/get-diskimage?view=windowsserver2025-ps)
- [Docker Desktop: Troubleshoot and get support](https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/)
- [Docker Desktop release notes](https://docs.docker.com/desktop/release-notes/)
