"""Per-agent controls evaluator (Step AK, slice 1).

Pure functions, stdlib only: no I/O, no database, no network. Given one agent's
control record and one requested action, decide allow / deny / needs-approval.

Rules enforced here (see docs/step-ak-agent-controls-plan.md):
- Deny by default: anything not explicitly listed is denied. No wildcard
  permission ("*") or wildcard action for agents — ever.
- Kill-switch is terminal: a killed agent is always denied, whatever else the
  record says.
- Spend is money: Decimal, never float. A request that would take spend over
  the limit is denied before it runs; near the limit (>= alert threshold) the
  decision stays "allow" but carries a spend_near_limit alert.
- Sensitive actions and high/critical risk always need an independent human
  approval, even when the agent is otherwise allowed to ask for them.
- Delegation is bounded: depth limit, delegate must be alive, no self-delegation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
import uuid

__all__ = [
    "AgentControl",
    "ActionRequest",
    "ControlDecision",
    "ControlEvent",
    "SENSITIVE_ACTIONS",
    "STATUSES",
    "RISKS",
    "evaluate",
    "spend_status",
    "kill_switch",
    "check_delegation",
    "validate_control",
]

STATUSES = ("active", "paused", "disabled", "killed")
RISKS = ("low", "medium", "high", "critical")
AGENT_KINDS = ("studio", "profile", "governance")

# Actions that always need an independent human approval, even for an agent
# that is otherwise allowed to request them.
SENSITIVE_ACTIONS = frozenset({
    "funds.transfer",
    "payment.approve",
    "document.share",
    "agent.shutdown",
    "agent.kill",
    "permission.grant",
    "user.revoke",
    "data.delete",
    "secret.rotate",
})

DEFAULT_ALERT_THRESHOLD = Decimal("0.80")
MIN_STEPS = 1
MAX_STEPS = 8


@dataclass(frozen=True)
class AgentControl:
    """One agent's control record. Immutable: changes produce a new record."""

    agent_id: uuid.UUID
    agent_kind: str = "studio"
    owner_id: uuid.UUID | None = None
    status: str = "active"
    data_scopes: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    allowed_actions: frozenset[str] = frozenset()
    spend_limit_usd: Decimal | None = None
    spend_used_usd: Decimal = Decimal("0")
    alert_threshold: Decimal = DEFAULT_ALERT_THRESHOLD
    max_steps: int = 6
    delegate_to: frozenset[uuid.UUID] = frozenset()
    max_delegation_depth: int = 1
    killed_at: datetime | None = None
    killed_by: uuid.UUID | None = None
    kill_reason: str | None = None

    @property
    def is_alive(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class ActionRequest:
    """One action an agent wants to take."""

    action: str
    scope: str
    permission: str | None = None
    estimated_cost_usd: Decimal = Decimal("0")
    risk: str = "low"
    subject: str | None = None


@dataclass(frozen=True)
class ControlDecision:
    """The evaluator's answer. `alerts` never change the outcome; they warn."""

    outcome: Literal["allow", "deny", "needs_approval"]
    reason: str
    alerts: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ControlEvent:
    """An audit-shaped record of a control change (kill-switch included)."""

    event: str
    agent_id: uuid.UUID
    actor_id: uuid.UUID | None
    reason: str
    at: datetime
    before: dict
    after: dict


def _money(value: Decimal | int | str, what: str) -> Decimal:
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - any unparsable value is invalid input
        raise ValueError(f"{what} must be a decimal amount.") from exc
    if amount.is_nan() or amount.is_infinite():
        raise ValueError(f"{what} must be a finite amount.")
    return amount


def validate_control(control: AgentControl) -> AgentControl:
    """Reject a control record that could never be safely enforced.

    Returns the record unchanged when it is valid. An agent may hold an empty
    allow-list (it may then request nothing) but never a wildcard.
    """
    if control.agent_kind not in AGENT_KINDS:
        raise ValueError(f"Unknown agent kind: {control.agent_kind!r}.")
    if control.status not in STATUSES:
        raise ValueError(f"Unknown agent status: {control.status!r}.")
    if "*" in control.permissions:
        raise ValueError("Agents never hold the wildcard permission.")
    if "*" in control.allowed_actions:
        raise ValueError("Agents never hold a wildcard action.")
    if any(not p or not str(p).strip() for p in control.permissions):
        raise ValueError("Permissions must be non-empty strings.")
    if any(not a or not str(a).strip() for a in control.allowed_actions):
        raise ValueError("Allowed actions must be non-empty strings.")
    if any(not s or not str(s).strip() for s in control.data_scopes):
        raise ValueError("Data scopes must be non-empty strings.")
    used = _money(control.spend_used_usd, "spend_used_usd")
    if used < 0:
        raise ValueError("spend_used_usd cannot be negative.")
    if control.spend_limit_usd is not None:
        limit = _money(control.spend_limit_usd, "spend_limit_usd")
        if limit < 0:
            raise ValueError("spend_limit_usd cannot be negative.")
        if used > limit:
            raise ValueError("spend_used_usd cannot exceed spend_limit_usd.")
    threshold = _money(control.alert_threshold, "alert_threshold")
    if threshold <= 0 or threshold > 1:
        raise ValueError("alert_threshold must be in (0, 1].")
    if not MIN_STEPS <= control.max_steps <= MAX_STEPS:
        raise ValueError(f"max_steps must be between {MIN_STEPS} and {MAX_STEPS}.")
    if control.max_delegation_depth < 0:
        raise ValueError("max_delegation_depth cannot be negative.")
    if control.agent_id in control.delegate_to:
        raise ValueError("An agent cannot delegate to itself.")
    if control.status == "killed" and control.killed_at is None:
        raise ValueError("A killed agent must record when it was killed.")
    if control.status != "killed" and (control.killed_at is not None or control.kill_reason is not None):
        raise ValueError("Only a killed agent records kill details.")
    return control


def spend_status(control: AgentControl) -> dict:
    """Spend state of a control record: ok | near_limit | at_limit | over_limit.

    An agent with no limit is always ok. `remaining` is None without a limit.
    """
    validate_control(control)
    if control.spend_limit_usd is None:
        return {"state": "ok", "used": control.spend_used_usd, "limit": None,
                "remaining": None, "percent_used": None}
    limit = _money(control.spend_limit_usd, "spend_limit_usd")
    used = _money(control.spend_used_usd, "spend_used_usd")
    if limit == 0:
        state = "at_limit" if used == 0 else "over_limit"
        return {"state": state, "used": used, "limit": limit, "remaining": Decimal("0"),
                "percent_used": None}
    percent = used / limit
    if used >= limit:
        state = "at_limit"
    elif percent >= control.alert_threshold:
        state = "near_limit"
    else:
        state = "ok"
    return {"state": state, "used": used, "limit": limit, "remaining": limit - used,
            "percent_used": percent}


def evaluate(control: AgentControl, request: ActionRequest) -> ControlDecision:
    """Decide one requested action. Checks run in precedence order; the first
    failing check decides, so a killed agent is denied before anything else is
    even looked at."""
    validate_control(control)
    if request.risk not in RISKS:
        raise ValueError(f"Unknown risk: {request.risk!r}.")
    cost = _money(request.estimated_cost_usd, "estimated_cost_usd")
    if cost < 0:
        raise ValueError("estimated_cost_usd cannot be negative.")
    if not request.action or not str(request.action).strip():
        raise ValueError("An action name is required.")
    if not request.scope or not str(request.scope).strip():
        raise ValueError("A data scope is required.")

    # 1. Liveness — the kill-switch is terminal.
    if control.status == "killed":
        return ControlDecision("deny", "agent_killed",
                               details={"agent_id": str(control.agent_id)})
    if control.status == "disabled":
        return ControlDecision("deny", "agent_disabled",
                               details={"agent_id": str(control.agent_id)})
    if control.status == "paused":
        return ControlDecision("deny", "agent_paused",
                               details={"agent_id": str(control.agent_id)})

    # 2. Allow-list: the action must be explicitly listed.
    if request.action not in control.allowed_actions:
        return ControlDecision("deny", "action_not_allowed",
                               details={"action": request.action})

    # 3. Data scope: the agent may only touch data in its scopes.
    if request.scope not in control.data_scopes:
        return ControlDecision("deny", "scope_not_covered",
                               details={"scope": request.scope})

    # 4. Permission: the agent must hold the permission the action needs.
    if request.permission is not None and request.permission not in control.permissions:
        return ControlDecision("deny", "permission_missing",
                               details={"permission": request.permission})

    # 5. Spend: deny before running when the estimate would break the limit.
    alerts: list[str] = []
    if control.spend_limit_usd is not None:
        limit = _money(control.spend_limit_usd, "spend_limit_usd")
        used = _money(control.spend_used_usd, "spend_used_usd")
        if used + cost > limit:
            return ControlDecision("deny", "spend_limit_exceeded", details={
                "used": str(used), "estimated_cost": str(cost), "limit": str(limit)})
        if limit > 0 and (used + cost) / limit >= control.alert_threshold:
            alerts.append("spend_near_limit")

    # 6. Approval gates: sensitive actions and high/critical risk always need
    #    an independent human approval, even when otherwise allowed.
    if request.action in SENSITIVE_ACTIONS:
        return ControlDecision("needs_approval", "sensitive_action",
                               tuple(alerts), details={"action": request.action})
    if request.risk in ("high", "critical"):
        return ControlDecision("needs_approval", "approval_required",
                               tuple(alerts), details={"risk": request.risk})

    return ControlDecision("allow", "granted", tuple(alerts),
                           details={"action": request.action, "scope": request.scope})


def kill_switch(control: AgentControl, *, actor_id: uuid.UUID | None, reason: str,
                at: datetime | None = None) -> tuple[AgentControl, ControlEvent]:
    """Kill an agent immediately. Returns the killed record plus its audit event.

    The kill is terminal: `evaluate()` denies every later request with
    `agent_killed`. Killing an already-killed agent is a no-op that still
    records the attempt as an event. Only an administrator creating a new
    control record can bring an agent back; this function never revives one.
    """
    validate_control(control)
    clean_reason = " ".join(str(reason or "").split())[:300]
    if not clean_reason:
        raise ValueError("A kill reason is required.")
    moment = at or datetime.now(UTC)
    before = {"status": control.status, "killed_at": None, "kill_reason": None}
    if control.status == "killed":
        event = ControlEvent("agent.kill_repeat", control.agent_id, actor_id, clean_reason,
                             moment, before, {"status": "killed", "killed_at": control.killed_at,
                                              "kill_reason": control.kill_reason})
        return control, event
    killed = AgentControl(
        agent_id=control.agent_id, agent_kind=control.agent_kind, owner_id=control.owner_id,
        status="killed", data_scopes=control.data_scopes, permissions=control.permissions,
        allowed_actions=control.allowed_actions, spend_limit_usd=control.spend_limit_usd,
        spend_used_usd=control.spend_used_usd, alert_threshold=control.alert_threshold,
        max_steps=control.max_steps, delegate_to=control.delegate_to,
        max_delegation_depth=control.max_delegation_depth,
        killed_at=moment, killed_by=actor_id, kill_reason=clean_reason,
    )
    event = ControlEvent("agent.killed", control.agent_id, actor_id, clean_reason, moment,
                         before, {"status": "killed", "killed_at": moment,
                                  "kill_reason": clean_reason})
    return killed, event


def check_delegation(control: AgentControl, delegate_id: uuid.UUID, *, depth: int = 1,
                     delegate_alive: bool = True) -> ControlDecision:
    """May `control` delegate to `delegate_id` at this depth?

    Delegation is bounded: the delegate must be listed, the depth must be
    within the record's limit, the delegate must itself be alive, and a dead
    agent delegates nothing. Self-delegation is rejected by validation.
    """
    validate_control(control)
    if depth < 1:
        raise ValueError("Delegation depth starts at 1.")
    if control.status == "killed":
        return ControlDecision("deny", "agent_killed",
                               details={"agent_id": str(control.agent_id)})
    if control.status in ("disabled", "paused"):
        return ControlDecision("deny", f"agent_{control.status}",
                               details={"agent_id": str(control.agent_id)})
    if delegate_id not in control.delegate_to:
        return ControlDecision("deny", "delegate_not_allowed",
                               details={"delegate_id": str(delegate_id)})
    if depth > control.max_delegation_depth:
        return ControlDecision("deny", "delegation_depth_exceeded",
                               details={"depth": depth,
                                        "max_depth": control.max_delegation_depth})
    if not delegate_alive:
        return ControlDecision("deny", "delegate_not_alive",
                               details={"delegate_id": str(delegate_id)})
    return ControlDecision("allow", "granted",
                           details={"delegate_id": str(delegate_id), "depth": depth})
