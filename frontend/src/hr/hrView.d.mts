export interface LeaveLike { status: string; days: string | number | null }
export interface BalanceLike { remaining: number | string; entitled: number | string; used: number | string; pending: number | string }
export interface SummaryLike { business_days: number | string; present: number | string; work_from_home: number | string; leave: number | string; holiday: number | string; unrecorded: number | string }
export interface ReminderLike { pay_date: string; pending_delivery: boolean }

export function monthLabel(month: string): string;
export function leaveStatusCounts(leave: readonly LeaveLike[] | null | undefined): {
  pending: number; approved: number; rejected: number; applied: number; cancelled: number; other: number;
};
export function leaveDaysTotal(leave: readonly LeaveLike[] | null | undefined): number;
export function balanceText(balance: BalanceLike | null | undefined): string;
export function attendanceTotals(summary: SummaryLike | null | undefined): {
  businessDays: number; attended: number; leave: number; holiday: number; unrecorded: number; pct: number;
};
export function upcomingReminders<T extends ReminderLike>(reminders: readonly T[] | null | undefined): T[];
export function canApprove(permissions: readonly string[] | null | undefined): boolean;
export function canManage(permissions: readonly string[] | null | undefined): boolean;
export function currentMonth(): string;
