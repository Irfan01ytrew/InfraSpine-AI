"""Installation-scoped, metadata-only operational measurements (Module 11).

No document contents, user identifiers, credentials, query text or endpoint URLs.
One deployment/database is one tenant; this is not a cross-tenant fleet API.
"""

import asyncio
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app import __version__


def resource_sample(data_dir: Path) -> dict:
    """OS-visible measurements; containers may expose host totals, labelled accordingly."""
    import psutil

    memory = psutil.virtual_memory()
    process = psutil.Process()
    network = psutil.net_io_counters()
    result = {
        "status": "measured", "scope": "OS-visible resources; not a container quota",
        "cpu_percent": psutil.cpu_percent(interval=0.1), "logical_cpus": psutil.cpu_count(),
        "memory_used_bytes": memory.total - memory.available, "memory_total_bytes": memory.total,
        "memory_percent": memory.percent, "process_rss_bytes": process.memory_info().rss,
        "process_uptime_seconds": max(0, round(time.time() - process.create_time(), 1)),
        "network_sent_bytes": network.bytes_sent, "network_received_bytes": network.bytes_recv,
        "network_scope": "OS cumulative counters since boot; not a transfer rate",
        "disk": {"status": "unavailable"}, "gpu": {"status": "unavailable", "devices": []},
    }
    if data_dir.is_dir():
        disk = psutil.disk_usage(data_dir)
        result["disk"] = {"status": "measured", "total_bytes": disk.total, "used_bytes": disk.used,
                          "free_bytes": disk.free, "percent": disk.percent}
    executable = shutil.which("nvidia-smi")
    if executable:
        try:
            probe = subprocess.run(
                [executable, "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2, check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            devices = []
            for row in probe.stdout.splitlines()[:16]:
                utilization, used, total = [float(value.strip()) for value in row.split(",")]
                devices.append({"utilization_percent": utilization, "memory_used_mib": used, "memory_total_mib": total})
            result["gpu"] = {"status": "measured", "devices": devices}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return result


async def database_sample(engine, heartbeat_seconds: float) -> dict:
    """Bound the entire probe and every query; a rollback leaves the application untouched."""
    started = time.perf_counter()
    async with asyncio.timeout(6):
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await connection.execute(text("SET LOCAL statement_timeout = '1500ms'"))
                await connection.execute(text("SELECT 1"))
                latency = round((time.perf_counter() - started) * 1000, 2)
                info = dict((await connection.execute(text(
                    "SELECT pg_database_size(current_database()) AS size_bytes, "
                    "current_setting('server_version') AS version, "
                    "(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()) AS connections, "
                    "(SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS role_superuser"
                ))).mappings().one())
                heads = list((await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars())
                workers = dict((await connection.execute(text(
                    "SELECT count(*) FILTER (WHERE heartbeat_at >= now() - :seconds * interval '1 second') AS online, "
                    "count(*) FILTER (WHERE heartbeat_at < now() - :seconds * interval '1 second') AS stale, "
                    "coalesce(sum(active_jobs) FILTER (WHERE heartbeat_at >= now() - :seconds * interval '1 second'), 0) AS active_jobs "
                    "FROM ingestion_workers"
                ), {"seconds": heartbeat_seconds * 3})).mappings().one())
                queues = {}
                for name, table in (("ingestion", "documents"), ("workflows", "automation_runs")):
                    queues[name] = dict((await connection.execute(text(
                        f"SELECT status, count(*) FROM {table} GROUP BY status"
                    ))).all())
                vectors = dict((await connection.execute(text(
                    "SELECT count(*) FILTER (WHERE status='ready' AND indexed_version IS DISTINCT FROM version) AS version_mismatches, "
                    "count(*) FILTER (WHERE status='processing' AND locked_until < now()) AS expired_leases, "
                    "count(*) FILTER (WHERE indexed_at >= now()-interval '1 hour') AS documents_indexed_last_hour FROM documents"
                ))).mappings().one())
                inference = dict((await connection.execute(text(
                    "SELECT count(*) AS requests, count(*) FILTER (WHERE status='error') AS errors, "
                    "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms, "
                    "coalesce(sum(prompt_tokens),0) AS prompt_tokens, coalesce(sum(completion_tokens),0) AS completion_tokens "
                    "FROM rag_traces WHERE created_at >= now()-interval '1 hour'"
                ))).mappings().one())
                # Provider names/model labels only; never load base_url, settings or ciphertext.
                providers = [dict(row) for row in (await connection.execute(text(
                    "SELECT provider, model, is_active FROM provider_configs ORDER BY provider LIMIT 50"
                ))).mappings()]
    return {"status": "measured", "latency_ms": latency, **info, "migration_heads": heads,
            "workers": workers, "queues": queues, "vectors": vectors, "inference_last_hour": inference,
            "providers": providers}


def alerts_for(resources: dict, database: dict) -> list[dict]:
    alerts = []
    def add(code, severity, message):
        alerts.append({"code": code, "severity": severity, "message": message})
    if database.get("status") != "measured":
        add("database_probe_failed", "critical", "Database telemetry is unavailable or exceeded its time budget.")
    else:
        if not database["workers"]["online"]:
            add("ingestion_workers_missing", "warning", "No recent ingestion worker heartbeat; queued documents will wait.")
        if database["role_superuser"]:
            add("database_superuser", "critical", "The application database role is a superuser. Provision a restricted role.")
        if database["vectors"]["version_mismatches"]:
            add("vector_version_mismatch", "warning", "Ready documents have an index version mismatch; investigate reconciliation.")
        if database["queues"]["ingestion"].get("failed", 0):
            add("ingestion_failed", "warning", "Document processing failures require review in Knowledge.")
        if database["vectors"]["expired_leases"]:
            add("ingestion_lease_expired", "warning", "Expired document leases are awaiting worker recovery.")
    if resources.get("status") != "measured":
        add("resource_probe_failed", "warning", "OS resource telemetry is unavailable.")
    if resources.get("disk", {}).get("percent", 0) >= 85:
        add("storage_pressure", "critical", "The data filesystem is at least 85% full.")
    if resources.get("memory_percent", 0) >= 90:
        add("memory_pressure", "warning", "OS-visible RAM utilization is at least 90%.")
    return alerts


class InfrastructureMonitor:
    """Per-process cache, single in-flight collection, no background thread or stored user data."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.cached = None
        self.collected = 0.0

    async def snapshot(self, app, settings):
        async with self.lock:
            if self.cached is not None and time.monotonic() - self.collected < 10:
                return self.cached
            resources, database = await asyncio.gather(
                asyncio.to_thread(resource_sample, settings.rag_data_dir),
                database_sample(app.state.engine, settings.rag_worker_heartbeat_seconds), return_exceptions=True,
            )
            if isinstance(resources, BaseException):
                resources = {"status": "unavailable"}
            if isinstance(database, BaseException):
                database = {"status": "unavailable"}
            alerts = alerts_for(resources, database)
            revision = os.environ.get("INFRASPINE_RELEASE", "unversioned")
            if not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", revision):
                revision = "invalid-release-label"
            tasks = getattr(app.state, "infrastructure_tasks", [])
            task_states = [{"name": task.get_name(), "status": "stopped" if task.done() else "running"} for task in tasks]
            if any(task.done() for task in tasks):
                alerts.append({"code": "background_task_stopped", "severity": "critical", "message": "An API background task stopped unexpectedly."})
            self.cached = {
                "schema_version": 1, "observed_at": datetime.now(UTC).isoformat(), "cache_seconds": 10,
                "scope": "current installation", "status": "degraded" if alerts else "observed",
                "release": {"version": __version__, "revision": revision, "environment": settings.app_env},
                "resources": resources, "database": database, "alerts": alerts, "background_tasks": task_states,
                "services": {
                    "api": "responding", "frontend": "artifact_present" if Path("frontend/dist/index.html").is_file() else "artifact_missing",
                    "index_role": getattr(getattr(app.state, "rag", None), "index_role", "unknown"),
                    "vector_store": settings.rag_vector_store, "provider_availability": "not_probed",
                },
                "security": {"secure_cookies": settings.session_cookie_secure, "mfa_required": settings.org_mfa_required,
                             "local_models_only": settings.llm_local_only, "parser_isolation": settings.rag_parse_isolation},
                "recovery": {"status": "unverified", "message": "A successful restore drill is required; archive presence alone does not establish recovery readiness."},
            }
            self.collected = time.monotonic()
            return self.cached
