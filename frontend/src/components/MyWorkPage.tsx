import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, describeError } from "../api";
import { AlarmClock, Bell, CalendarDays, Check, ChevronLeft, ChevronRight, ClipboardList, FolderSync, GraduationCap, LifeBuoy, ListTodo, Plus, RefreshCw, Workflow } from "lucide-react";
import { cn } from "../lib/utils";
import type { FolderSyncNotification, PersonalTask, PersonalTaskOptions, PersonalTaskReminder, ServiceRequestNotification, TrainingReminder, User, WorkflowNotification, WorkflowTask } from "../types";
import { formatDate, regionalSettings } from "../format";
import { createMonthGrid, groupCalendarItems, localDayKey } from "../workCalendar.mjs";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Input, PageHeader, SelectInput, Skeleton, Stat, Switch, Textarea } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

type WorkCalendarEntry = {
  id: string;
  target_id: string;
  target_kind: "personal" | "workflow" | "training";
  title: string;
  due_at: string | null;
  status: string;
  kind_label: string;
};

function when(value: string | null): string {
  if (!value) return "No due date";
  const date = new Date(value);
  return formatDate(date);
}

function reminderEmailLabel(status: PersonalTaskReminder["email_status"]): string {
  if (status === "sent") return "sent";
  if (status === "pending" || status === "sending") return "queued";
  if (status === "retry") return "retry scheduled";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "cancelled";
  return "disabled";
}

function workflowNoticeLabel(item: WorkflowNotification): string {
  if (item.notification_type === "approval_required") return "A workflow run needs an approval";
  if (item.notification_type === "run_approved") return "A workflow run was approved";
  if (item.notification_type === "run_rejected") return "A workflow run was rejected";
  return "A workflow task was assigned";
}

export function MyWorkPage({ user }: { user: User }) {
  const [personalTasks, setPersonalTasks] = useState<PersonalTask[]>([]);
  const [personalTaskReminders, setPersonalTaskReminders] = useState<PersonalTaskReminder[]>([]);
  const [taskOptions, setTaskOptions] = useState<PersonalTaskOptions>({ departments: [] });
  const [tasks, setTasks] = useState<WorkflowTask[]>([]);
  const [workflowNotices, setWorkflowNotices] = useState<WorkflowNotification[]>([]);
  const [requestNotices, setRequestNotices] = useState<ServiceRequestNotification[]>([]);
  const [trainingReminders, setTrainingReminders] = useState<TrainingReminder[]>([]);
  const [folderSyncNotices, setFolderSyncNotices] = useState<FolderSyncNotification[]>([]);
  const [folderSyncAlertsEnabled, setFolderSyncAlertsEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [taskTitle, setTaskTitle] = useState("");
  const [taskBody, setTaskBody] = useState("");
  const [taskDue, setTaskDue] = useState("");
  const [taskDepartmentId, setTaskDepartmentId] = useState("");
  const [taskAssigneeId, setTaskAssigneeId] = useState(user.id);
  const [calendarMonth, setCalendarMonth] = useState(() => {
    const today = new Date();
    return new Date(today.getFullYear(), today.getMonth(), 1);
  });
  const [selectedCalendarDay, setSelectedCalendarDay] = useState(() => localDayKey(new Date()));

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    const failures: string[] = [];
    const [personalResult, reminderResult, optionsResult, taskResult, workflowResult, requestResult, trainingResult, folderSyncResult, folderPreference] = await Promise.all([
      api.personalTasks().catch((err) => { failures.push(`Personal tasks: ${describeError(err)}`); return { tasks: [] as PersonalTask[] }; }),
      api.personalTaskReminders().catch((err) => { failures.push(`Assignment reminders: ${describeError(err)}`); return { reminders: [] as PersonalTaskReminder[] }; }),
      api.personalTaskOptions().catch((err) => { failures.push(`Assignment options: ${describeError(err)}`); return { departments: [] } as PersonalTaskOptions; }),
      api.automationTasks().catch((err) => { failures.push(`Tasks: ${describeError(err)}`); return { tasks: [] as WorkflowTask[] }; }),
      api.automationNotifications().catch((err) => { failures.push(`Workflow notices: ${describeError(err)}`); return { items: [] as WorkflowNotification[] }; }),
      api.serviceRequestNotifications().catch((err) => { failures.push(`Request notices: ${describeError(err)}`); return { items: [] as ServiceRequestNotification[] }; }),
      api.trainingReminders().catch((err) => { failures.push(`Training deadlines: ${describeError(err)}`); return { reminders: [] as TrainingReminder[] }; }),
      api.folderSyncNotifications().catch((err) => { failures.push(`Folder updates: ${describeError(err)}`); return { items: [] as FolderSyncNotification[] }; }),
      api.folderSyncPreference().catch((err) => { failures.push(`Folder alert settings: ${describeError(err)}`); return { change_alerts_enabled: true }; }),
    ]);
    setPersonalTasks(personalResult.tasks);
    setPersonalTaskReminders(reminderResult.reminders);
    setTaskOptions(optionsResult);
    setTasks(taskResult.tasks.filter((task) => task.assignee_user_id === user.id));
    setWorkflowNotices(workflowResult.items);
    setRequestNotices(requestResult.items);
    setTrainingReminders(trainingResult.reminders);
    setFolderSyncNotices(folderSyncResult.items);
    setFolderSyncAlertsEnabled(folderPreference.change_alerts_enabled);
    setError(failures.join(" "));
    setLoading(false);
  }, [user.id]);

  useEffect(() => { void refresh(); }, [refresh]);
  useLiveRefresh(() => refresh(true));

  const orderedPersonalTasks = useMemo(() => [...personalTasks].sort((a, b) => {
    if (a.status !== b.status) return a.status === "open" ? -1 : 1;
    if (a.due_at === null) return b.due_at === null ? 0 : 1;
    if (b.due_at === null) return -1;
    return a.due_at.localeCompare(b.due_at);
  }), [personalTasks]);
  const orderedTasks = useMemo(() => [...tasks].sort((a, b) => {
    if (a.status !== b.status) return a.status === "open" ? -1 : 1;
    if (a.due_at === null) return b.due_at === null ? 0 : 1;
    if (b.due_at === null) return -1;
    return a.due_at.localeCompare(b.due_at);
  }), [tasks]);
  const calendarEntries = useMemo<WorkCalendarEntry[]>(() => [
    ...orderedPersonalTasks.map((task) => ({
      id: `personal:${task.id}`,
      target_id: task.id,
      target_kind: "personal" as const,
      title: task.title,
      due_at: task.due_at,
      status: task.status,
      kind_label: task.department_group_id ? "Department assignment" : "Personal assignment",
    })),
    ...orderedTasks.map((task) => ({
      id: `workflow:${task.id}`,
      target_id: task.id,
      target_kind: "workflow" as const,
      title: task.title,
      due_at: task.due_at,
      status: task.status,
      kind_label: "Workflow task",
    })),
    ...trainingReminders.map((item) => ({
      id: `training:${item.id}`,
      target_id: item.id,
      target_kind: "training" as const,
      title: item.topic,
      due_at: item.due_at,
      status: item.reminder_type === "overdue" ? "overdue" : "due soon",
      kind_label: "Training deadline",
    })),
  ], [orderedPersonalTasks, orderedTasks, trainingReminders]);
  const calendarDays = useMemo(() => createMonthGrid(calendarMonth), [calendarMonth]);
  const calendarItemsByDay = useMemo(() => groupCalendarItems(calendarEntries), [calendarEntries]);
  const selectedCalendarEntries = calendarItemsByDay.get(selectedCalendarDay) ?? [];
  const unreadCount = workflowNotices.filter((item) => !item.read_at).length
    + requestNotices.filter((item) => !item.read_at).length
    + personalTaskReminders.filter((item) => !item.read_at).length
    + trainingReminders.filter((item) => !item.read_at).length
    + folderSyncNotices.filter((item) => !item.read_at).length;
  const overdueTasks = orderedPersonalTasks.filter((task) => task.status === "open" && task.due_at && new Date(task.due_at).valueOf() < Date.now()).length;
  const selectedDepartment = taskOptions.departments.find((department) => department.id === taskDepartmentId);

  function shiftCalendarMonth(amount: number) {
    const next = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + amount, 1);
    setCalendarMonth(next);
    setSelectedCalendarDay(localDayKey(next));
  }

  function showCurrentCalendarMonth() {
    const today = new Date();
    setCalendarMonth(new Date(today.getFullYear(), today.getMonth(), 1));
    setSelectedCalendarDay(localDayKey(today));
  }

  function scrollToCalendarItem(item: WorkCalendarEntry) {
    const targetId = item.target_kind === "personal"
      ? `personal-task-${item.target_id}`
      : item.target_kind === "workflow" ? `workflow-task-${item.target_id}` : null;
    if (targetId) document.getElementById(targetId)?.scrollIntoView({ behavior: "smooth", block: "center" });
    else window.location.hash = "/training";
  }

  async function completeTask(task: WorkflowTask) {
    setBusyId(task.id);
    setError("");
    try {
      const updated = await api.completeAutomationTask(task.id);
      setTasks((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusyId(null);
    }
  }

  async function createPersonalTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusyId("create-personal-task");
    setError("");
    try {
      const assignedTo = taskDepartmentId ? (taskAssigneeId || user.id) : user.id;
      const created = await api.createPersonalTask({
        title: taskTitle,
        body: taskBody,
        assignee_user_id: assignedTo,
        department_group_id: taskDepartmentId || null,
        due_at: taskDue ? new Date(taskDue).toISOString() : null,
      });
      setPersonalTasks((current) => [created, ...current.filter((task) => task.id !== created.id)]);
      setTaskTitle("");
      setTaskBody("");
      setTaskDue("");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusyId(null);
    }
  }

  async function completePersonalTask(task: PersonalTask) {
    setBusyId(task.id);
    setError("");
    try {
      const updated = await api.completePersonalTask(task.id);
      setPersonalTasks((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusyId(null);
    }
  }

  async function cancelPersonalTask(task: PersonalTask) {
    setBusyId(task.id);
    setError("");
    try {
      const updated = await api.cancelPersonalTask(task.id);
      setPersonalTasks((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusyId(null);
    }
  }

  async function markWorkflowRead(item: WorkflowNotification) {
    setBusyId(item.id);
    setError("");
    try {
      const updated = await api.readAutomationNotification(item.id);
      setWorkflowNotices((current) => current.map((notice) => notice.id === updated.id ? updated : notice));
    } catch (err) { setError(describeError(err)); }
    finally { setBusyId(null); }
  }

  async function markRequestRead(item: ServiceRequestNotification) {
    setBusyId(item.id);
    setError("");
    try {
      const updated = await api.readServiceRequestNotification(item.id);
      setRequestNotices((current) => current.map((notice) => notice.id === updated.id ? updated : notice));
    } catch (err) { setError(describeError(err)); }
    finally { setBusyId(null); }
  }

  async function markTrainingRead(item: TrainingReminder) {
    setBusyId(item.id);
    setError("");
    try {
      const updated = await api.readTrainingReminder(item.id);
      setTrainingReminders((current) => current.map((reminder) => reminder.id === updated.id ? updated : reminder));
    } catch (err) { setError(describeError(err)); }
    finally { setBusyId(null); }
  }

  async function markPersonalTaskReminderRead(item: PersonalTaskReminder) {
    setBusyId(item.id);
    setError("");
    try {
      const updated = await api.readPersonalTaskReminder(item.id);
      setPersonalTaskReminders((current) => current.map((reminder) => reminder.id === updated.id ? updated : reminder));
    } catch (err) { setError(describeError(err)); }
    finally { setBusyId(null); }
  }

  async function markFolderSyncRead(item: FolderSyncNotification) {
    setBusyId(item.id);
    setError("");
    try {
      const updated = await api.readFolderSyncNotification(item.id);
      setFolderSyncNotices((current) => current.map((notice) => notice.id === updated.id ? updated : notice));
    } catch (err) { setError(describeError(err)); }
    finally { setBusyId(null); }
  }

  async function updateFolderSyncPreference(enabled: boolean) {
    setBusyId("folder-sync-preference");
    setError("");
    try {
      const saved = await api.updateFolderSyncPreference(enabled);
      setFolderSyncAlertsEnabled(saved.change_alerts_enabled);
    } catch (err) { setError(describeError(err)); }
    finally { setBusyId(null); }
  }

  const openPersonal = orderedPersonalTasks.filter((task) => task.status === "open").length;
  const openWorkflow = orderedTasks.filter((task) => task.status === "open").length;
  const todayKey = localDayKey(new Date());

  return <>
    <PageHeader
      title="My work"
      description="Everything that needs you: assignments, workflow tasks, deadlines and updates, in one place."
      actions={<Button onClick={() => void refresh()} loading={loading}>{!loading && <RefreshCw />} Refresh</Button>}
    />
    <div className="my-work-summary mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4" role="status">
      <Stat icon={<ListTodo />} label="Open assignments" value={openPersonal} />
      <Stat icon={<AlarmClock />} label="Overdue" value={<span className={overdueTasks ? "text-danger-text" : undefined}>{overdueTasks}</span>} />
      <Stat icon={<Workflow />} label="Open workflow tasks" value={openWorkflow} />
      <Stat icon={<Bell />} label="Unread updates" value={unreadCount} />
    </div>
    {error && <Alert tone="danger" className="mb-5">{error}</Alert>}
    {loading ? <div className="grid gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]"><Skeleton className="h-96" /><Skeleton className="h-96" /></div> : (
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <div className="grid min-w-0 gap-5">
          <Card className="overflow-hidden" aria-label="Personal assignments">
            <CardHeader icon={<ClipboardList />} title="Assignments" description="Create a reminder for yourself, or assign a task to someone in a department you manage." actions={<Badge>{orderedPersonalTasks.length}</Badge>} />
            <CardBody className="grid gap-4">
              <form className="personal-task-form grid gap-3 rounded-lg border border-border bg-surface-2/40 p-4 sm:grid-cols-2" onSubmit={(event) => void createPersonalTask(event)}>
                <label className={cn(labelClass, "sm:col-span-2")}>Task title<Input value={taskTitle} onChange={(event) => setTaskTitle(event.target.value)} maxLength={180} placeholder="What needs to be done?" required /></label>
                <label className={cn(labelClass, "sm:col-span-2")}>Description<Textarea value={taskBody} onChange={(event) => setTaskBody(event.target.value)} maxLength={4000} rows={2} placeholder="Details, links or steps" required /></label>
                <label className={labelClass}>Deadline (optional)<Input type="datetime-local" value={taskDue} onChange={(event) => setTaskDue(event.target.value)} /></label>
                {taskOptions.departments.length > 0 && <label className={labelClass}>Department<SelectInput value={taskDepartmentId} onChange={(event) => {
                  const nextDepartmentId = event.target.value;
                  const nextDepartment = taskOptions.departments.find((department) => department.id === nextDepartmentId);
                  setTaskDepartmentId(nextDepartmentId);
                  setTaskAssigneeId(nextDepartment?.members[0]?.user_id ?? user.id);
                }}>
                  <option value="">My personal task</option>
                  {taskOptions.departments.map((department) => <option value={department.id} key={department.id}>{department.name}</option>)}
                </SelectInput></label>}
                {selectedDepartment && <label className={labelClass}>Assign to<SelectInput value={taskAssigneeId} onChange={(event) => setTaskAssigneeId(event.target.value)}>
                  <option value={user.id}>Me ({user.email})</option>
                  {selectedDepartment.members.map((member) => <option value={member.user_id} key={member.user_id}>{member.email}</option>)}
                </SelectInput></label>}
                <div className="flex justify-end sm:col-span-2"><Button type="submit" variant="primary" loading={busyId === "create-personal-task"}>{busyId !== "create-personal-task" && <Plus />} {busyId === "create-personal-task" ? "Saving…" : "Create task"}</Button></div>
              </form>
              {orderedPersonalTasks.length === 0 ? <EmptyState icon={<ListTodo />} title="No assignments yet" description="Tasks you create or are given will appear here." /> : <ul className="grid">
                {orderedPersonalTasks.map((task) => {
                  const overdue = task.status === "open" && task.due_at !== null && new Date(task.due_at).valueOf() < Date.now();
                  return <li className={itemClass} id={`personal-task-${task.id}`} key={task.id}>
                    <div className="my-work-copy grid min-w-0 gap-1">
                      <div className="flex flex-wrap items-center gap-2"><strong className="text-sm font-medium [overflow-wrap:anywhere]">{task.title}</strong><Badge tone={task.status === "completed" ? "success" : task.status === "cancelled" ? "neutral" : overdue ? "danger" : "warning"} dot>{overdue ? "overdue" : task.status}</Badge></div>
                      <p className="text-[13px] whitespace-pre-wrap text-foreground-2 [overflow-wrap:anywhere]">{task.body}</p>
                      <small className="text-xs text-muted [overflow-wrap:anywhere]">{task.department_group_id ? "Department assignment" : "Personal"} · For {task.assignee_email} · By {task.created_by_email} · Due {when(task.due_at)}</small>
                    </div>
                    <div className={actionsClass}>
                      {task.status === "open" && task.assignee_user_id === user.id && <Button size="sm" variant="primary" loading={busyId === task.id} onClick={() => void completePersonalTask(task)}>{busyId !== task.id && <Check />} Complete</Button>}
                      {task.status === "open" && (task.created_by_user_id === user.id || task.assignee_user_id === user.id) && <Button size="sm" variant="ghost" disabled={busyId === task.id} onClick={() => void cancelPersonalTask(task)}>Cancel</Button>}
                    </div>
                  </li>;
                })}
              </ul>}
            </CardBody>
          </Card>

          <Card className="overflow-hidden" aria-label="Assigned workflow tasks">
            <CardHeader icon={<Workflow />} title="Workflow tasks" description="Steps from automated workflows that are assigned to you." actions={<Badge>{orderedTasks.length}</Badge>} />
            <CardBody>
              {orderedTasks.length === 0 ? <EmptyState className="py-8" icon={<Workflow />} title="Nothing assigned" description="When a workflow hands you a step, it shows up here." /> : <ul className="grid">
                {orderedTasks.map((task) => <li className={itemClass} id={`workflow-task-${task.id}`} key={task.id}>
                  <div className="my-work-copy grid min-w-0 gap-1">
                    <div className="flex flex-wrap items-center gap-2"><strong className="text-sm font-medium [overflow-wrap:anywhere]">{task.title}</strong><Badge tone={task.status === "completed" ? "success" : "warning"} dot>{task.status}</Badge></div>
                    <p className="text-[13px] whitespace-pre-wrap text-foreground-2 [overflow-wrap:anywhere]">{task.body}</p>
                    <small className="text-xs text-muted">Due {when(task.due_at)} · Created {when(task.created_at)}</small>
                  </div>
                  {task.status === "open" && <div className={actionsClass}><Button size="sm" variant="primary" loading={busyId === task.id} onClick={() => void completeTask(task)}>{busyId !== task.id && <Check />} Complete task</Button></div>}
                </li>)}
              </ul>}
            </CardBody>
          </Card>
        </div>

        <div className="grid min-w-0 gap-5">
          <Card className="my-work-calendar overflow-hidden" aria-label="Work calendar">
            <CardHeader
              icon={<CalendarDays />}
              title="Due dates"
              description="Assignments, workflow tasks and training deadlines."
              actions={<div className="work-calendar-controls flex items-center gap-1" aria-label="Calendar navigation">
                <Button size="icon-sm" variant="ghost" aria-label="Previous month" onClick={() => shiftCalendarMonth(-1)}><ChevronLeft /></Button>
                <strong className="min-w-28 text-center text-[13px] font-semibold" aria-live="polite">{calendarMonth.toLocaleDateString(regionalSettings().locale, { month: "long", year: "numeric" })}</strong>
                <Button size="icon-sm" variant="ghost" aria-label="Next month" onClick={() => shiftCalendarMonth(1)}><ChevronRight /></Button>
                <Button size="sm" variant="ghost" onClick={showCurrentCalendarMonth}>Today</Button>
              </div>}
            />
            <CardBody className="grid gap-4">
              <div className="grid grid-cols-7 gap-1" role="group" aria-label="Due dates by day">
                {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day) => <span className="py-1 text-center text-xs font-semibold text-muted" key={day}>{day}</span>)}
                {calendarDays.map((day) => {
                  const key = localDayKey(day);
                  const entries = calendarItemsByDay.get(key) ?? [];
                  const isCurrentMonth = day.getMonth() === calendarMonth.getMonth();
                  const isToday = key === todayKey;
                  return <button
                    className={cn(
                      "flex min-h-14 min-w-0 cursor-pointer flex-col items-start justify-between gap-1 rounded-md border border-border bg-surface p-1.5 text-left text-xs outline-none transition-colors hover:border-primary focus-visible:ring-[3px] focus-visible:ring-ring aria-pressed:border-primary aria-pressed:bg-primary-soft",
                      !isCurrentMonth && "text-muted opacity-60",
                    )}
                    type="button"
                    aria-label={`${day.toLocaleDateString(regionalSettings().locale, { weekday: "long", month: "long", day: "numeric", year: "numeric" })}, ${entries.length} due ${entries.length === 1 ? "item" : "items"}`}
                    aria-pressed={selectedCalendarDay === key}
                    aria-current={isToday ? "date" : undefined}
                    key={key}
                    onClick={() => setSelectedCalendarDay(key)}
                  >
                    <span className={cn("font-medium", isToday && "grid size-5 place-items-center rounded-full bg-primary text-xs text-primary-foreground")}>{day.getDate()}</span>
                    {entries.length > 0 && <span className="work-calendar-count max-w-full truncate rounded-full bg-warning-soft px-1.5 text-xs font-medium text-warning-text">{entries.length} due</span>}
                  </button>;
                })}
              </div>
              <div className="work-calendar-agenda border-t border-border pt-3" aria-live="polite">
                <h3 className="mb-1 text-[13px] font-semibold">{new Date(`${selectedCalendarDay}T12:00:00`).toLocaleDateString(regionalSettings().locale, { weekday: "long", month: "long", day: "numeric" })}</h3>
                {selectedCalendarEntries.length === 0 ? <p className="text-xs text-muted">Nothing due on this day.</p> : <ul className="grid">
                  {selectedCalendarEntries.map((item) => <li className="flex flex-wrap items-center justify-between gap-2 border-t border-border py-2 first:border-t-0" key={item.id}>
                    <div className="grid min-w-0 gap-0.5"><strong className="text-[13px] font-medium [overflow-wrap:anywhere]">{item.title}</strong><small className="text-xs text-muted">{item.kind_label} · {item.status} · {when(item.due_at)}</small></div>
                    <Button size="sm" variant="ghost" onClick={() => scrollToCalendarItem(item)}>{item.target_kind === "training" ? "Open training" : "Show"}</Button>
                  </li>)}
                </ul>}
              </div>
            </CardBody>
          </Card>

          <Card className="overflow-hidden" aria-label="Updates">
            <CardHeader icon={<Bell />} title="Updates" description="Approvals, request deadlines, reminders and folder changes. Read receipts are saved." actions={unreadCount > 0 ? <Badge tone="primary">{unreadCount} unread</Badge> : <Badge tone="success">All read</Badge>} />
            <CardBody className="grid gap-5">
              <UpdateGroup icon={<Workflow />} title="Workflow updates" count={workflowNotices.length} empty="No workflow updates.">
                {workflowNotices.map((item) => <li className={itemClass} key={item.id}>
                  <div className="my-work-copy grid min-w-0 gap-0.5"><strong className="text-[13px] font-medium">{workflowNoticeLabel(item)}</strong><small className="text-xs text-muted">Run {item.run_id.slice(0, 8)} · {when(item.created_at)}{item.read_at ? " · Read" : " · Unread"}</small></div>
                  <div className={actionsClass}>{item.notification_type === "approval_required" && <Button size="sm" variant="ghost" onClick={() => { window.location.hash = "/automation"; }}>Review workflow</Button>}{!item.read_at && <Button size="sm" loading={busyId === item.id} onClick={() => void markWorkflowRead(item)}>Mark read</Button>}</div>
                </li>)}
              </UpdateGroup>
              <UpdateGroup icon={<LifeBuoy />} title="Help request updates" count={requestNotices.length} empty="No help request updates.">
                {requestNotices.map((item) => <li className={itemClass} key={item.id}>
                  <div className="my-work-copy grid min-w-0 gap-0.5"><strong className="text-[13px] font-medium">Response deadline alert</strong><small className="text-xs text-muted">Request {item.request_id.slice(0, 8)} · {when(item.created_at)}{item.read_at ? " · Read" : " · Unread"}</small></div>
                  <div className={actionsClass}><Button size="sm" variant="ghost" onClick={() => { window.location.hash = "/requests"; }}>Open requests</Button>{!item.read_at && <Button size="sm" loading={busyId === item.id} onClick={() => void markRequestRead(item)}>Mark read</Button>}</div>
                </li>)}
              </UpdateGroup>
              <UpdateGroup icon={<AlarmClock />} title="Assignment deadlines" hint="Reminders up to three days before a due date and again when overdue. Optional emails contain no task details." count={personalTaskReminders.length} empty="No assignment deadline reminders.">
                {personalTaskReminders.map((item) => <li className={itemClass} key={item.id}>
                  <div className="my-work-copy grid min-w-0 gap-0.5"><strong className="text-[13px] font-medium [overflow-wrap:anywhere]">{item.title}</strong><small className="text-xs text-muted">{item.reminder_type === "overdue" ? "Overdue" : "Due soon"} · Due {when(item.due_at)}{item.read_at ? " · Read" : " · Unread"} · Email {reminderEmailLabel(item.email_status)}</small></div>
                  <div className={actionsClass}><Button size="sm" variant="ghost" onClick={() => document.getElementById(`personal-task-${item.task_id}`)?.scrollIntoView({ behavior: "smooth", block: "center" })}>Open assignment</Button>{!item.read_at && <Button size="sm" loading={busyId === item.id} onClick={() => void markPersonalTaskReminderRead(item)}>Mark read</Button>}</div>
                </li>)}
              </UpdateGroup>
              <UpdateGroup icon={<GraduationCap />} title="Training deadlines" count={trainingReminders.length} empty="No training deadlines.">
                {trainingReminders.map((item) => <li className={itemClass} key={item.id}>
                  <div className="my-work-copy grid min-w-0 gap-0.5"><strong className="text-[13px] font-medium [overflow-wrap:anywhere]">{item.topic}</strong><small className="text-xs text-muted">{item.reminder_type === "overdue" ? "Overdue" : "Due soon"} · Due {when(item.due_at)}{item.read_at ? " · Read" : " · Unread"}</small></div>
                  <div className={actionsClass}><Button size="sm" variant="ghost" onClick={() => { window.location.hash = "/training"; }}>Open training</Button>{!item.read_at && <Button size="sm" loading={busyId === item.id} onClick={() => void markTrainingRead(item)}>Mark read</Button>}</div>
                </li>)}
              </UpdateGroup>
              <UpdateGroup
                icon={<FolderSync />}
                title="Knowledge source updates"
                hint="Change summaries for folders you administer; removed files are quarantined until restored."
                count={folderSyncNotices.length}
                empty="No folder source updates."
                extra={<label className="flex items-center gap-2 text-xs text-foreground-2"><Switch checked={folderSyncAlertsEnabled} disabled={busyId === "folder-sync-preference"} onCheckedChange={(checked) => void updateFolderSyncPreference(checked)} aria-label="Receive folder change and failure alerts" /> Alerts</label>}
              >
                {folderSyncNotices.map((item) => <li className={itemClass} key={item.id}>
                  <div className="my-work-copy grid min-w-0 gap-0.5"><strong className="text-[13px] font-medium [overflow-wrap:anywhere]">{item.source_name}: {item.status === "failed" ? "sync failed" : item.status === "partial" ? "sync completed with file errors" : "sync completed"}</strong>
                    <small className="text-xs text-muted">Added {item.created} · Updated {item.updated} · Removed {item.removed} · Failed {item.failed} · {when(item.created_at)}{item.read_at ? " · Read" : " · Unread"}{item.error_code ? ` · ${item.error_code}` : ""}</small></div>
                  {!item.read_at && <div className={actionsClass}><Button size="sm" loading={busyId === item.id} onClick={() => void markFolderSyncRead(item)}>Mark read</Button></div>}
                </li>)}
              </UpdateGroup>
            </CardBody>
          </Card>
        </div>
      </div>
    )}
  </>;
}

const labelClass = "grid min-w-0 gap-1.5 text-xs font-medium text-foreground-2";
const itemClass = "my-work-item flex flex-col gap-2 border-t border-border py-3 first:border-t-0 sm:flex-row sm:items-center sm:justify-between";
const actionsClass = "flex shrink-0 flex-wrap items-center gap-2";

function UpdateGroup({ icon, title, hint, count, empty, extra, children }: {
  icon: ReactNode; title: string; hint?: string; count: number; empty: string; extra?: ReactNode; children: ReactNode;
}) {
  return (
    <section aria-label={title}>
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <h4 className="flex items-center gap-2 text-[13px] font-semibold text-foreground [&_svg]:size-4 [&_svg]:text-muted">{icon}{title}<Badge>{count}</Badge></h4>
        {extra}
      </div>
      {hint && <p className="mb-1 text-xs text-muted">{hint}</p>}
      {count === 0 ? <p className="py-2 text-xs text-muted">{empty}</p> : <ul className="grid">{children}</ul>}
    </section>
  );
}
