/** Exclude the current owner so the UI cannot default a reassignment to a no-op. */
export function reassignableAssignees(assignees, currentAssigneeId) {
  return assignees.filter((assignee) => assignee.user_id !== currentAssigneeId);
}
