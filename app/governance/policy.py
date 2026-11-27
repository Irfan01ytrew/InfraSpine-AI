"""Deterministic, default-deny evaluation. Inputs are server-resolved identities and resources."""

from dataclasses import asdict, dataclass

from app.governance.schemas import ACTIONS, RISKS, Rule


@dataclass(frozen=True)
class Evaluation:
    effect: str
    risk: str
    required_approvals: int
    reason: str
    matched_rules: list[str]
    conflicts: list[str]

    def to_dict(self):
        return asdict(self)


def evaluate(rules: list[dict], *, action: str, resource, roles: set[str], permissions: set[str], department_id, all_departments: bool) -> Evaluation:
    minimum = ACTIONS.get(action)
    denied = lambda reason: Evaluation("deny", minimum[1] if minimum else "critical", 0, reason, [], [])
    if minimum is None or resource.kind != minimum[0]:
        return denied("unknown_action_or_resource")
    if action not in permissions:
        return denied("missing_explicit_permission")
    if resource.department_id is not None and not all_departments and resource.department_id != department_id:
        return denied("department_boundary")
    matched = []
    for raw in rules:
        rule = Rule.model_validate(raw)
        if action not in rule.actions:
            continue
        if rule.role_ids and not (set(map(str, rule.role_ids)) & roles):
            continue
        if rule.department_ids and resource.department_id not in rule.department_ids:
            continue
        if rule.resource_ids and resource.id not in rule.resource_ids:
            continue
        if rule.classifications and resource.classification not in rule.classifications:
            continue
        matched.append(rule)
    if not matched:
        return denied("default_deny")
    names = [rule.name for rule in matched]
    effects = {rule.effect for rule in matched}
    conflicts = names if "deny" in effects and len(effects) > 1 else []
    if "deny" in effects:
        return Evaluation("deny", minimum[1], 0, "explicit_deny", names, conflicts)
    risk = max([minimum[1], *[rule.risk for rule in matched]], key=RISKS.index)
    if "approval" in effects and risk == "low":
        risk = "medium"
    count = {"low": 0, "medium": 1, "high": 1, "critical": 2}[risk]
    return Evaluation("approval" if count else "allow", risk, count, "matched_policy", names, conflicts)
