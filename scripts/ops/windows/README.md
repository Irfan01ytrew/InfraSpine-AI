# Windows runtime (venv + scheduled tasks)

These scripts keep the native Windows install running from the project `.venv`,
not a system Python, and keep the document worker up after a crash or a missed
logon start.

## What is installed

| Task | Script | When |
| --- | --- | --- |
| `InfraSpine-IRFANIX-API` | `run-api.ps1` | At logon (30s delay), restart on failure |
| `InfraSpine-IRFANIX-Worker` | `run-worker.ps1` | At logon (60s delay), restart on failure |
| `InfraSpine-IRFANIX-Watchdog` | `watchdog.ps1` | At logon, then every 5 minutes |

The watchdog starts a missing process only. It will not restart a healthy API
that is already listening on `127.0.0.1:8000`.

Wrapper logs (rotated at 10 MiB, 5 backups) live in the install `logs/` folder:

- `api-wrapper.log`
- `worker-wrapper.log`
- `watchdog.log`

The applications still write `infraspine.log` and `worker.log`.

## Windows venv process path

On Windows, `\.venv\Scripts\python.exe` is a launcher. Task Manager may show
the child as `...\Python312\python.exe` even though `sys.prefix` is the venv
and packages load from `\.venv\Lib\site-packages`. The wrappers refuse to
start if `sys.prefix == sys.base_prefix`.

## Install / repair

From an interactive logon of the account that should run the services (no
Administrator prompt is required):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\ops\windows\install-autostart.ps1
```

Existing task definitions are exported first to
`..\infraspine-live-backups\<timestamp>-windows-runtime\`.

A proper Windows service (NSSM / WinSW) would need an Administrator prompt;
these scheduled tasks do not.

These scripts do not change `.env`, database contents, or uploaded documents.
