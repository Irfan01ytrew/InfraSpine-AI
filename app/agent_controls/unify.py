"""Step AK (slice 5): unification adapters for the two legacy control islands.

The per-agent control record (slice 2) is authoritative. But two older
islands still carry their own limits and have no control record yet:

* Agent Studio agents (`agent_studio_agents`): `tool_permissions`,
  `approval_tools`, `delegate_agent_ids`, `enabled` / `paused`, `max_steps`,
  `rag_enabled`.
* Governance agents (`security_agents` / `AgentIdentity`): `tools`,
  `enabled`, and a call-window budget (`max_calls` / `calls_used` /
  `window_seconds`).

These adapters build a *read-only view* of a legacy agent as an
`AgentControl`, so the slice-1 evaluator can evaluate it and the admin UI
can report on it. They grant nothing and change nothing:

* A real control record always wins (`effective_control`).
* Anything the legacy island does not express is left empty (deny by
  default) and listed in the view's `notes` — never guessed.
* A call-window budget is **not** money: `max_calls` / `calls_used` are
  never mapped onto `spend_limit_usd` / `spend_used_usd`. The note records
  the window so an administrator can set a real spend limit.
* Studio tool names are Studio-island names, not platform action names;
  the view lists them as `allowed_actions` only so the evaluator can see
  what the agent was already allowed to do in its own island.

Writing a real control record from a legacy agent (migration) is a
merge-time decision for the owner — these adapters deliberately do not
write to the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.agent_controls.evaluator import DEFAULT_ALERT_THRESHOLD, AgentControl, validate_control


@dataclass(frozen=True)
class LegacyControlView:
    """A legacy agent seen as a control record, plus what could not be mapped."""

    control: AgentControl
    notes: tuple[str, ...] = ()


def _uuids(values: object) -> tuple[frozenset[uuid.UUID], tuple[str, ...]]:
    """Parse a JSONB id list, dropping self-references and unparseable entries."""
    parsed: set[uuid.UUID] = set()
    dropped = 0
    for raw in values or []:
        try:
            parsed.add(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            dropped += 1
    notes: list[str] = []
    if dropped:
        notes.append(f"{dropped} delegate id(s) could not be parsed and were dropped.")
    return frozenset(parsed), tuple(notes)


def _strings(values: object) -> frozenset[str]:
    return frozenset(str(item) for item in (values or []) if str(item).strip())


def control_from_studio(agent: object) -> LegacyControlView:
    """View an Agent Studio agent (`agent_studio_agents` row) as a control record."""
    agent_id = uuid.UUID(str(getattr(agent, "id")))
    raw_delegates = getattr(agent, "delegate_agent_ids", None)
    parsed_delegates, delegate_notes = _uuids(raw_delegates)
    delegate_notes = list(delegate_notes)
    if agent_id in parsed_delegates:
        delegate_notes.append("Self-delegation was dropped.")
    delegate_to = frozenset(d for d in parsed_delegates if d != agent_id)
    enabled = bool(getattr(agent, "enabled", True))
    paused = bool(getattr(agent, "paused", False))
    status = "disabled" if not enabled else "paused" if paused else "active"
    max_steps = getattr(agent, "max_steps", 6)
    try:
        max_steps = int(max_steps)
    except (TypeError, ValueError):
        max_steps = 6
    clamped = max(1, min(8, max_steps))
    notes = [
        "Studio has no platform-permission list; permissions are empty (deny by default).",
        "Studio has no data-scope list; data scopes are empty even when rag_enabled.",
    ]
    if getattr(agent, "rag_enabled", False):
        notes.append("rag_enabled is set, but it grants no data scope on its own — create a control record to scope reads.")
    if clamped != max_steps:
        notes.append(f"max_steps {max_steps} is outside 1–8 and was clamped to {clamped}.")
    notes.extend(delegate_notes)
    control = validate_control(AgentControl(
        agent_id=agent_id,
        agent_kind="studio",
        owner_id=getattr(agent, "owner_id", None),
        status=status,
        data_scopes=frozenset(),
        permissions=frozenset(),
        allowed_actions=_strings(getattr(agent, "tool_permissions", None)),
        spend_limit_usd=None,
        spend_used_usd=Decimal("0"),
        alert_threshold=DEFAULT_ALERT_THRESHOLD,
        max_steps=clamped,
        delegate_to=delegate_to,
        max_delegation_depth=1,
    ))
    return LegacyControlView(control=control, notes=tuple(notes))


def control_from_governance(identity: object) -> LegacyControlView:
    """View a governance agent (`security_agents` / AgentIdentity row) as a control record."""
    agent_id = uuid.UUID(str(getattr(identity, "id")))
    enabled = bool(getattr(identity, "enabled", True))
    max_calls = getattr(identity, "max_calls", None)
    calls_used = getattr(identity, "calls_used", None)
    window_seconds = getattr(identity, "window_seconds", None)
    notes = [
        "Governance has no platform-permission or data-scope list; both are empty (deny by default).",
        "Governance has no step limit; max_steps defaults to 6.",
        f"Call-window budget ({calls_used}/{max_calls} calls in {window_seconds}s) is not money and was not mapped to a spend limit.",
    ]
    control = validate_control(AgentControl(
        agent_id=agent_id,
        agent_kind="governance",
        owner_id=getattr(identity, "owner_user_id", None),
        status="disabled" if not enabled else "active",
        data_scopes=frozenset(),
        permissions=frozenset(),
        allowed_actions=_strings(getattr(identity, "tools", None)),
        spend_limit_usd=None,
        spend_used_usd=Decimal("0"),
        alert_threshold=DEFAULT_ALERT_THRESHOLD,
        max_steps=6,
        delegate_to=frozenset(),
        max_delegation_depth=1,
    ))
    return LegacyControlView(control=control, notes=tuple(notes))


def effective_control(record: AgentControl | None,
                      *legacy: LegacyControlView | None) -> LegacyControlView | None:
    """The control that governs an agent: its real record when one exists,
    otherwise the first legacy view. A record always wins over legacy data."""
    if record is not None:
        return LegacyControlView(control=record, notes=("Authoritative control record.",))
    for view in legacy:
        if view is not None:
            return view
    return None
