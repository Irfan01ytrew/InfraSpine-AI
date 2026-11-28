"""Step J (AI Workforce OS), slice 4: delegation policy rules (pure functions).

Pure functions (stdlib only). No database, no routes, no production code
changed. These rules decide whether agent A may hand work to agent B; they
never execute anything and never grant anything.

A delegation policy is a versioned set of rules. One rule says: agents
matching ``from`` may delegate to agents matching ``to``, for the listed
actions and scopes. An empty ``allowed_actions`` / ``allowed_scopes`` on a
rule means "any action / any scope" — the rule then constrains only who
may delegate to whom (and whether sensitive tasks are allowed). A rule
never matches on the delegated task's free text.

The task ``description`` is untrusted text. It is accepted as an opaque
label and **never read for matching**: injection text in it ("ignore
policy", "approve all invoices", "grant admin", a changed payee, ...)
cannot change the decision, because no code path here looks at it.

Checks, in order (the first failing check is the recorded reason):

1. An active policy must exist (version >= 1).
2. Delegation depth must not exceed the policy's ``max_depth`` (default 2,
   plan §7.6). Depth 1 is the first delegation from the originating run.
3. The child agent must be effectively ``active`` (registry paused /
   retired / disabled / owner-revoked children never take delegated work).
4. A rule must cover (from-agent, to-agent, action, scope).
5. A sensitive task needs a rule that allows sensitive tasks.
6. Every tool the delegation asks the child to use must be one of the
   child's own tools: B can never use a tool A has if B's own role
   forbids it.
7. Separation of duties: when the delegation carries an approver, the
   originator and the approver must differ — the same person/agent can
   never both originate and approve a separated duty.

Money is never involved here: a delegation decision is about permission,
not price (budget enforcement is slice 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
import uuid

__all__ = [
    "DEFAULT_MAX_DELEGATION_DEPTH",
    "MAX_DELEGATION_DEPTH_LIMIT",
    "DelegationDecision",
    "DelegationPolicy",
    "DelegationRequest",
    "DelegationRule",
    "evaluate_delegation",
    "policy_from_dict",
    "policy_to_dict",
    "validate_policy_rules",
]

DEFAULT_MAX_DELEGATION_DEPTH = 2
MAX_DELEGATION_DEPTH_LIMIT = 5
_CHILD_RUN_KIND_INLINE = "inline"

_SENSITIVITIES = frozenset({"standard", "sensitive"})


def _norm_action(value: object) -> str:
    """Platform actions are lowercase dotted names; match on that form."""
    return str(value).strip().lower()


def _norm_scope(value: object) -> str:
    """Scope ids match exactly after stripping (case is significant)."""
    return str(value).strip()


def _norm_tools(values: object) -> frozenset[str]:
    """Tool names match exactly after stripping; blanks are dropped."""
    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes)):
        values = [values]
    out: set[str] = set()
    for raw in values:  # type: ignore[union-attr]
        token = str(raw).strip()
        if token:
            out.add(token)
    return frozenset(out)


def _norm_id(value: object) -> uuid.UUID | None:
    """An agent id, or None for 'any agent of this kind'."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class DelegationRule:
    """One allow-rule: from-agent(s) may delegate to to-agent(s).

    ``from_agent_id`` / ``to_agent_id`` of None mean "any agent of that
    kind". An empty ``allowed_actions`` means any action; an empty
    ``allowed_scopes`` means any scope. ``allows_sensitive`` gates
    sensitive tasks only — standard tasks need no such flag.
    """

    from_kind: str
    to_kind: str
    from_agent_id: uuid.UUID | None = None
    to_agent_id: uuid.UUID | None = None
    allowed_actions: frozenset[str] = frozenset()
    allowed_scopes: frozenset[str] = frozenset()
    allows_sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_kind", str(self.from_kind).strip().lower())
        object.__setattr__(self, "to_kind", str(self.to_kind).strip().lower())
        object.__setattr__(self, "from_agent_id", _norm_id(self.from_agent_id))
        object.__setattr__(self, "to_agent_id", _norm_id(self.to_agent_id))
        object.__setattr__(
            self, "allowed_actions",
            frozenset(_norm_action(a) for a in (self.allowed_actions or ())))
        object.__setattr__(
            self, "allowed_scopes",
            frozenset(_norm_scope(s) for s in (self.allowed_scopes or ())))


@dataclass(frozen=True)
class DelegationPolicy:
    """One versioned delegation policy: a rule set plus a depth cap."""

    version: int
    rules: tuple[DelegationRule, ...] = ()
    max_depth: int = DEFAULT_MAX_DELEGATION_DEPTH
    is_active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", tuple(self.rules or ()))


@dataclass(frozen=True)
class DelegationRequest:
    """One delegation to decide. ``description`` is opaque untrusted text."""

    parent_kind: str
    parent_agent_id: uuid.UUID
    child_kind: str
    child_agent_id: uuid.UUID
    action: str = ""
    scope_id: str = ""
    sensitivity: str = "standard"
    description: str = ""
    depth: int = 1
    requested_tools: frozenset[str] = frozenset()
    child_tools: frozenset[str] = frozenset()
    child_status: str = "active"
    originator_user_id: uuid.UUID | None = None
    approver_user_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_kind", str(self.parent_kind).strip().lower())
        object.__setattr__(self, "child_kind", str(self.child_kind).strip().lower())
        object.__setattr__(self, "action", _norm_action(self.action))
        object.__setattr__(self, "scope_id", _norm_scope(self.scope_id))
        object.__setattr__(self, "sensitivity", str(self.sensitivity).strip().lower())
        object.__setattr__(self, "requested_tools", _norm_tools(self.requested_tools))
        object.__setattr__(self, "child_tools", _norm_tools(self.child_tools))
        object.__setattr__(self, "child_status", str(self.child_status).strip().lower())


@dataclass(frozen=True)
class DelegationDecision:
    """The delegation decision: allowed or denied, with a reason.

    ``reason`` is ``"allowed"`` when the delegation may proceed, else a
    machine-readable denial code (``"no_active_policy"``,
    ``"delegation_depth_exceeded"``, ``"child_not_active:<status>"``,
    ``"no_delegation_rule"``, ``"sensitive_not_permitted"``,
    ``"tools_not_permitted:<tools>"``, ``"separated_duty_violation"``,
    ``"invalid_request:<detail>"``).
    """

    allowed: bool
    reason: str
    policy_version: int = 0


def validate_policy_rules(rules: object) -> tuple[str, ...]:
    """Structural problems in a rule list, as human-readable strings.

    An empty tuple means the rules are structurally sound. This never
    raises: unparseable entries are reported, not thrown.
    """
    problems: list[str] = []
    if rules is None:
        return ("delegation policy rules must be a list",)
    if isinstance(rules, (str, bytes)):
        return ("delegation policy rules must be a list",)
    try:
        items = list(rules)  # type: ignore[arg-type]
    except TypeError:
        return ("delegation policy rules must be a list",)
    seen: set[str] = set()
    for index, raw in enumerate(items):
        label = f"rules[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{label}: rule must be an object")
            continue
        from_kind = str(raw.get("from_kind") or "").strip().lower()
        to_kind = str(raw.get("to_kind") or "").strip().lower()
        if not from_kind:
            problems.append(f"{label}: from_kind is required")
        if not to_kind:
            problems.append(f"{label}: to_kind is required")
        for name in ("from_agent_id", "to_agent_id"):
            value = raw.get(name)
            if value is not None and _norm_id(value) is None:
                problems.append(f"{label}: {name} is not a valid agent id")
        for name in ("allowed_actions", "allowed_scopes"):
            value = raw.get(name)
            if value is not None and not isinstance(value, (list, tuple, set, frozenset)):
                problems.append(f"{label}: {name} must be a list")
        if "allows_sensitive" in raw and not isinstance(raw["allows_sensitive"], bool):
            problems.append(f"{label}: allows_sensitive must be true or false")
        key = (
            f"{from_kind}|{raw.get('from_agent_id')}|{to_kind}|{raw.get('to_agent_id')}|"
            f"{sorted(_norm_action(a) for a in (raw.get('allowed_actions') or []))}|"
            f"{sorted(_norm_scope(s) for s in (raw.get('allowed_scopes') or []))}|"
            f"{bool(raw.get('allows_sensitive'))}"
        )
        if key in seen:
            problems.append(f"{label}: duplicate rule")
        seen.add(key)
    return tuple(problems)


def policy_to_dict(policy: DelegationPolicy) -> dict:
    """Serialize a policy for the JSONB column (deterministic ordering)."""
    return {
        "version": policy.version,
        "max_depth": policy.max_depth,
        "is_active": policy.is_active,
        "rules": [
            {
                "from_kind": rule.from_kind,
                "from_agent_id": str(rule.from_agent_id) if rule.from_agent_id else None,
                "to_kind": rule.to_kind,
                "to_agent_id": str(rule.to_agent_id) if rule.to_agent_id else None,
                "allowed_actions": sorted(rule.allowed_actions),
                "allowed_scopes": sorted(rule.allowed_scopes),
                "allows_sensitive": rule.allows_sensitive,
            }
            for rule in policy.rules
        ],
    }


def policy_from_dict(data: dict | None) -> DelegationPolicy | None:
    """Rebuild a policy from its stored dict; None when there is none.

    A malformed stored policy returns None (fail closed: no policy means
    no policy-governed delegation), never an exception.
    """
    if not isinstance(data, dict):
        return None
    try:
        version = int(data.get("version") or 0)
        max_depth = int(data.get("max_depth") or DEFAULT_MAX_DELEGATION_DEPTH)
    except (TypeError, ValueError):
        return None
    rules: list[DelegationRule] = []
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        return None
    for raw in raw_rules:
        if not isinstance(raw, dict):
            return None
        rules.append(DelegationRule(
            from_kind=str(raw.get("from_kind") or ""),
            from_agent_id=_norm_id(raw.get("from_agent_id")),
            to_kind=str(raw.get("to_kind") or ""),
            to_agent_id=_norm_id(raw.get("to_agent_id")),
            allowed_actions=frozenset(
                _norm_action(a) for a in (raw.get("allowed_actions") or [])),
            allowed_scopes=frozenset(
                _norm_scope(s) for s in (raw.get("allowed_scopes") or [])),
            allows_sensitive=bool(raw.get("allows_sensitive")),
        ))
    return DelegationPolicy(
        version=version, rules=tuple(rules), max_depth=max_depth,
        is_active=bool(data.get("is_active", True)))


def _rule_matches(rule: DelegationRule, request: DelegationRequest) -> bool:
    """True when this rule covers the (from, to, action, scope) at all."""
    if rule.from_kind != request.parent_kind or rule.to_kind != request.child_kind:
        return False
    if rule.from_agent_id is not None and rule.from_agent_id != request.parent_agent_id:
        return False
    if rule.to_agent_id is not None and rule.to_agent_id != request.child_agent_id:
        return False
    if rule.allowed_actions and request.action not in rule.allowed_actions:
        return False
    if rule.allowed_scopes and request.scope_id not in rule.allowed_scopes:
        return False
    return True


def evaluate_delegation(
    policy: DelegationPolicy | None, request: DelegationRequest,
) -> DelegationDecision:
    """Decide one delegation. The task description is never read.

    Never raises on bad policy/request data — bad data becomes a denial
    with a reason.
    """
    if policy is None or not policy.is_active or policy.version < 1:
        return DelegationDecision(
            allowed=False, reason="no_active_policy", policy_version=0)
    version = policy.version
    if not isinstance(request.depth, int) or isinstance(request.depth, bool) \
            or request.depth < 1:
        return DelegationDecision(
            allowed=False, reason="invalid_request:bad_depth", policy_version=version)
    if request.sensitivity not in _SENSITIVITIES:
        return DelegationDecision(
            allowed=False,
            reason=f"invalid_request:unknown_sensitivity:{request.sensitivity}",
            policy_version=version)
    if not isinstance(policy.max_depth, int) or isinstance(policy.max_depth, bool) \
            or policy.max_depth < 1:
        return DelegationDecision(
            allowed=False, reason="invalid_request:bad_max_depth", policy_version=version)
    if request.depth > policy.max_depth:
        return DelegationDecision(
            allowed=False, reason="delegation_depth_exceeded", policy_version=version)
    if request.child_status != "active":
        return DelegationDecision(
            allowed=False,
            reason=f"child_not_active:{request.child_status}",
            policy_version=version)
    matching = [rule for rule in policy.rules if _rule_matches(rule, request)]
    if not matching:
        return DelegationDecision(
            allowed=False, reason="no_delegation_rule", policy_version=version)
    if request.sensitivity == "sensitive" and not any(
            rule.allows_sensitive for rule in matching):
        return DelegationDecision(
            allowed=False, reason="sensitive_not_permitted", policy_version=version)
    forbidden = sorted(request.requested_tools - request.child_tools)
    if forbidden:
        return DelegationDecision(
            allowed=False,
            reason="tools_not_permitted:" + ",".join(forbidden),
            policy_version=version)
    if request.approver_user_id is not None \
            and request.originator_user_id is not None \
            and request.approver_user_id == request.originator_user_id:
        return DelegationDecision(
            allowed=False, reason="separated_duty_violation", policy_version=version)
    return DelegationDecision(allowed=True, reason="allowed", policy_version=version)
