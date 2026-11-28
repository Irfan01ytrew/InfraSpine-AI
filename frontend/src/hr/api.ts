import { request } from "../api";

export interface HrScope { id: string; name: string; permissions: string[] }
export interface LeaveRequest {
  id: string; group_id: string; employee_id: string; leave_type: string;
  start_date: string; end_date: string; half_day: boolean; days: string; status: string;
  workflow_run_id: string | null; approval_id: string | null; decided_by: string | null;
  decided_at: string | null; applied_at: string | null; created_at: string | null;
}
export interface LeaveApproval {
  approval_id: string; request_id: string; employee_id: string; email: string | null;
  leave_type: string; start_date: string; end_date: string; days: string | null;
  action_hash: string; expires_at: string | null;
}
export interface LeaveBalance {
  employee_id: string; leave_type: string; year: number;
  entitled: number; carried: number; used: number; pending: number; remaining: number;
}
export interface AttendanceRow {
  id: string; group_id: string; employee_id: string; date: string; status: string;
  check_in: string | null; check_out: string | null; created_at: string | null;
}
export interface AttendanceSummaryEmployee {
  employee_id: string; email: string | null; business_days: number;
  present: number; work_from_home: number; leave: number; holiday: number; unrecorded: number;
  rows: AttendanceRow[];
}
export interface AttendanceSummary { group_id: string; month: string; employees: AttendanceSummaryEmployee[] }
export interface PayrollSchedule {
  id: string; group_id: string; employee_id: string; email: string | null;
  pay_day: number; amount: string; currency: string; active: boolean; created_at: string | null;
}
export interface SalaryReminder {
  id: string; group_id: string; employee_id: string; email: string | null;
  payroll_schedule_id: string; pay_date: string; kind: string; amount: string; currency: string;
  message: string; sent_at: string | null; channel: string | null; pending_delivery: boolean;
  created_at: string | null;
}

const root = "/api/hr";
const call = <T,>(method: string, path: string, body?: unknown) => request<T>(method, root + path, body);

export const hrApi = {
  scopes: () => call<{ scopes: HrScope[] }>("GET", "/scopes"),
  myLeave: (group: string) => call<{ leave: LeaveRequest[] }>("GET", `/leave?group_id=${group}`),
  teamLeave: (group: string) => call<{ leave: LeaveRequest[] }>("GET", `/leave/team?group_id=${group}`),
  leaveApprovals: (group: string) => call<{ approvals: LeaveApproval[] }>("GET", `/leave/approvals?group_id=${group}`),
  requestLeave: (group: string, leave_type: string, start_date: string, end_date: string, half_day: boolean) =>
    call<LeaveRequest>("POST", "/leave", { group_id: group, leave_type, start_date, end_date, half_day }),
  decideLeave: (id: string, group: string, decision: "approve" | "reject", presented_hash: string, note = "") =>
    call<LeaveRequest>("POST", `/leave/${id}/decide?group_id=${group}`, { decision, presented_hash, note }),
  executeLeave: (id: string, group: string) =>
    call<LeaveRequest>("POST", `/leave/${id}/execute?group_id=${group}`),
  cancelLeave: (id: string, group: string) =>
    call<LeaveRequest>("POST", `/leave/${id}/cancel?group_id=${group}`),
  balance: (group: string, year: number, leave_type: string) =>
    call<LeaveBalance>("GET", `/leave/balance?group_id=${group}&year=${year}&leave_type=${leave_type}`),
  recordAttendance: (group: string, date: string, status: string, check_in?: string | null, check_out?: string | null, employee_id?: string) =>
    call<AttendanceRow>("POST", "/attendance", { group_id: group, employee_id: employee_id ?? null, date, status, check_in: check_in ?? null, check_out: check_out ?? null }),
  attendance: (group: string, month: string, employee_id?: string) =>
    call<{ attendance: AttendanceRow[] }>("GET", `/attendance?group_id=${group}&month=${month}${employee_id ? `&employee_id=${employee_id}` : ""}`),
  attendanceSummary: (group: string, month: string, employee_id?: string) =>
    call<AttendanceSummary>("GET", `/attendance/summary?group_id=${group}&month=${month}${employee_id ? `&employee_id=${employee_id}` : ""}`),
  payrollSchedules: (group: string) => call<{ schedules: PayrollSchedule[] }>("GET", `/payroll-schedules?group_id=${group}`),
  myPayrollSchedule: (group: string) => call<{ schedule: PayrollSchedule | null }>("GET", `/payroll-schedules/mine?group_id=${group}`),
  savePayrollSchedule: (group: string, employee_id: string, pay_day: number, amount: string, currency: string) =>
    call<PayrollSchedule>("POST", "/payroll-schedules", { group_id: group, employee_id, pay_day, amount, currency }),
  deactivatePayrollSchedule: (id: string, group: string) =>
    call<PayrollSchedule>("POST", `/payroll-schedules/${id}/deactivate?group_id=${group}`),
  salaryReminders: (group: string, month?: string, employee_id?: string) =>
    call<{ reminders: SalaryReminder[] }>("GET", `/salary-reminders?group_id=${group}${month ? `&month=${month}` : ""}${employee_id ? `&employee_id=${employee_id}` : ""}`),
  runSalaryReminders: (group: string) =>
    call<{ created: number; already_present: number; due: number }>("POST", "/salary-reminders/run", { group_id: group }),
};
