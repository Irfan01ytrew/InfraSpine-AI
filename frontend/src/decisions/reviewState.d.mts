export type ReviewState = "overdue" | "due" | "ok" | "none";

export function reviewState(reviewAt: string | null | undefined, asOf: string, daysAhead?: number): ReviewState;
export function reviewStateLabel(state: ReviewState): string;
export function sortDue<T extends { id: string; review_at?: string | null; decided_at?: string | null }>(decisions: T[]): T[];
