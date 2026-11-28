// Pure helpers for the HR page. No imports, no side effects — unit-tested with node --test.

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export function monthLabel(month) {
  if (typeof month !== "string") return "";
  const match = month.match(/^(\d{4})-(\d{2})$/);
  if (!match) return month;
  const year = Number(match[1]);
  const m = Number(match[2]);
  if (m < 1 || m > 12) return month;
  return `${MONTH_NAMES[m - 1]} ${year}`;
}

export function leaveStatusCounts(leave) {
  const counts = { pending: 0, approved: 0, rejected: 0, applied: 0, cancelled: 0, other: 0 };
  for (const item of leave ?? []) {
    const status = item.status ?? "";
    if (Object.hasOwn(counts, status)) counts[status] += 1;
    else counts.other += 1;
  }
  return counts;
}

export function leaveDaysTotal(leave) {
  return (leave ?? []).reduce((sum, item) => sum + (Number(item.days) || 0), 0);
}

export function balanceText(balance) {
  if (!balance) return "";
  const remaining = Number(balance.remaining) || 0;
  const entitled = Number(balance.entitled) || 0;
  const used = Number(balance.used) || 0;
  const pending = Number(balance.pending) || 0;
  return `${remaining} of ${entitled} days left · ${used} used · ${pending} awaiting decision`;
}

export function attendanceTotals(summary) {
  if (!summary) return { businessDays: 0, attended: 0, leave: 0, holiday: 0, unrecorded: 0, pct: 0 };
  const businessDays = Number(summary.business_days) || 0;
  const attended = (Number(summary.present) || 0) + (Number(summary.work_from_home) || 0);
  const leave = Number(summary.leave) || 0;
  const holiday = Number(summary.holiday) || 0;
  const unrecorded = Number(summary.unrecorded) || 0;
  const pct = businessDays > 0 ? Math.round((attended / businessDays) * 100) : 0;
  return { businessDays, attended, leave, holiday, unrecorded, pct };
}

export function upcomingReminders(reminders) {
  return [...(reminders ?? [])].sort((a, b) => {
    if (a.pending_delivery !== b.pending_delivery) return a.pending_delivery ? -1 : 1;
    return String(a.pay_date).localeCompare(String(b.pay_date));
  });
}

export function canApprove(permissions) {
  return (permissions ?? []).includes("hr.leave.approve") || (permissions ?? []).includes("hr.manage");
}

export function canManage(permissions) {
  return (permissions ?? []).includes("hr.manage");
}

export function currentMonth() {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  return `${now.getFullYear()}-${month}`;
}
