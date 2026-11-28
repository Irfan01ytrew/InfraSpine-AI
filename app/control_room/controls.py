"""Step Q (slice 2): control-room controls.

Pause / resume a unified workflow run and pause / resume / reassign an
Agent Studio agent, from the control room, through the existing execution
paths only:

* Runs go through ``WorkflowService.control`` (``app/workflows/service.py``)
  — the unified engine. Pausing stops dispatch at the next step boundary;
  resuming continues from the last verified step and never executes a step
  twice. There is no second execution path here.
* Agents are paused/resumed by flipping the agent's own ``paused`` flag —
  the unified ``agent_studio.execute`` handler honours it before every
  attempt (a paused agent runs nothing until resumed). Assigning an agent
  transfers ownership to another member of the same scope.

Every control needs two things, in this order:

1. ``workflow.manage`` on the scope (``require_scope(..., manage=True)``):
   unknown scope or non-member → 404, member without the permission → 403.
2. An exact-action platform approval (``app/platform/approvals.py``):
   the request endpoint creates a ``PlatformApprovalRequest`` bound to the
   SHA-256 hash of the exact action (run/agent id + command + assignee);
   a different manager approves it through the approvals inbox
   (separation of duties — the requester can never approve their own
   request); the execute endpoint consumes it once. An approval for
   "pause run X" cannot resume run X, cannot pause run Y, and cannot be
   used twice; an expired approval is refused. The action specs are
   scoped to the *platform* scope id for the department (resolved with
   ``platform_scope_for``), not the legacy group id — the platform
   approval machinery only knows platform scopes.

The run/agent state is pre-validated *before* the approval is consumed,
so a stale or already-applied request never burns a good approval. The
consume + the state change commit (or roll back) together.

Studio-run mirroring note: ``AgentStudioRun.status`` has no ``paused``
value by design (check constraint). Pausing a studio-origin unified run
pauses the mirror: the engine stops dispatching the
``agent_studio.execute`` action, so the Studio run makes no further
attempt (an in-flight attempt finishes, exactly like a workflow cancel),
and resuming the mirror run lets it continue. The Studio run row itself
is left untouched — the unified run is the source of truth.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.core.errors import ConflictError, InvalidInputError, NotFoundError
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import AccessGroupMember, User
from app.platform.access import principal_for
from app.platform.approvals import ActionSpec, consume_approval, request_approval
from app.workflows.security import require_member, require_scope

RUN_COMMANDS = ("pause", "resume")
AGENT_COMMANDS = ("pause", "resume", "assign")

# Statuses a unified run must be in for each control command. Checked
# before the approval is consumed; WorkflowService.control re-checks
# under a row lock.
_RUN_COMMAND_STATES = {
    "pause": {"queued", "running"},
    "resume": {"paused", "failed"},
}


def run_control_spec(scope_id: UUID, run_id: UUID, command: str) -> ActionSpec:
    """The exact action a run-control approval covers."""
    return ActionSpec(
        "control_room.run_control",
        scope_id,
        {"run_id": str(run_id), "command": command},
        subject=f"workflow_run:{run_id}",
    )


def agent_control_spec(
    scope_id: UUID, agent_id: UUID, command: str, assignee_id: UUID | None = None
) -> ActionSpec:
    """The exact action an agent-control approval covers."""
    return ActionSpec(
        "control_room.agent_control",
        scope_id,
        {
            "agent_id": str(agent_id),
            "command": command,
            "assignee_id": str(assignee_id) if assignee_id is not None else None,
        },
        subject=f"agent_studio_agent:{agent_id}",
    )


def _validate_run_command(command: str) -> None:
    if command not in RUN_COMMANDS:
        raise InvalidInputError("Run control must be pause or resume.")


async def _platform_scope_id(db, scope_id: UUID) -> UUID:
    """The platform scope for a department group.

    Platform approvals are scoped to *platform* scope ids (their own
    UUIDs), not to the legacy department group id: ``authorize`` resolves
    the scope chain from ``platform_scopes``. Passing the group id makes
    every approval request 404 with ``scope_missing``.
    """
    from app.workflows.action_runtime import platform_scope_for

    platform_scope_id = await platform_scope_for(db, scope_id)
    if platform_scope_id is None:
        raise NotFoundError("Workflow scope not found.")
    return platform_scope_id


def _validate_agent_command(command: str, assignee_id: UUID | None) -> None:
    if command not in AGENT_COMMANDS:
        raise InvalidInputError("Agent control must be pause, resume, or assign.")
    if command == "assign" and assignee_id is None:
        raise InvalidInputError("Assigning an agent needs an assignee.")
    if command != "assign" and assignee_id is not None:
        raise InvalidInputError("Only assign takes an assignee.")


def _master_agent_id(owner_id: UUID) -> UUID:
    """The deterministic id of a user's permanent Master Agent."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"infraspine-agent-studio-master:{owner_id}")


async def _agent_in_scope(db, scope_id: UUID, agent_id: UUID) -> AgentStudioAgent:
    """An agent whose owner is a member of the scope, or 404 (an agent
    owned outside the scope is never revealed)."""
    agent = await db.get(AgentStudioAgent, agent_id)
    if agent is None:
        raise NotFoundError("Agent not found in this scope.")
    member = await db.get(AccessGroupMember, (scope_id, agent.owner_id))
    if member is None:
        raise NotFoundError("Agent not found in this scope.")
    return agent


async def request_run_control(db, user, scope_id: UUID, run_id: UUID, command: str):
    """Create the approval request for pausing/resuming one run.

    The caller must hold ``workflow.manage`` on the scope. The returned
    row carries the exact action hash the approver reviews; it commits
    inside ``request_approval`` by design.
    """
    _validate_run_command(command)
    await require_scope(db, user, scope_id, manage=True)
    from app.workflows.service import WorkflowService

    svc = WorkflowService(db, user, None)
    run = await svc.run(scope_id, run_id)
    if run.status not in _RUN_COMMAND_STATES[command]:
        raise ConflictError(
            f"This run cannot be {command}d in its current state ({run.status})."
        )
    return await request_approval(
        db,
        principal_for(user),
        run_control_spec(await _platform_scope_id(db, scope_id), run_id, command),
        required_permission="workflow.manage",
    )


async def execute_run_control(
    db, user, cipher, scope_id: UUID, run_id: UUID, command: str, approval_id: UUID, *, ctx=None
):
    """Consume the exact-action approval, then pause/resume the run
    through the unified engine. The approval and the state change commit
    (or roll back) together; the route commits."""
    _validate_run_command(command)
    await require_scope(db, user, scope_id, manage=True)
    from app.services.audit import record
    from app.workflows.service import WorkflowService

    svc = WorkflowService(db, user, cipher, ctx)
    run = await svc.run(scope_id, run_id, lock=True)
    if run.status not in _RUN_COMMAND_STATES[command]:
        raise ConflictError(
            f"This run cannot be {command}d in its current state ({run.status})."
        )
    await consume_approval(
        db, approval_id,
        run_control_spec(await _platform_scope_id(db, scope_id), run_id, command),
    )
    run = await svc.control(scope_id, run_id, command)
    if ctx is not None:
        record(
            db, ctx, f"control_room.run_{command}d",
            target=str(run.id),
            details={"command": command, "approval_id": str(approval_id)},
        )
    return run


async def request_agent_control(
    db, user, scope_id: UUID, agent_id: UUID, command: str, assignee_id: UUID | None = None
):
    """Create the approval request for pausing/resuming/assigning one agent."""
    _validate_agent_command(command, assignee_id)
    await require_scope(db, user, scope_id, manage=True)
    agent = await _agent_in_scope(db, scope_id, agent_id)
    await _check_agent_command(db, scope_id, agent, command, assignee_id)
    return await request_approval(
        db,
        principal_for(user),
        agent_control_spec(
            await _platform_scope_id(db, scope_id), agent_id, command, assignee_id
        ),
        required_permission="workflow.manage",
    )


async def _check_agent_command(db, scope_id: UUID, agent: AgentStudioAgent,
                               command: str, assignee_id: UUID | None) -> None:
    """Pre-validate an agent control before its approval is consumed."""
    if command == "pause" and agent.paused:
        raise ConflictError("This agent is already paused.")
    if command == "resume" and not agent.paused:
        raise ConflictError("This agent is not paused.")
    if command == "assign":
        if agent.id == _master_agent_id(agent.owner_id):
            raise InvalidInputError("The Master Agent belongs to its owner and cannot be reassigned.")
        if assignee_id == agent.owner_id:
            raise ConflictError("This agent already belongs to that member.")
        await require_member(db, scope_id, assignee_id)


async def execute_agent_control(
    db, user, scope_id: UUID, agent_id: UUID, command: str, approval_id: UUID,
    *, assignee_id: UUID | None = None, ctx=None,
):
    """Consume the exact-action approval, then apply the agent control."""
    _validate_agent_command(command, assignee_id)
    await require_scope(db, user, scope_id, manage=True)
    from app.services.audit import record

    agent = await _agent_in_scope(db, scope_id, agent_id)
    await _check_agent_command(db, scope_id, agent, command, assignee_id)
    await consume_approval(
        db, approval_id,
        agent_control_spec(
            await _platform_scope_id(db, scope_id), agent_id, command, assignee_id
        ),
    )
    now = datetime.now(UTC)
    if command in {"pause", "resume"}:
        agent.paused = command == "pause"
    else:  # assign: transfer ownership to another member of the scope
        previous_owner = agent.owner_id
        agent.owner_id = assignee_id
    agent.version += 1
    agent.updated_at = now
    await db.flush()
    if ctx is not None:
        details = {"command": command, "approval_id": str(approval_id)}
        if command == "assign":
            details["from"] = str(previous_owner)
            details["to"] = str(assignee_id)
        record(db, ctx, f"control_room.agent_{command}d", target=str(agent.id), details=details)
    return agent
