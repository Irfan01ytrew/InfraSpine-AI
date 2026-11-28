"""Step AK (slice 3): enforcement wiring for per-agent controls.

The gate is the one place that answers "(agent, requested action) -> allow /
deny / needs-approval" at the moment of the request, using the slice-1
evaluator against the stored control record. It is called by the platform, not
by the agent: an agent's own text can never widen what it is allowed to do.

Wiring points:
- `app.platform.access.resolve_principal` / `_principal_current`: a control
  record in a non-active status refuses the agent everywhere at once.
- `app.agent_studio.worker.claim_agent_studio_run`: the claim query excludes
  runs whose agent has a non-active control record, so a stopped agent's queued
  runs are never dispatched.
- `app.services.agent_action_runtime.execute_proposal`: a governance proposal
  from a controlled-stopped profile agent is refused before execution.
- `gate_agent_action` / `require_agent_action`: the runtime check for tool and
  action dispatch sites that know the action name, data scope, and cost.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_controls.evaluator import (
    ActionRequest,
    ControlDecision,
    evaluate,
)
from app.agent_controls.store import get_control_record, record_to_control
from app.core.errors import PermissionDeniedError
from app.db.agent_controls_models import AgentControlEvent
from app.platform.access import Principal
from app.platform.events import audit

__all__ = [
    "control_status_for",
    "gate_agent_liveness",
    "gate_agent_action",
    "require_agent_action",
]


async def control_status_for(db: AsyncSession, agent_id: uuid.UUID | None) -> str | None:
    """The stored control status for an agent, or None when it has no record.

    Agents without a control record keep their previous behaviour (slice 5
    unification backfills records); a record always wins over the legacy
    `enabled` / `paused` flags.
    """
    if agent_id is None:
        return None
    row = await get_control_record(db, agent_id)
    return row.status if row is not None else None


async def gate_agent_liveness(db: AsyncSession, agent_id: uuid.UUID | None) -> ControlDecision:
    """Allow unless a control record says the agent is not alive.

    Used where the call site knows the agent but not a specific action (the
    governance proposal path, whose proposals carry a profile agent id but no
    data scope). The kill-switch is terminal here too.
    """
    status = await control_status_for(db, agent_id)
    if status is None or status == "active":
        return ControlDecision("allow", "granted", details={"agent_id": str(agent_id) if agent_id else None})
    return ControlDecision("deny", f"agent_{status}", details={"agent_id": str(agent_id)})


async def _audit_decision(db: AsyncSession, principal: Principal, agent_id: uuid.UUID,
                          decision: ControlDecision, request: ActionRequest) -> None:
    """Denied and approval-gated decisions are audit events; allows are not
    (they would drown the trail). The event is written in the caller's
    transaction, so a denied dispatch leaves its trace behind."""
    if decision.outcome == "allow":
        return
    db.add(AgentControlEvent(
        agent_id=agent_id,
        event="agent.action_denied" if decision.outcome == "deny" else "agent.action_needs_approval",
        actor_id=principal.user_id,
        reason=decision.reason,
        before={"action": request.action, "scope": request.scope,
                "permission": request.permission, "risk": request.risk,
                "estimated_cost_usd": str(request.estimated_cost_usd)},
        after={"outcome": decision.outcome, "alerts": list(decision.alerts),
               "details": {k: str(v) for k, v in decision.details.items()}},
    ))
    audit(db, principal, "agent.action_denied" if decision.outcome == "deny"
          else "agent.action_needs_approval", target=str(agent_id),
          details={"action": request.action, "reason": decision.reason})


async def gate_agent_action(db: AsyncSession, principal: Principal, *, action: str, scope: str,
                            permission: str | None = None,
                            estimated_cost_usd: Decimal = Decimal("0"), risk: str = "low",
                            subject: str | None = None) -> ControlDecision:
    """Decide one action an agent wants to take, against its stored record.

    Non-agent principals are not constrained by agent controls (their own
    platform permissions already apply) and agents without a control record
    keep their previous behaviour. Every deny / needs-approval is audited.
    """
    request = ActionRequest(action=action, scope=scope, permission=permission,
                            estimated_cost_usd=estimated_cost_usd, risk=risk, subject=subject)
    if principal.kind != "agent" or principal.agent_id is None:
        return ControlDecision("allow", "not_agent", details={"kind": principal.kind})
    row = await get_control_record(db, principal.agent_id)
    if row is None:
        return ControlDecision("allow", "no_control_record",
                               details={"agent_id": str(principal.agent_id)})
    decision = evaluate(record_to_control(row), request)
    await _audit_decision(db, principal, principal.agent_id, decision, request)
    return decision


async def require_agent_action(db: AsyncSession, principal: Principal, **kwargs) -> ControlDecision:
    """Like `gate_agent_action`, but a denial raises instead of returning.

    A `needs_approval` decision is returned (not raised): the caller must
    obtain an independent human approval before dispatching, and must never
    dispatch on a `deny`.
    """
    decision = await gate_agent_action(db, principal, **kwargs)
    if decision.outcome == "deny":
        raise PermissionDeniedError(
            f"This agent is not allowed to take this action ({decision.reason}).")
    return decision
