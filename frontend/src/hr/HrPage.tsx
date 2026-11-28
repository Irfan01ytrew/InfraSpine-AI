import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { CalendarCheck, Clock, RefreshCw, UserRound, Wallet } from "lucide-react";
import { api, describeError } from "../api";
import { useFeedback } from "../components/Feedback";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, SelectInput, Skeleton, Stat, Table, Tabs } from "../components/ui";
import { hrApi } from "./api";
import type { AttendanceSummaryEmployee, HrScope, LeaveApproval, LeaveBalance, LeaveRequest, PayrollSchedule, SalaryReminder } from "./api";
import { attendanceTotals, balanceText, currentMonth, leaveStatusCounts, monthLabel } from "./hrView.mjs";

type Tab = "mine" | "team" | "attendance" | "payroll";
type Tone = "neutral" | "primary" | "success" | "warning" | "danger";
const LEAVE_TYPES = ["annual", "sick", "casual"] as const;
const ATTENDANCE_STATUSES = ["present", "work_from_home", "leave", "holiday"] as const;

const STATUS_TONE: Record<string, Tone> = {
  pending: "warning", approved: "primary", applied: "success",
  rejected: "neutral", cancelled: "neutral", present: "success",
  work_from_home: "primary", leave: "warning", holiday: "neutral",
};

function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? "neutral"} dot>{status.replace(/_/g, " ")}</Badge>;
}

export function HrPage() {
  const { toast } = useFeedback();
  const [scopes, setScopes] = useState<HrScope[] | null>(null);
  const [group, setGroup] = useState("");
  const [tab, setTab] = useState<Tab>("mine");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    hrApi.scopes().then((result) => {
      setScopes(result.scopes);
      setGroup((current) => current || result.scopes[0]?.id || "");
    }).catch((err) => setError(describeError(err)));
  }, []);
  const scope = scopes?.find((item) => item.id === group);
  const can = useCallback((permission: string) => !!scope?.permissions.includes(permission), [scope]);
  const refresh = () => setRevision((value) => value + 1);

  return <div className="grid gap-5 pb-8">
    <PageHeader eyebrow="PEOPLE · HR" title="Leave, attendance & payroll reminders"
      description="Request leave, see your balances, record attendance and keep salary reminders on schedule. Leave is applied to your balance only after a manager approves the exact request — and salary reminders never move money."
      actions={<Button variant="secondary" onClick={refresh}><RefreshCw /> Refresh</Button>} />
    {error && <Alert tone="danger" title="HR data could not be loaded">{error}</Alert>}
    {scopes === null && !error && <Skeleton className="h-40 w-full" />}
    {scopes && scopes.length === 0 && <Card><EmptyState icon={<UserRound />} title="No HR access yet"
      description="An administrator grants HR roles per department: employee, manager or admin." /></Card>}
    {scopes && scopes.length > 0 && <>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Department" className="min-w-64">
          <SelectInput value={group} onChange={(event) => setGroup(event.target.value)} aria-label="Department">
            {scopes.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </SelectInput>
        </Field>
        <div className="flex flex-wrap gap-1.5 pb-1">{scope?.permissions.map((p) => <Badge key={p} tone="primary">{p}</Badge>)}</div>
      </div>
      <Tabs value={tab} onValueChange={(value) => setTab(value as Tab)} items={[
        { value: "mine", label: "My leave" },
        { value: "team", label: "Team leave" },
        { value: "attendance", label: "Attendance" },
        { value: "payroll", label: "Payroll reminders" },
      ]} />
      {tab === "mine" && <MyLeaveTab group={group} revision={revision} toast={toast} />}
      {tab === "team" && <TeamLeaveTab group={group} can={can} revision={revision} toast={toast} />}
      {tab === "attendance" && <AttendanceTab group={group} can={can} revision={revision} toast={toast} />}
      {tab === "payroll" && <PayrollTab group={group} can={can} revision={revision} toast={toast} />}
    </>}
  </div>;
}

type ToastFn = ReturnType<typeof useFeedback>["toast"];

function MyLeaveTab({ group, revision, toast }: { group: string; revision: number; toast: ToastFn }) {
  const [leave, setLeave] = useState<LeaveRequest[] | null>(null);
  const [balances, setBalances] = useState<LeaveBalance[] | null>(null);
  const [leaveType, setLeaveType] = useState<string>("annual");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [halfDay, setHalfDay] = useState(false);
  const [busy, setBusy] = useState(false);
  const [local, setLocal] = useState(0);
  const year = new Date().getFullYear();

  useEffect(() => {
    let live = true;
    hrApi.myLeave(group).then((result) => { if (live) setLeave(result.leave); })
      .catch((err) => toast("error", "Leave requests could not be loaded", describeError(err)));
    Promise.all(LEAVE_TYPES.map((type) => hrApi.balance(group, year, type).catch(() => null)))
      .then((rows) => { if (live) setBalances(rows.filter((row): row is LeaveBalance => row !== null)); })
      .catch(() => undefined);
    return () => { live = false; };
  }, [group, revision, local, toast, year]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!startDate || !endDate) return;
    setBusy(true);
    try {
      await hrApi.requestLeave(group, leaveType, startDate, endDate, halfDay);
      toast("success", "Leave requested", "A manager must approve it before it counts against your balance.");
      setStartDate(""); setEndDate(""); setHalfDay(false); setLocal((v) => v + 1);
    } catch (err) { toast("error", "Leave request failed", describeError(err)); }
    finally { setBusy(false); }
  }
  async function cancel(id: string) {
    try { await hrApi.cancelLeave(id, group); setLocal((v) => v + 1); }
    catch (err) { toast("error", "Leave could not be cancelled", describeError(err)); }
  }

  const counts = leaveStatusCounts(leave ?? []);
  return <div className="grid gap-4">
    <div className="grid gap-4 sm:grid-cols-3">
      {(balances ?? []).map((balance) => <Stat key={balance.leave_type} label={`${balance.leave_type} leave · ${balance.year}`}
        value={`${balance.remaining} days left`} hint={balanceText(balance)} />)}
      {balances === null && <Skeleton className="h-24 w-full" />}
    </div>
    <Card>
      <CardHeader title="Request leave" description="Weekends do not count. A half day counts as 0.5." />
      <CardBody>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[160px_1fr_1fr_auto] sm:items-end">
          <Field label="Leave type">
            <SelectInput value={leaveType} onChange={(event) => setLeaveType(event.target.value)} aria-label="Leave type">
              {LEAVE_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
            </SelectInput>
          </Field>
          <Field label="From"><Input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} aria-label="Start date" /></Field>
          <Field label="To"><Input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} aria-label="End date" /></Field>
          <label className="flex items-center gap-2 pb-2 text-sm"><input type="checkbox" checked={halfDay} onChange={(event) => setHalfDay(event.target.checked)} /> Half day</label>
          <Button type="submit" disabled={busy || !startDate || !endDate}>Request leave</Button>
        </form>
      </CardBody>
    </Card>
    <Card>
      <CardHeader title="My requests" description={`${counts.pending} pending · ${counts.approved} approved · ${counts.applied} applied`} />
      <CardBody>
        {leave === null && <Skeleton className="h-24 w-full" />}
        {leave?.length === 0 && <EmptyState icon={<CalendarCheck />} title="No leave requests yet" />}
        {(leave?.length ?? 0) > 0 && <Table>
          <thead><tr><th>Type</th><th>Dates</th><th>Days</th><th>Status</th><th></th></tr></thead>
          <tbody>{leave!.map((item) => <tr key={item.id}>
            <td>{item.leave_type}</td>
            <td>{item.start_date} → {item.end_date}{item.half_day ? " (half day)" : ""}</td>
            <td>{item.days}</td>
            <td><StatusBadge status={item.status} /></td>
            <td>{item.status === "pending" && <Button variant="secondary" onClick={() => cancel(item.id)}>Cancel</Button>}</td>
          </tr>)}</tbody>
        </Table>}
      </CardBody>
    </Card>
  </div>;
}

function TeamLeaveTab({ group, can, revision, toast }: { group: string; can: (p: string) => boolean; revision: number; toast: ToastFn }) {
  const [leave, setLeave] = useState<LeaveRequest[] | null>(null);
  const [approvals, setApprovals] = useState<LeaveApproval[] | null>(null);
  const [local, setLocal] = useState(0);
  const approver = can("hr.leave.approve") || can("hr.manage");

  useEffect(() => {
    let live = true;
    hrApi.teamLeave(group).then((result) => { if (live) setLeave(result.leave); })
      .catch((err) => toast("error", "Team leave could not be loaded", describeError(err)));
    if (approver) {
      hrApi.leaveApprovals(group).then((result) => { if (live) setApprovals(result.approvals); })
        .catch(() => { if (live) setApprovals([]); });
    }
    return () => { live = false; };
  }, [group, revision, local, toast, approver]);

  async function decide(approval: LeaveApproval, decision: "approve" | "reject") {
    try {
      await hrApi.decideLeave(approval.request_id, group, decision, approval.action_hash);
      toast("success", decision === "approve" ? "Leave approved" : "Leave rejected",
        decision === "approve" ? "Run Apply to update the balance." : "");
      setLocal((v) => v + 1);
    } catch (err) { toast("error", "Decision failed", describeError(err)); }
  }
  async function execute(id: string) {
    try { await hrApi.executeLeave(id, group); toast("success", "Leave applied to the balance"); setLocal((v) => v + 1); }
    catch (err) { toast("error", "Apply failed", describeError(err)); }
  }

  return <div className="grid gap-4">
    {approver && <Card>
      <CardHeader title="Awaiting your decision" description="Each decision applies only to the exact request shown — the action hash binds it." />
      <CardBody>
        {approvals === null && <Skeleton className="h-20 w-full" />}
        {approvals?.length === 0 && <p className="text-sm text-muted">Nothing waiting for a decision.</p>}
        {(approvals?.length ?? 0) > 0 && <Table>
          <thead><tr><th>Employee</th><th>Type</th><th>Dates</th><th>Days</th><th></th></tr></thead>
          <tbody>{approvals!.map((item) => <tr key={item.approval_id}>
            <td>{item.email ?? item.employee_id}</td>
            <td>{item.leave_type}</td>
            <td>{item.start_date} → {item.end_date}</td>
            <td>{item.days}</td>
            <td><div className="flex gap-2">
              <Button onClick={() => decide(item, "approve")}>Approve</Button>
              <Button variant="secondary" onClick={() => decide(item, "reject")}>Reject</Button>
            </div></td>
          </tr>)}</tbody>
        </Table>}
      </CardBody>
    </Card>}
    <Card>
      <CardHeader title="Team leave" description="Every request in this department, newest first." />
      <CardBody>
        {leave === null && <Skeleton className="h-24 w-full" />}
        {leave?.length === 0 && <EmptyState icon={<CalendarCheck />} title="No team leave yet" />}
        {(leave?.length ?? 0) > 0 && <Table>
          <thead><tr><th>Type</th><th>Dates</th><th>Days</th><th>Status</th><th></th></tr></thead>
          <tbody>{leave!.map((item) => <tr key={item.id}>
            <td>{item.leave_type}</td>
            <td>{item.start_date} → {item.end_date}</td>
            <td>{item.days}</td>
            <td><StatusBadge status={item.status} /></td>
            <td>{approver && item.status === "approved" && <Button variant="secondary" onClick={() => execute(item.id)}>Apply</Button>}</td>
          </tr>)}</tbody>
        </Table>}
      </CardBody>
    </Card>
  </div>;
}

function AttendanceTab({ group, can, revision, toast }: { group: string; can: (p: string) => boolean; revision: number; toast: ToastFn }) {
  const [month, setMonth] = useState(currentMonth());
  const [summary, setSummary] = useState<AttendanceSummaryEmployee[] | null>(null);
  const [date, setDate] = useState("");
  const [status, setStatus] = useState<string>("present");
  const [checkIn, setCheckIn] = useState("");
  const [checkOut, setCheckOut] = useState("");
  const [busy, setBusy] = useState(false);
  const [local, setLocal] = useState(0);
  const recorder = can("hr.attendance.record") || can("hr.manage");

  useEffect(() => {
    let live = true;
    hrApi.attendanceSummary(group, month).then((result) => { if (live) setSummary(result.employees); })
      .catch((err) => toast("error", "Attendance summary could not be loaded", describeError(err)));
    return () => { live = false; };
  }, [group, month, revision, local, toast]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!date) return;
    setBusy(true);
    try {
      await hrApi.recordAttendance(group, date, status, checkIn || null, checkOut || null);
      toast("success", "Attendance recorded");
      setDate(""); setCheckIn(""); setCheckOut(""); setLocal((v) => v + 1);
    } catch (err) { toast("error", "Attendance could not be recorded", describeError(err)); }
    finally { setBusy(false); }
  }

  return <div className="grid gap-4">
    <div className="flex flex-wrap items-end gap-3">
      <Field label="Month" className="min-w-48">
        <Input type="month" value={month} onChange={(event) => setMonth(event.target.value)} aria-label="Month" />
      </Field>
      <p className="pb-2 text-sm text-muted">{monthLabel(month)} — days with no record and no approved leave are shown as unrecorded, never as absent.</p>
    </div>
    {recorder && <Card>
      <CardHeader title="Record attendance" description="One row per day. Recording the same day again updates it." />
      <CardBody>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[1fr_160px_140px_140px_auto] sm:items-end">
          <Field label="Date"><Input type="date" value={date} onChange={(event) => setDate(event.target.value)} aria-label="Date" /></Field>
          <Field label="Status">
            <SelectInput value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Status">
              {ATTENDANCE_STATUSES.map((item) => <option key={item} value={item}>{item.replace(/_/g, " ")}</option>)}
            </SelectInput>
          </Field>
          <Field label="Check-in"><Input type="time" value={checkIn} onChange={(event) => setCheckIn(event.target.value)} aria-label="Check-in" /></Field>
          <Field label="Check-out"><Input type="time" value={checkOut} onChange={(event) => setCheckOut(event.target.value)} aria-label="Check-out" /></Field>
          <Button type="submit" disabled={busy || !date}>Record</Button>
        </form>
      </CardBody>
    </Card>}
    {summary === null && <Skeleton className="h-32 w-full" />}
    {(summary ?? []).map((employee) => {
      const totals = attendanceTotals(employee);
      return <Card key={employee.employee_id}>
        <CardHeader title={employee.email ?? employee.employee_id}
          description={`${totals.attended} of ${totals.businessDays} business days attended (${totals.pct}%) · ${totals.leave} on leave · ${totals.unrecorded} unrecorded`} />
        <CardBody>
          {employee.rows.length === 0 && <p className="text-sm text-muted">No attendance recorded this month.</p>}
          {employee.rows.length > 0 && <Table>
            <thead><tr><th>Date</th><th>Status</th><th>Check-in</th><th>Check-out</th></tr></thead>
            <tbody>{employee.rows.map((row) => <tr key={row.id}>
              <td>{row.date}</td>
              <td><StatusBadge status={row.status} /></td>
              <td>{row.check_in ?? "—"}</td>
              <td>{row.check_out ?? "—"}</td>
            </tr>)}</tbody>
          </Table>}
        </CardBody>
      </Card>;
    })}
  </div>;
}

function PayrollTab({ group, can, revision, toast }: { group: string; can: (p: string) => boolean; revision: number; toast: ToastFn }) {
  const [schedules, setSchedules] = useState<PayrollSchedule[] | null>(null);
  const [reminders, setReminders] = useState<SalaryReminder[] | null>(null);
  const [members, setMembers] = useState<{ user_id: string; email: string }[]>([]);
  const [month, setMonth] = useState(currentMonth());
  const [employeeId, setEmployeeId] = useState("");
  const [payDay, setPayDay] = useState("25");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("PKR");
  const [busy, setBusy] = useState(false);
  const [local, setLocal] = useState(0);
  const isManager = can("hr.manage");

  useEffect(() => {
    let live = true;
    hrApi.salaryReminders(group, month).then((result) => { if (live) setReminders(result.reminders); })
      .catch((err) => toast("error", "Salary reminders could not be loaded", describeError(err)));
    if (isManager) {
      hrApi.payrollSchedules(group).then((result) => { if (live) setSchedules(result.schedules); })
        .catch((err) => toast("error", "Payroll schedules could not be loaded", describeError(err)));
      api.departmentMembers(group).then((result) => { if (live) setMembers(result.members); })
        .catch(() => undefined);
    }
    return () => { live = false; };
  }, [group, month, revision, local, toast, isManager]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!employeeId || !amount) return;
    setBusy(true);
    try {
      await hrApi.savePayrollSchedule(group, employeeId, Number(payDay), amount, currency);
      toast("success", "Payroll schedule saved");
      setAmount(""); setLocal((v) => v + 1);
    } catch (err) { toast("error", "Schedule could not be saved", describeError(err)); }
    finally { setBusy(false); }
  }
  async function runCycle() {
    try {
      const result = await hrApi.runSalaryReminders(group);
      toast("success", `Salary reminders checked`, `${result.created} created, ${result.already_present} already present.`);
      setLocal((v) => v + 1);
    } catch (err) { toast("error", "Reminder cycle failed", describeError(err)); }
  }
  async function deactivate(id: string) {
    try { await hrApi.deactivatePayrollSchedule(id, group); setLocal((v) => v + 1); }
    catch (err) { toast("error", "Schedule could not be deactivated", describeError(err)); }
  }

  return <div className="grid gap-4">
    <div className="flex flex-wrap items-end gap-3">
      <Field label="Month" className="min-w-48">
        <Input type="month" value={month} onChange={(event) => setMonth(event.target.value)} aria-label="Month" />
      </Field>
      <p className="pb-2 text-sm text-muted">{monthLabel(month)} — reminders only remind. No money ever moves from this page.</p>
      {isManager && <Button variant="secondary" onClick={runCycle}><Clock /> Run reminder cycle</Button>}
    </div>
    {isManager && <Card>
      <CardHeader title="Payroll schedules" description="One schedule per employee. Saving again updates the existing schedule." />
      <CardBody className="grid gap-3">
        <form onSubmit={save} className="grid gap-3 sm:grid-cols-[1fr_110px_160px_100px_auto] sm:items-end">
          <Field label="Employee">
            <SelectInput value={employeeId} onChange={(event) => setEmployeeId(event.target.value)} aria-label="Employee">
              <option value="">Select employee</option>
              {members.map((member) => <option key={member.user_id} value={member.user_id}>{member.email}</option>)}
            </SelectInput>
          </Field>
          <Field label="Pay day"><Input type="number" min={1} max={31} value={payDay} onChange={(event) => setPayDay(event.target.value)} aria-label="Pay day" /></Field>
          <Field label="Amount"><Input inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} aria-label="Amount" /></Field>
          <Field label="Currency"><Input value={currency} onChange={(event) => setCurrency(event.target.value)} aria-label="Currency" /></Field>
          <Button type="submit" disabled={busy || !employeeId || !amount}>Save schedule</Button>
        </form>
        {schedules === null && <Skeleton className="h-20 w-full" />}
        {(schedules?.length ?? 0) > 0 && <Table>
          <thead><tr><th>Employee</th><th>Pay day</th><th>Amount</th><th>Active</th><th></th></tr></thead>
          <tbody>{schedules!.map((item) => <tr key={item.id}>
            <td>{item.email ?? item.employee_id}</td>
            <td>{item.pay_day}</td>
            <td>{item.amount} {item.currency}</td>
            <td><Badge tone={item.active ? "success" : "neutral"}>{item.active ? "Active" : "Inactive"}</Badge></td>
            <td>{item.active && <Button variant="secondary" onClick={() => deactivate(item.id)}>Deactivate</Button>}</td>
          </tr>)}</tbody>
        </Table>}
      </CardBody>
    </Card>}
    <Card>
      <CardHeader title="Salary reminders" description="Rows are created by the reminder cycle; delivery (WhatsApp or email) is a scheduled report, shown as pending until sent." />
      <CardBody>
        {reminders === null && <Skeleton className="h-24 w-full" />}
        {reminders?.length === 0 && <EmptyState icon={<Wallet />} title="No reminders this month" />}
        {(reminders?.length ?? 0) > 0 && <Table>
          <thead><tr><th>Pay date</th><th>Employee</th><th>Kind</th><th>Amount</th><th>Delivery</th></tr></thead>
          <tbody>{reminders!.map((item) => <tr key={item.id}>
            <td>{item.pay_date}</td>
            <td>{item.email ?? item.employee_id}</td>
            <td><Badge tone={item.kind === "overdue" ? "danger" : item.kind === "due_today" ? "warning" : "primary"}>{item.kind.replace(/_/g, " ")}</Badge></td>
            <td>{item.amount} {item.currency}</td>
            <td>{item.pending_delivery ? <Badge tone="warning">Pending delivery</Badge> : <Badge tone="success">Sent {item.channel ?? ""}</Badge>}</td>
          </tr>)}</tbody>
        </Table>}
      </CardBody>
    </Card>
  </div>;
}
