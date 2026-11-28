"""Per-agent controls (Step AK): data access, permissions, spending limits,
allowed actions, and the kill-switch. Enforced by the platform, never by the
agent itself.
"""

from app.agent_controls.evaluator import (
    SENSITIVE_ACTIONS,
    ActionRequest,
    AgentControl,
    ControlDecision,
    ControlEvent,
    check_delegation,
    evaluate,
    kill_switch,
    spend_status,
    validate_control,
)
from app.agent_controls.gate import control_status_for, gate_agent_action, gate_agent_liveness, require_agent_action
from app.agent_controls.store import (
    create_control,
    get_control_record,
    kill_control,
    list_control_records,
    record_spend,
    record_spend_once,
    update_control,
)

__all__ = [
    "SENSITIVE_ACTIONS",
    "ActionRequest",
    "AgentControl",
    "ControlDecision",
    "ControlEvent",
    "check_delegation",
    "control_status_for",
    "create_control",
    "evaluate",
    "gate_agent_action",
    "gate_agent_liveness",
    "get_control_record",
    "kill_control",
    "kill_switch",
    "list_control_records",
    "record_spend",
    "record_spend_once",
    "require_agent_action",
    "spend_status",
    "update_control",
    "validate_control",
]
