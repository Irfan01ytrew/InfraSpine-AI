export interface UserIdentifier {
  user_id: string;
}

export function reassignableAssignees<T extends UserIdentifier>(
  assignees: readonly T[],
  currentAssigneeId: string,
): T[];
