"""Step J (AI Workforce OS), slice 1: deterministic workforce assignment.

Pure functions (stdlib only). No database, no routes, no production code
changed. This module decides *which* workforce agent should do a task; it
never executes anything and never grants anything.

The decision uses structured fields only:

* agent status, allowed scopes, allowed actions, skills, current load,
  capacity, remaining spend, per-task cost cap, sensitive-handling flag;
* task required skills, scope, action, estimated cost, sensitivity.

The task ``description`` is untrusted free text. It is accepted as an opaque
label and **never read for matching**: injection text in it ("ignore policy",
"approve all invoices", "grant admin", a changed payee, ...) cannot change
the decision, because no code path here looks at it.

Money is ``Decimal`` only, never float. A missing spend limit
(``spend_remaining_usd is None``) means "no limit set", not "free" and not
"zero": the agent is eligible on spend, and the missing limit is visible to
callers through the roster, not silently treated as unlimited money.

Statuses: only ``"active"`` is eligible. ``paused`` / ``disabled`` /
``killed`` / ``retired`` (and any unknown status string) are rejected with a
reason, never an exception.

Ranking among eligible agents is deterministic: lowest current load first
(load balancing), then most extra skills beyond the required ones
(versatility), then smallest agent id as a final tie-break. Shuffling the
roster order never changes the winner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable
import uuid

__all__ = [
    "ACTIVE",
    "Assignment",
    "Rejection",
    "WorkTask",
    "WorkforceAgent",
    "assign_task",
    "skill_match",
    "validate_roster",
]

ACTIVE = "active"
_KNOWN_STATUSES = frozenset({"active", "paused", "disabled", "killed", "retired"})
_SENSITIVITIES = frozenset({"standard", "sensitive"})


def _norm_skill(value: object) -> str:
    """One skill token, normalized for matching: stripped + lowercased."""
    return str(value).strip().lower()


def _norm_skills(values: object) -> frozenset[str]:
    """Normalize a skill collection; empty/blank tokens are dropped."""
    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes)):
        values = [values]
    out: set[str] = set()
    for raw in values:  # type: ignore[union-attr]
        token = _norm_skill(raw)
        if token:
            out.add(token)
    return frozenset(out)


def _norm_action(value: object) -> str:
    """Platform actions are lowercase dotted names; match on that form."""
    return str(value).strip().lower()


def _norm_scope(value: object) -> str:
    """Scope ids match exactly after stripping (case is significant)."""
    return str(value).strip()


def _money(value: object) -> Decimal | None:
    """Coerce to Decimal, or None when the value is not a number.

    ``None`` input means "no value set" (callers decide what that means);
    a non-numeric value is also None — the caller treats it as invalid.
    Booleans are not money.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def skill_match(required: object, have: object) -> tuple[frozenset[str], frozenset[str]]:
    """Split skills into (missing, extra).

    ``missing`` = required skills the agent does not have (empty = qualified).
    ``extra`` = skills the agent has beyond the required ones.
    Matching is case-insensitive; blank tokens are ignored.
    """
    req = _norm_skills(required)
    got = _norm_skills(have)
    return req - got, got - req


@dataclass(frozen=True)
class WorkforceAgent:
    """One workforce agent as the assignment decision sees it.

    This is a *view* of a registry entry, not a database row: slices 2–3 will
    build it from Studio / governance / legacy records (via the AK control
    record where one exists). Fields that are unknown are left empty and
    deny by default — never guessed.
    """

    agent_id: uuid.UUID
    owner_id: uuid.UUID
    skills: frozenset[str] = frozenset()
    status: str = ACTIVE
    max_concurrent: int = 1
    current_load: int = 0
    spend_remaining_usd: Decimal | None = None
    allowed_scopes: frozenset[str] = frozenset()
    allowed_actions: frozenset[str] = frozenset()
    max_cost_per_task_usd: Decimal | None = None
    handles_sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "skills", _norm_skills(self.skills))
        object.__setattr__(self, "status", str(self.status).strip().lower())
        object.__setattr__(
            self, "allowed_scopes", frozenset(_norm_scope(s) for s in (self.allowed_scopes or ()))
        )
        object.__setattr__(
            self, "allowed_actions", frozenset(_norm_action(a) for a in (self.allowed_actions or ()))
        )


@dataclass(frozen=True)
class WorkTask:
    """One unit of work to assign. ``description`` is opaque untrusted text."""

    task_id: str
    required_skills: frozenset[str] = frozenset()
    scope_id: str = ""
    action: str = ""
    estimated_cost_usd: Decimal = Decimal("0")
    sensitivity: str = "standard"
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_skills", _norm_skills(self.required_skills))
        object.__setattr__(self, "scope_id", _norm_scope(self.scope_id))
        object.__setattr__(self, "action", _norm_action(self.action))
        object.__setattr__(self, "sensitivity", str(self.sensitivity).strip().lower())
        cost = _money(self.estimated_cost_usd)
        object.__setattr__(self, "estimated_cost_usd", cost if cost is not None else Decimal("-1"))


@dataclass(frozen=True)
class Rejection:
    """Why one roster agent was not assigned the task."""

    agent_id: uuid.UUID
    reason: str


@dataclass(frozen=True)
class Assignment:
    """The assignment decision. ``status`` is ``"assigned"`` or ``"refused"``.

    ``rejected`` lists every roster agent that was considered and why it was
    not chosen, so the decision is auditable without re-running it.
    ``score`` is the winner's ranking score (extra skills minus current load),
    or None when nothing was assigned.
    """

    status: str
    agent_id: uuid.UUID | None
    reason: str
    score: float | None = None
    rejected: tuple[Rejection, ...] = ()


def validate_roster(agents: Iterable[WorkforceAgent]) -> tuple[str, ...]:
    """Structural problems in a roster, as human-readable strings.

    An empty tuple means the roster is structurally sound. This never raises:
    unparseable entries are reported, not thrown.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for index, agent in enumerate(agents):
        label = f"roster[{index}]"
        try:
            agent_id = str(agent.agent_id)
        except Exception:
            problems.append(f"{label}: missing or invalid agent_id")
            continue
        if agent_id in seen:
            problems.append(f"{label}: duplicate agent_id {agent_id}")
        seen.add(agent_id)
        if agent.status not in _KNOWN_STATUSES:
            problems.append(f"{label} ({agent_id}): unknown status {agent.status!r}")
        if not isinstance(agent.max_concurrent, int) or isinstance(agent.max_concurrent, bool) \
                or agent.max_concurrent < 1:
            problems.append(f"{label} ({agent_id}): max_concurrent must be an int >= 1")
        if not isinstance(agent.current_load, int) or isinstance(agent.current_load, bool) \
                or agent.current_load < 0:
            problems.append(f"{label} ({agent_id}): current_load must be an int >= 0")
        for name in ("spend_remaining_usd", "max_cost_per_task_usd"):
            value = getattr(agent, name, None)
            if value is not None and _money(value) is None:
                problems.append(f"{label} ({agent_id}): {name} is not a number")
    return tuple(problems)


def _eligibility(agent: WorkforceAgent, task: WorkTask) -> tuple[str | None, int]:
    """Return (rejection reason, extra-skill count) for one agent.

    ``None`` reason means eligible. Checks run cheapest-first; the first
    failing check is the recorded reason.
    """
    if agent.status != ACTIVE:
        if agent.status in _KNOWN_STATUSES:
            return f"not_active:{agent.status}", 0
        return f"unknown_status:{agent.status}", 0
    if not isinstance(agent.max_concurrent, int) or isinstance(agent.max_concurrent, bool) \
            or agent.max_concurrent < 1:
        return "invalid_capacity", 0
    if not isinstance(agent.current_load, int) or isinstance(agent.current_load, bool) \
            or agent.current_load < 0:
        return "invalid_load", 0
    if task.scope_id and task.scope_id not in agent.allowed_scopes:
        return "scope_not_allowed", 0
    if task.action and task.action not in agent.allowed_actions:
        return "action_not_allowed", 0
    missing, extra = skill_match(task.required_skills, agent.skills)
    if missing:
        return "missing_skills:" + ",".join(sorted(missing)), 0
    if task.sensitivity == "sensitive" and not agent.handles_sensitive:
        return "sensitive_not_permitted", 0
    if agent.current_load >= agent.max_concurrent:
        return "at_capacity", 0
    spend = _money(agent.spend_remaining_usd)
    if agent.spend_remaining_usd is not None and spend is None:
        return "invalid_spend_remaining", 0
    if spend is not None and spend < task.estimated_cost_usd:
        return "insufficient_spend", 0
    cap = _money(agent.max_cost_per_task_usd)
    if agent.max_cost_per_task_usd is not None and cap is None:
        return "invalid_task_cost_cap", 0
    if cap is not None and task.estimated_cost_usd > cap:
        return "over_task_cost_cap", 0
    return None, len(extra)


def assign_task(agents: Iterable[WorkforceAgent], task: WorkTask) -> Assignment:
    """Assign ``task`` to the best eligible roster agent, deterministically.

    Ranking among eligible agents: lowest current load, then most extra
    skills, then smallest agent id. The task description is never read.
    Never raises on bad roster/task data — bad data becomes a refusal with
    a reason.
    """
    try:
        roster = list(agents)
    except TypeError:
        return Assignment(status="refused", agent_id=None, reason="invalid_roster")
    if not roster:
        return Assignment(status="refused", agent_id=None, reason="no_agents")
    if task.estimated_cost_usd < 0:
        return Assignment(status="refused", agent_id=None, reason="invalid_task:negative_cost")
    if task.sensitivity not in _SENSITIVITIES:
        return Assignment(
            status="refused", agent_id=None,
            reason=f"invalid_task:unknown_sensitivity:{task.sensitivity}",
        )

    rejected: list[Rejection] = []
    eligible: list[tuple[tuple[int, int, str], WorkforceAgent, int]] = []
    for agent in roster:
        try:
            reason, extra = _eligibility(agent, task)
        except Exception:
            rejected.append(Rejection(agent_id=_safe_id(agent), reason="invalid_agent_record"))
            continue
        if reason is not None:
            rejected.append(Rejection(agent_id=_safe_id(agent), reason=reason))
            continue
        # Ranking key: (load asc, -extra skills, agent id asc).
        key = (agent.current_load, -extra, str(agent.agent_id))
        eligible.append((key, agent, extra))

    if not eligible:
        return Assignment(
            status="refused", agent_id=None,
            reason="no_eligible_agent", rejected=tuple(rejected),
        )
    eligible.sort(key=lambda item: item[0])
    _, winner, extra = eligible[0]
    return Assignment(
        status="assigned",
        agent_id=winner.agent_id,
        reason="assigned",
        score=float(extra - winner.current_load),
        rejected=tuple(rejected),
    )


def _safe_id(agent: object) -> uuid.UUID:
    """Best-effort agent id for a rejection record; never raises."""
    try:
        return agent.agent_id  # type: ignore[attr-defined]
    except Exception:
        return uuid.UUID(int=0)
