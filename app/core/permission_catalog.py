"""The supported resource/action permissions and their available scope dimensions."""

from dataclasses import dataclass

PERMISSION_REVIEW = "knowledge.review"
PERMISSION_PUBLISH = "knowledge.publish"
PERMISSION_STEWARD = "data.steward"
# Business capabilities that can be delegated to Managers/Employees without making them administrators.
PERMISSION_REPORTS_VIEW = "reports.view"
PERMISSION_PEOPLE_INVITE = "people.invite"  # invite Employees into the departments in scope
PERMISSION_TRAINING_MANAGE = "training.manage"  # assign training to, and follow progress in, departments in scope
PERMISSION_AGENTS_MANAGE = "agents.manage"  # create, version and disable department agents in scope
PERMISSION_AGENTS_READ = "agents.read"  # view per-agent control records, events and spend (Step AK)
PERMISSION_SELF_HEALING_REMEDY = "self_healing.remedy"  # run only allow-listed, verified self-healing remedies
PERMISSION_WORKFORCE_MANAGE = "workforce.manage"  # manage the org-wide workforce skill registry (Step J)
PERMISSION_WORKFORCE_READ = "workforce.read"  # view workforce skills (Step J)
# Step L (slice 3): read stored predictive-continuity forecasts. Org-wide,
# read-only; graph visibility still filters every forecast body.
PERMISSION_FORECASTS_READ = "forecasts.read"
PERMISSION_DECISION_RECORD = "decision.record"  # record, supersede and revoke org decisions (Step M)
PERMISSION_DECISION_READ = "decision.read"  # read org decisions and their readable evidence (Step M)
PERMISSION_DECISION_APPROVE = "decision.approve"  # approve sensitive org decisions (Step M)
PERMISSION_COST_READ = "cost.read"  # read cost/ROI figures for scopes in scope (Step N)
PERMISSION_CONTROL_READ = "control.read"  # read the control-plane inventory for scopes in scope (Step N)
DEPARTMENT_CAPABILITIES = (PERMISSION_PEOPLE_INVITE, PERMISSION_TRAINING_MANAGE, PERMISSION_AGENTS_MANAGE,
                           PERMISSION_COST_READ, PERMISSION_CONTROL_READ)


@dataclass(frozen=True)
class PermissionDefinition:
    permission: str
    resource: str
    action: str
    scope_dimensions: tuple[str, ...]


PERMISSION_CATALOG = (
    PermissionDefinition(PERMISSION_REVIEW, "knowledge", "review", ("category", "access_group")),
    PermissionDefinition(PERMISSION_PUBLISH, "knowledge", "publish", ("category", "access_group")),
    PermissionDefinition(PERMISSION_STEWARD, "data", "steward", ("category", "access_group")),
    PermissionDefinition(PERMISSION_REPORTS_VIEW, "reports", "view", ()),  # organization-wide usage reports, read-only
    PermissionDefinition(PERMISSION_PEOPLE_INVITE, "people", "invite", ("access_group",)),
    PermissionDefinition(PERMISSION_TRAINING_MANAGE, "training", "manage", ("access_group",)),
    PermissionDefinition(PERMISSION_AGENTS_MANAGE, "agents", "manage", ("access_group",)),
    PermissionDefinition(PERMISSION_AGENTS_READ, "agents", "read", ("access_group",)),
    # Granted to ops roles only; deliberately NOT a department capability and NOT in the legacy
    # admin bridge: remedies are operational duties, never business duties.
    PermissionDefinition(PERMISSION_SELF_HEALING_REMEDY, "self_healing", "remedy", ("access_group",)),
    PermissionDefinition(PERMISSION_WORKFORCE_MANAGE, "workforce", "manage", ()),  # org-wide skill registry
    PermissionDefinition(PERMISSION_WORKFORCE_READ, "workforce", "read", ()),  # org-wide skill registry, read-only
    PermissionDefinition(PERMISSION_FORECASTS_READ, "forecasts", "read", ()),  # stored predictive-continuity forecasts, read-only
    PermissionDefinition(PERMISSION_DECISION_RECORD, "decision", "record", ("access_group",)),
    PermissionDefinition(PERMISSION_DECISION_READ, "decision", "read", ("access_group",)),
    PermissionDefinition(PERMISSION_DECISION_APPROVE, "decision", "approve", ("access_group",)),
    PermissionDefinition(PERMISSION_COST_READ, "cost", "read", ()),  # org-wide / department cost figures, read-only
    PermissionDefinition(PERMISSION_CONTROL_READ, "control", "read", ()),  # control-plane inventory, read-only
)
PERMISSION_BY_KEY = {item.permission: item for item in PERMISSION_CATALOG}
PERMISSIONS = tuple(item.permission for item in PERMISSION_CATALOG)
KNOWLEDGE_CATEGORIES = ("policy", "financial", "legal", "client", "operational", "general")
