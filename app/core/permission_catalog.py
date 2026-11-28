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
DEPARTMENT_CAPABILITIES = (PERMISSION_PEOPLE_INVITE, PERMISSION_TRAINING_MANAGE, PERMISSION_AGENTS_MANAGE)


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
)
PERMISSION_BY_KEY = {item.permission: item for item in PERMISSION_CATALOG}
PERMISSIONS = tuple(item.permission for item in PERMISSION_CATALOG)
KNOWLEDGE_CATEGORIES = ("policy", "financial", "legal", "client", "operational", "general")
