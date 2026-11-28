"""Step Q (slice 1): read-only control-room overview.

Aggregates the unified workflow engine and Agent Studio state for one
department scope so the control room can show every agent's live status,
progress, and blockers on one screen.

Pure reads. This module never executes, pauses, or resumes anything, never
decrypts a sealed run payload or Studio run state, and never grants a
permission. Authorization happens in the route before this is called
(`require_scope` -> ``app.platform`` group gate, permission
``workflow.access``: 404 for non-members, 403 for members without it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.agent_studio_models import AgentStudioAgent, AgentStudioRun
from app.db.models import AccessGroupMember
from app.db.workflow_engine_models import Workflow, WorkflowEvent, WorkflowRun

MAX_AGENTS = 50
MAX_BLOCKERS = 10


async def workflow_engine_health(db) -> dict:
    """Slice 3: workflow-engine worker health for ``/health/ready``.

    The engine keeps no heartbeat row — a tick that has nothing to do
    leaves no trace — so "last tick" is the newest ``wf_events`` row:
    every engine transition (claim, step completion, failure, pause,
    resume) writes one. An idle engine with nothing queued therefore
    reports an old timestamp honestly; ``due_runs`` (active runs whose
    ``wake_at`` has passed) is the backlog signal a watchdog should
    watch alongside it. ``engine_on`` is the Gap 5 kill switch
    (``WORKFLOW_ENGINE_WORKER_ENABLED``, default on); "off" is a valid
    operator choice, not an outage, so callers must not turn it into a
    503 on their own.
    """
    settings = get_settings()
    engine_on = bool(getattr(settings, "workflow_engine_on", False))
    last_tick_at = await db.scalar(select(func.max(WorkflowEvent.created_at)))
    due_runs = await db.scalar(
        select(func.count())
        .select_from(WorkflowRun)
        .where(
            WorkflowRun.status.in_(("queued", "running")),
            WorkflowRun.wake_at <= datetime.now(UTC),
        )
    )
    return {
        "engine_on": engine_on,
        "status": "on" if engine_on else "off",
        "last_tick_at": last_tick_at,
        "due_runs": int(due_runs or 0),
    }


async def control_room_overview(db, *, scope_id: UUID, limit: int = 10) -> dict:
    """One read-only snapshot of a scope: worker health, run roll-ups,
    the Agent Studio registry, recent runs, and blockers."""
    settings = get_settings()

    status_rows = (await db.execute(
        select(WorkflowRun.status, func.count())
        .where(WorkflowRun.scope_id == scope_id)
        .group_by(WorkflowRun.status)
    )).all()
    origin_rows = (await db.execute(
        select(WorkflowRun.origin, func.count())
        .where(WorkflowRun.scope_id == scope_id)
        .group_by(WorkflowRun.origin)
    )).all()

    recent = (await db.execute(
        select(WorkflowRun, Workflow.name)
        .join(Workflow, (Workflow.id == WorkflowRun.workflow_id)
              & (Workflow.scope_id == WorkflowRun.scope_id))
        .where(WorkflowRun.scope_id == scope_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(limit)
    )).all()

    member_ids = list(await db.scalars(
        select(AccessGroupMember.user_id)
        .where(AccessGroupMember.group_id == scope_id)
    ))
    agents: list[dict] = []
    if member_ids:
        agent_rows = (await db.execute(
            select(AgentStudioAgent)
            .where(AgentStudioAgent.owner_id.in_(member_ids))
            .order_by(AgentStudioAgent.updated_at.desc())
            .limit(MAX_AGENTS)
        )).scalars().all()
        agent_ids = [a.id for a in agent_rows]
        latest_by_agent: dict[UUID, AgentStudioRun] = {}
        if agent_ids:
            run_rows = (await db.execute(
                select(AgentStudioRun)
                .where(AgentStudioRun.agent_id.in_(agent_ids))
                .order_by(AgentStudioRun.created_at.desc())
            )).scalars().all()
            for run in run_rows:
                latest_by_agent.setdefault(run.agent_id, run)
        for agent in agent_rows:
            latest = latest_by_agent.get(agent.id)
            agents.append({
                "id": str(agent.id),
                "name": agent.name,
                "owner_id": str(agent.owner_id),
                "enabled": agent.enabled,
                "paused": agent.paused,
                "latest_run": None if latest is None else {
                    "id": str(latest.id),
                    "status": latest.status,
                    "failure_code": latest.failure_code,
                    "llm_calls": latest.llm_calls,
                    "prompt_tokens": latest.prompt_tokens,
                    "completion_tokens": latest.completion_tokens,
                    "created_at": latest.created_at,
                    "started_at": latest.started_at,
                    "finished_at": latest.finished_at,
                },
            })

    blockers: list[dict] = []
    paused = (await db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.scope_id == scope_id, WorkflowRun.status == "paused")
        .order_by(WorkflowRun.created_at.desc())
        .limit(MAX_BLOCKERS)
    )).scalars().all()
    for run in paused:
        blockers.append({
            "kind": "run_paused",
            "run_id": str(run.id),
            "origin": run.origin,
            "reason": run.blocked_reason,
            "created_at": run.created_at,
        })
    failed = (await db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.scope_id == scope_id, WorkflowRun.status == "failed")
        .order_by(WorkflowRun.created_at.desc())
        .limit(MAX_BLOCKERS)
    )).scalars().all()
    for run in failed:
        blockers.append({
            "kind": "run_failed",
            "run_id": str(run.id),
            "origin": run.origin,
            "reason": run.failure_code,
            "created_at": run.created_at,
        })
    if member_ids:
        waiting = (await db.execute(
            select(AgentStudioRun, AgentStudioAgent.name)
            .join(AgentStudioAgent, AgentStudioAgent.id == AgentStudioRun.agent_id)
            .where(AgentStudioRun.owner_id.in_(member_ids),
                   AgentStudioRun.status == "awaiting_approval")
            .order_by(AgentStudioRun.created_at.desc())
            .limit(MAX_BLOCKERS)
        )).all()
        for run, name in waiting:
            blockers.append({
                "kind": "run_awaiting_approval",
                "run_id": str(run.id),
                "origin": "studio",
                "reason": f"Agent '{name}' is waiting for an approval.",
                "created_at": run.created_at,
            })
    blockers.sort(key=lambda b: b["created_at"], reverse=True)
    blockers = blockers[:MAX_BLOCKERS]

    return {
        "scope_id": str(scope_id),
        "worker": {"engine_on": bool(getattr(settings, "workflow_engine_on", False))},
        "runs_by_status": {status: count for status, count in status_rows},
        "runs_by_origin": {origin: count for origin, count in origin_rows},
        "agents": agents,
        "recent_runs": [
            {
                "id": str(run.id),
                "workflow_id": str(run.workflow_id),
                "workflow_name": name,
                "status": run.status,
                "phase": run.phase,
                "origin": run.origin,
                "origin_ref": run.origin_ref,
                "blocked_reason": run.blocked_reason,
                "failure_code": run.failure_code,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
            }
            for run, name in recent
        ],
        "blockers": blockers,
    }
