"""Database-backed Agent Studio performance analytics and live host measurements."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun
from app.db.models import User

_STATUSES = ("queued", "running", "awaiting_approval", "completed", "failed", "cancelled")


def _int(value: Any) -> int:
    return int(value or 0)


def _float(value: Any) -> float | None:
    return round(float(value), 2) if value is not None else None


def process_metrics() -> dict[str, Any]:
    """Return real process/system measurements; unavailable hardware stays null."""
    result: dict[str, Any] = {
        "available": False,
        "host_cpu_percent": None,
        "process_cpu_percent": None,
        "process_rss_bytes": None,
        "host_memory_percent": None,
        "host_memory_available_bytes": None,
        "gpu_available": False,
        "gpu_utilization_percent": None,
        "gpu_memory_used_bytes": None,
        "gpu_memory_total_bytes": None,
    }
    try:
        import psutil

        process = psutil.Process()
        memory = psutil.virtual_memory()
        result.update({
            "available": True,
            "host_cpu_percent": _float(psutil.cpu_percent(interval=0.1)),
            "process_cpu_percent": _float(process.cpu_percent(interval=0.0)),
            "process_rss_bytes": int(process.memory_info().rss),
            "host_memory_percent": _float(memory.percent),
            "host_memory_available_bytes": int(memory.available),
        })
    except Exception:
        pass

    executable = shutil.which("nvidia-smi")
    if not executable:
        return result
    try:
        output = subprocess.run(
            [executable, "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout
        rows = [tuple(float(value.strip()) for value in line.split(",")) for line in output.splitlines() if line.strip()]
        if rows:
            result.update({
                "gpu_available": True,
                "gpu_utilization_percent": _float(sum(row[0] for row in rows) / len(rows)),
                "gpu_memory_used_bytes": int(sum(row[1] for row in rows) * 1024 * 1024),
                "gpu_memory_total_bytes": int(sum(row[2] for row in rows) * 1024 * 1024),
            })
    except Exception:
        pass
    return result


async def agent_studio_analytics(
    db: AsyncSession,
    user: User,
    *,
    days: int,
    worker_enabled: bool,
    worker_running: bool,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    first_day = now.date() - timedelta(days=days - 1)
    since = datetime.combine(first_day, time.min, UTC)
    scope = (AgentStudioRun.owner_id == user.id, AgentStudioRun.created_at >= since)

    status_rows = await db.execute(
        select(AgentStudioRun.status, func.count(AgentStudioRun.id))
        .where(*scope)
        .group_by(AgentStudioRun.status)
    )
    counts = {status: 0 for status in _STATUSES}
    for status, count in status_rows:
        if status in counts:
            counts[status] = _int(count)

    duration_ms = func.extract("epoch", AgentStudioRun.finished_at - AgentStudioRun.started_at) * 1000
    queue_ms = func.extract("epoch", AgentStudioRun.started_at - AgentStudioRun.created_at) * 1000
    aggregate = (await db.execute(select(
        func.count(AgentStudioRun.id),
        func.avg(duration_ms).filter(AgentStudioRun.finished_at.is_not(None)),
        func.avg(queue_ms).filter(AgentStudioRun.started_at.is_not(None)),
        func.sum(AgentStudioRun.prompt_tokens),
        func.sum(AgentStudioRun.completion_tokens),
        func.sum(AgentStudioRun.llm_calls),
        func.sum(AgentStudioRun.estimated_cost_usd),
        func.count(AgentStudioRun.estimated_cost_usd),
    ).where(*scope))).one()
    total_runs = _int(aggregate[0])
    priced_runs = _int(aggregate[7])
    terminal_runs = counts["completed"] + counts["failed"]

    daily_rows = await db.execute(select(
        func.date_trunc("day", AgentStudioRun.created_at).label("day"),
        AgentStudioRun.status,
        func.count(AgentStudioRun.id),
    ).where(*scope).group_by("day", AgentStudioRun.status).order_by("day"))
    daily_by_date: dict[str, dict[str, int]] = {}
    for offset in range(days):
        key = (first_day + timedelta(days=offset)).isoformat()
        daily_by_date[key] = {status: 0 for status in _STATUSES}
    for day_value, status, count in daily_rows:
        key = day_value.date().isoformat()
        if key in daily_by_date and status in daily_by_date[key]:
            daily_by_date[key][status] = _int(count)
    daily = []
    for key, values in daily_by_date.items():
        terminal = values["completed"] + values["failed"]
        daily.append({
            "date": key,
            **values,
            "success_rate": round(values["completed"] * 100 / terminal, 2) if terminal else None,
        })

    completed = case((AgentStudioRun.status == "completed", 1), else_=0)
    failed = case((AgentStudioRun.status == "failed", 1), else_=0)
    queued = case((AgentStudioRun.status == "queued", 1), else_=0)
    running = case((AgentStudioRun.status == "running", 1), else_=0)
    awaiting = case((AgentStudioRun.status == "awaiting_approval", 1), else_=0)
    agents_rows = await db.execute(select(
        AgentStudioAgent.id,
        AgentStudioAgent.name,
        AgentStudioAgent.enabled,
        func.count(AgentStudioRun.id),
        func.sum(completed),
        func.sum(failed),
        func.sum(queued),
        func.sum(running),
        func.sum(awaiting),
        func.avg(duration_ms).filter(AgentStudioRun.finished_at.is_not(None)),
        func.sum(AgentStudioRun.prompt_tokens),
        func.sum(AgentStudioRun.completion_tokens),
    ).outerjoin(AgentStudioRun, and_(
        AgentStudioRun.agent_id == AgentStudioAgent.id,
        AgentStudioRun.owner_id == user.id,
        AgentStudioRun.created_at >= since,
    )).where(AgentStudioAgent.owner_id == user.id).group_by(
        AgentStudioAgent.id, AgentStudioAgent.name, AgentStudioAgent.enabled,
    ).order_by(func.count(AgentStudioRun.id).desc(), AgentStudioAgent.name))
    agents = []
    for row in agents_rows:
        total = _int(row[3])
        succeeded, failed_count = _int(row[4]), _int(row[5])
        queued_count, running_count, awaiting_count = _int(row[6]), _int(row[7]), _int(row[8])
        terminal = succeeded + failed_count
        agents.append({
            "id": str(row[0]),
            "name": row[1],
            "enabled": bool(row[2]),
            "total_runs": total,
            "completed": succeeded,
            "failed": failed_count,
            "queued": queued_count,
            "running": running_count,
            "awaiting_approval": awaiting_count,
            "success_rate": round(succeeded * 100 / terminal, 2) if terminal else None,
            "failure_rate": round(failed_count * 100 / terminal, 2) if terminal else None,
            "average_execution_ms": _float(row[9]),
            "prompt_tokens": _int(row[10]),
            "completion_tokens": _int(row[11]),
        })

    return {
        "period_days": days,
        "worker_enabled": worker_enabled,
        "worker_running": worker_running,
        "totals": {
            **counts,
            "total_runs": total_runs,
            "success_rate": round(counts["completed"] * 100 / terminal_runs, 2) if terminal_runs else None,
            "failure_rate": round(counts["failed"] * 100 / terminal_runs, 2) if terminal_runs else None,
            "average_execution_ms": _float(aggregate[1]),
            "average_queue_latency_ms": _float(aggregate[2]),
            "prompt_tokens": _int(aggregate[3]),
            "completion_tokens": _int(aggregate[4]),
            "llm_calls": _int(aggregate[5]),
            "estimated_cost_usd": _float(aggregate[6]) if priced_runs else None,
            "runs_without_cost": max(0, total_runs - priced_runs),
        },
        "daily": daily,
        "agents": agents,
        "system": process_metrics() if user.role == "admin" else None,
    }
