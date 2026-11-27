/** Plain-language names for delegable permissions, shared by Permissions and Custom roles. */
export const PERMISSION_LABELS: Record<string, string> = {
  "knowledge.review": "Review knowledge",
  "knowledge.publish": "Publish knowledge",
  "data.steward": "Data steward (inherits leavers' knowledge)",
  "reports.view": "View organization usage reports",
  "people.invite": "Invite employees",
  "training.manage": "Manage training",
  "agents.manage": "Manage AI agents",
};

/** Permissions limited by knowledge category; the others apply per department or organization-wide. */
export const CATEGORY_PERMISSIONS = new Set(["knowledge.review", "knowledge.publish", "data.steward"]);

export function permissionLabel(permission: string): string {
  return PERMISSION_LABELS[permission] ?? permission;
}
