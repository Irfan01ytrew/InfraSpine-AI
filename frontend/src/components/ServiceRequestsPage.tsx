import { useCallback, useEffect, useState } from "react";
import { api, describeError } from "../api";
import { formatDate, formatMoney, regionalSettings } from "../format";
import type { DepartmentGroup, ManagedUser, ServiceRequestCategory, ServiceRequestItem, ServiceRequestNotification, ServiceRequestStatus, User } from "../types";
import { useFeedback } from "./Feedback";
import { ApprovalDelegationCard } from "./ApprovalDelegationCard";
import { RequestFormFields, answersForApi, formComplete, type FormAnswers } from "./RequestFormFields";
import type { RequestForm } from "../types";
import { AlarmClock, Paperclip, Bell, Check, CheckCircle2, Circle, CircleDot, Inbox, LifeBuoy, Lock, MessageSquare, RefreshCw, Send, X } from "lucide-react";
import { cn } from "../lib/utils";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, Select, SelectInput, Skeleton, Stat, Tabs, Textarea } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

const categories: Array<{ value: ServiceRequestCategory; label: string }> = [
  { value: "it", label: "IT support" },
  { value: "hr", label: "HR request" },
  { value: "access", label: "Account or access" },
  { value: "facilities", label: "Facilities" },
  { value: "purchase", label: "Purchase request" },
  { value: "expense", label: "Expense claim" },
  { value: "other", label: "Other" },
];
const workflowStatuses: Array<{ value: Exclude<ServiceRequestStatus, "submitted" | "cancelled">; label: string }> = [
  { value: "acknowledged", label: "Acknowledged" },
  { value: "in_progress", label: "In progress" },
  { value: "waiting_requester", label: "Waiting for requester" },
  { value: "resolved", label: "Resolved" },
  { value: "rejected", label: "Rejected" },
];

const NOTIFICATION_TITLES: Record<ServiceRequestNotification["notification_type"], string> = {
  response_sla_escalated: "Initial response deadline passed",
  approval_step_pending: "Approval step waiting for you",
  approval_step_reminder: "Reminder: approval step still waiting",
  approval_step_escalated: "Escalated: an approval step is overdue",
};

function label(value: string) {
  return value.replaceAll("_", " ");
}

export function ServiceRequestsPage({ user }: { user: User }) {
  const { toast } = useFeedback();
  const [departments, setDepartments] = useState<DepartmentGroup[]>([]);
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [items, setItems] = useState<ServiceRequestItem[] | null>(null);
  const [notifications, setNotifications] = useState<ServiceRequestNotification[]>([]);
  const [category, setCategory] = useState<ServiceRequestCategory>("it");
  const [priority, setPriority] = useState<"low" | "normal" | "high" | "urgent">("normal");
  const [departmentId, setDepartmentId] = useState("");
  const [subject, setSubject] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState(() => regionalSettings().currency ?? "");
  const [description, setDescription] = useState("");
  const [forms, setForms] = useState<RequestForm[]>([]);
  const [formId, setFormId] = useState("");
  const [answers, setAnswers] = useState<FormAnswers>({});
  const [comments, setComments] = useState<Record<string, string>>({});
  const [triage, setTriage] = useState<Record<string, { status: Exclude<ServiceRequestStatus, "submitted" | "cancelled">; assignee: string; comment: string }>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"open" | "closed" | "all">("open");

  const load = useCallback(async () => {
    try {
      const [departmentResult, requestResult, notificationResult] = await Promise.all([
        api.myDepartments(), api.serviceRequests(), api.serviceRequestNotifications(),
      ]);
      setDepartments(departmentResult.groups.filter((group) => group.kind === "department"));
      setItems(requestResult.items);
      setNotifications(notificationResult.items);
      api.requestForms().then((result) => setForms(result.forms)).catch(() => setForms([]));
      if (user.role === "admin") {
        setUsers((await api.adminUsers()).users.filter((candidate) => candidate.is_active));
      } else if (user.role === "manager") {
        const managed = await api.managedDepartments();
        const members = await Promise.all(managed.groups.map((group) => api.departmentMembers(group.id)));
        const unique = new Map<string, ManagedUser>();
        for (let index = 0; index < members.length; index++) for (const member of members[index].members) {
          const candidate = unique.get(member.user_id) ?? {
            id: member.user_id, email: member.email, role: "employee", is_active: true,
            created_at: "", departments: [],
          };
          candidate.departments.push({ id: managed.groups[index].id, name: managed.groups[index].name, manager: false });
          unique.set(member.user_id, candidate);
        }
        setUsers([...unique.values()]);
      } else {
        setUsers([]);
      }
      setDepartmentId((current) => current || departmentResult.groups[0]?.id || "");
      setError(null);
    } catch (err) {
      setError(describeError(err));
    }
  }, [user.role]);

  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load);

  const isAdmin = user.role === "admin";
  function usersFor(item: ServiceRequestItem) {
    if (item.category === "hr") return isAdmin ? users.filter((candidate) => candidate.role === "admin") : [];
    if (item.department_group_id === null) return isAdmin ? users.filter((candidate) => candidate.role === "admin") : [];
    return users.filter((candidate) => candidate.role === "admin" || candidate.departments.some((department) => department.id === item.department_group_id));
  }

  const form = forms.find((candidate) => candidate.id === formId) ?? null;
  const effectiveCategory = form ? form.category : category;
  const financial = isFinancial(effectiveCategory);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.createServiceRequest({
        category: effectiveCategory, priority, department_group_id: form?.department_group_id ? null : departmentId || null,
        subject: subject.trim(), description: description.trim(),
        ...(financial ? { amount: amount.trim(), currency: currency.trim().toUpperCase() } : {}),
        ...(form ? { form_id: form.id, answers: answersForApi(form.fields, answers) } : {}),
      });
      setSubject(""); setDescription(""); setAmount(""); setAnswers({});
      toast("success", "Request submitted", effectiveCategory === "hr" ? "Your HR request is private to you and administrators." : "Your request is in the human support queue.");
      await load();
    } catch (err) { toast("error", "Request was not submitted", describeError(err)); }
    finally { setBusy(false); }
  }

  async function addComment(item: ServiceRequestItem) {
    const comment = (comments[item.id] ?? "").trim();
    if (!comment) return;
    try {
      await api.commentServiceRequest(item.id, comment);
      setComments((current) => ({ ...current, [item.id]: "" }));
      toast("success", "Update added to request history");
      await load();
    } catch (err) { toast("error", "Could not add update", describeError(err)); }
  }

  async function updateRequest(item: ServiceRequestItem) {
    const state = triage[item.id] ?? {
      status: "acknowledged" as const,
      assignee: item.assigned_to_user_id ?? "",
      comment: "",
    };
    setBusy(true);
    try {
      await api.triageServiceRequest(item.id, {
        status: state.status,
        assigned_to_user_id: state.assignee || null,
        comment: state.comment.trim() || null,
      });
      setTriage((current) => ({ ...current, [item.id]: { ...state, comment: "" } }));
      toast("success", "Request updated", "The status and action were added to its permanent history.");
      await load();
    } catch (err) { toast("error", "Could not update request", describeError(err)); }
    finally { setBusy(false); }
  }

  async function cancel(item: ServiceRequestItem) {
    try {
      await api.cancelServiceRequest(item.id);
      toast("success", "Request cancelled");
      await load();
    } catch (err) { toast("error", "Could not cancel request", describeError(err)); }
  }

  async function markNotificationRead(notification: ServiceRequestNotification) {
    try {
      const updated = await api.readServiceRequestNotification(notification.id);
      setNotifications((current) => current.map((item) =>
        item.id === notification.id ? updated : item,
      ));
    } catch (err) { toast("error", "Could not update notification", describeError(err)); }
  }

  const all = items ?? [];
  const openItems = all.filter((item) => !closedStatuses.includes(item.status));
  const visible = filter === "open" ? openItems : filter === "closed" ? all.filter((item) => closedStatuses.includes(item.status)) : all;
  const overdueCount = openItems.filter((item) => item.response_sla_overdue).length;
  const unreadAlerts = notifications.filter((item) => !item.read_at).length;

  return <>
    <PageHeader
      title="Help & requests"
      description="Ask IT, HR, facilities or any team for help, follow the status, and keep every decision on record."
      actions={<Button disabled={busy} onClick={() => void load()}><RefreshCw /> Refresh</Button>}
    />
    <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Stat icon={<Inbox />} label="Open requests" value={items === null ? "…" : openItems.length} />
      <Stat icon={<AlarmClock />} label="Response overdue" value={<span className={overdueCount ? "text-danger-text" : undefined}>{items === null ? "…" : overdueCount}</span>} />
      <Stat icon={<CheckCircle2 />} label="Resolved" value={items === null ? "…" : all.filter((item) => item.status === "resolved").length} />
      <Stat icon={<Bell />} label="Unread alerts" value={unreadAlerts} />
    </div>
    {error && <Alert tone="danger" className="mb-5">{error}</Alert>}

    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,380px)_minmax(0,1fr)]">
      <div className="grid min-w-0 gap-5">
        <Card className="overflow-hidden">
          <CardHeader icon={<LifeBuoy />} title="New request" description="Reviewed by people. HR requests are visible only to you and administrators." />
          <CardBody className="grid gap-3">
            <form className="service-request-form grid gap-3" onSubmit={(event) => void submit(event)}>
              {forms.length > 0 && (
                <label className={labelClass}><span className="field-label text-xs font-medium">Form</span>
                  <SelectInput aria-label="Request form" value={formId} onChange={(event) => { setFormId(event.target.value); setAnswers({}); }}>
                    <option value="">General request</option>
                    {forms.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.name}</option>)}
                  </SelectInput>
                </label>
              )}
              {form?.description && <p className="text-xs text-muted">{form.description}</p>}
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
                {!form && <label className={labelClass}><span className="field-label text-xs font-medium">Category</span><SelectInput value={category} onChange={(event) => setCategory(event.target.value as ServiceRequestCategory)}>{categories.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</SelectInput></label>}
                <label className={labelClass}><span className="field-label text-xs font-medium">Priority</span><SelectInput value={priority} onChange={(event) => setPriority(event.target.value as typeof priority)}><option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option><option value="urgent">Urgent</option></SelectInput></label>
                {form?.department_name ? <p className={cn("text-xs text-muted sm:col-span-2 xl:col-span-1")}>Goes to <strong className="text-foreground-2">{form.department_name}</strong></p>
                  : <label className={cn(labelClass, "sm:col-span-2 xl:col-span-1")}><span className="field-label text-xs font-medium">Department (optional)</span><SelectInput value={departmentId} onChange={(event) => setDepartmentId(event.target.value)}><option value="">No department</option>{departments.map((department) => <option key={department.id} value={department.id}>{department.name}</option>)}</SelectInput></label>}
              </div>
              {financial && <div className="grid grid-cols-[minmax(0,1fr)_90px] gap-3">
                <Field label="Amount"><Input inputMode="decimal" required value={amount} onChange={(event) => setAmount(event.target.value.replace(/[^0-9.]/g, ""))} placeholder="e.g. 45000" /></Field>
                <Field label="Currency"><Input required maxLength={3} value={currency} onChange={(event) => setCurrency(event.target.value.toUpperCase().replace(/[^A-Z]/g, ""))} placeholder="PKR" /></Field>
                <p className="col-span-2 text-xs text-muted">Approved only by someone whose approval limit covers this amount; you cannot approve your own request.</p>
              </div>}
              <label className={labelClass}><span className="field-label text-xs font-medium">Subject</span><Input required maxLength={160} value={subject} onChange={(event) => setSubject(event.target.value)} placeholder="Summarize the help you need" /></label>
              {form && <RequestFormFields idPrefix="new-request" fields={form.fields} answers={answers} onChange={setAnswers} />}
              <label className={labelClass}><span className="field-label text-xs font-medium">{form ? "Anything else (optional)" : "Details"}</span><Textarea required={!form} maxLength={8000} rows={form ? 2 : 4} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Explain the issue and what outcome would help." /></label>
              <Button type="submit" variant="primary" loading={busy} disabled={busy || !subject.trim() || (form ? !formComplete(form.fields, answers) : !description.trim()) || (financial && (!Number(amount) || currency.length !== 3))}>{!busy && <Send />} Submit request</Button>
            </form>
            <p className="flex items-start gap-1.5 text-xs text-muted"><Lock className="mt-0.5 size-3.5 shrink-0" /> Never include passwords, API keys or other secrets in a request.</p>
          </CardBody>
        </Card>

        <ApprovalDelegationCard user={user} onChanged={() => void load()} />

        {notifications.length > 0 && (
          <Card className="overflow-hidden" aria-label="Service request notifications">
            <CardHeader icon={<Bell />} title="Request alerts" description="Alerts contain no request text. Access is checked again when you open one." actions={unreadAlerts > 0 ? <Badge tone="primary">{unreadAlerts} unread</Badge> : undefined} />
            <CardBody className="grid">
              <ul className="service-request-alerts grid">{notifications.map((notification) => (
                <li className="flex flex-wrap items-center justify-between gap-2 border-t border-border py-2.5 first:border-t-0" key={notification.id}>
                  <span className="grid min-w-0 gap-0.5"><strong className="text-[13px] font-medium">{NOTIFICATION_TITLES[notification.notification_type] ?? "Request alert"}</strong><small className="text-xs text-muted">Request {notification.request_id.slice(0, 8)} · {formatDate(notification.created_at)}{notification.read_at ? " · Read" : " · Unread"}</small></span>
                  {!notification.read_at && <Button size="sm" variant="ghost" onClick={() => void markNotificationRead(notification)}>Mark read</Button>}
                </li>
              ))}</ul>
            </CardBody>
          </Card>
        )}
      </div>

      <Card className="min-w-0 overflow-hidden">
        <CardHeader
          icon={<Inbox />}
          title="Requests"
          description="Only requests you may read are shown. Status changes and comments are kept permanently."
          actions={<Tabs value={filter} onValueChange={(value) => setFilter(value as typeof filter)} items={[
            { value: "open", label: "Open", count: openItems.length },
            { value: "closed", label: "Closed", count: all.length - openItems.length },
            { value: "all", label: "All", count: all.length },
          ]} />}
        />
        {items === null ? <CardBody className="grid gap-3"><Skeleton className="h-28" /><Skeleton className="h-28" /></CardBody> : visible.length === 0 ? (
          <EmptyState icon={<Inbox />} title={filter === "open" ? "No open requests" : "No requests here"} description={filter === "open" ? "Everything is handled. New requests you submit or can manage appear here." : "Try another filter."} />
        ) : <div className="service-request-list grid gap-3 p-4">{visible.map((item) => {
          const state = triage[item.id] ?? { status: "acknowledged" as const, assignee: item.assigned_to_user_id ?? "", comment: "" };
          const options = usersFor(item);
          const noteRequired = state.status === "resolved" || state.status === "rejected";
          return <article className="service-request-item grid gap-3 rounded-lg border border-border p-4" key={item.id}>
            <div className="grid gap-1.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone={statusTone(item.status)} dot>{label(item.status)}</Badge>
                <Badge>{categories.find((option) => option.value === item.category)?.label ?? item.category}</Badge>
                <Badge tone={item.priority === "urgent" ? "danger" : item.priority === "high" ? "warning" : "neutral"}>{item.priority}</Badge>
                {item.response_sla_overdue && <Badge tone="danger">Response overdue</Badge>}
                {item.sla_escalated_at && <Badge tone="warning">Escalated</Badge>}
                {item.department_name && <Badge>{item.department_name}</Badge>}
                {item.amount && <Badge tone="primary">{formatMoney(Number(item.amount), item.currency)}</Badge>}
              </div>
              <h3 className="text-sm font-semibold text-foreground [overflow-wrap:anywhere]">{item.subject}</h3>
              <p className="text-xs text-muted">By {item.requester_email} · {formatDate(item.created_at)}{item.assigned_to_email ? ` · Assigned to ${item.assigned_to_email}` : " · Unassigned"}</p>
              <p className="text-xs text-muted">Response target {formatDate(item.response_due_at)}{item.response_sla_met_at ? ` · met ${formatDate(item.response_sla_met_at)}` : ""}</p>
            </div>
            {item.form_answers && item.form_answers.length > 0 ? (
              <div className="grid gap-1.5 rounded-md border border-border px-3 py-2" aria-label="Form answers">
                <p className="text-xs font-semibold text-foreground-2">{item.form_name}</p>
                <dl className="grid gap-x-4 gap-y-1 text-[13px] sm:grid-cols-[minmax(0,180px)_minmax(0,1fr)]">
                  {item.form_answers.map((answer) => <div key={answer.label} className="contents"><dt className="text-muted">{answer.label}</dt><dd className="text-foreground-2 whitespace-pre-wrap [overflow-wrap:anywhere]">
                    {answer.attachments && answer.attachments.length > 0 ? (
                      <ul className="grid gap-1">{answer.attachments.map((file) => (
                        <li key={file.id}><a className="inline-flex items-center gap-1.5 text-primary hover:underline" href={api.requestAttachmentUrl(item.id, file.id)} download={file.filename}>
                          <Paperclip className="size-3.5" />{file.filename}<span className="text-xs text-muted">({Math.max(1, Math.round(file.size_bytes / 1024))} KB)</span></a></li>
                      ))}</ul>
                    ) : answer.value}</dd></div>)}
                </dl>
                {!item.description.startsWith(item.form_answers.map((a) => `${a.label}: ${a.value}`).join("\n")) && <p className="text-[13px] whitespace-pre-wrap text-foreground-2 [overflow-wrap:anywhere]">{item.description}</p>}
              </div>
            ) : <p className="service-request-description text-[13px] whitespace-pre-wrap text-foreground-2 [overflow-wrap:anywhere]">{item.description}</p>}
            <details className="service-request-history group rounded-md bg-surface-2/60 px-3 py-2">
              <summary className="cursor-pointer text-xs font-medium text-foreground-2">History ({item.events.length})</summary>
              <ol className="mt-2 grid gap-2 border-l border-border pl-3">{item.events.map((event) => <li className="text-xs text-foreground-2" key={event.id}>
                <strong className="text-foreground">{label(event.action)}</strong> · {event.actor_email} · {formatDate(event.created_at)}{event.from_status !== event.to_status ? ` · ${label(event.from_status ?? "new")} → ${label(event.to_status)}` : ""}
                {event.comment && <p className="mt-0.5 whitespace-pre-wrap text-muted">{event.comment}</p>}
              </li>)}</ol>
            </details>
            {(item.can_cancel || item.status !== "cancelled") && <div className="service-request-actions flex flex-wrap items-center gap-2">
              {item.status !== "cancelled" && <>
                <Input className="min-w-0 flex-1" aria-label={`Add update to ${item.subject}`} maxLength={2000} placeholder="Add an update" value={comments[item.id] ?? ""} onChange={(event) => setComments((current) => ({ ...current, [item.id]: event.target.value }))} />
                <Button size="sm" disabled={!comments[item.id]?.trim()} onClick={() => void addComment(item)}><MessageSquare /> Add update</Button>
              </>}
              {item.can_cancel && <Button size="sm" variant="danger-ghost" disabled={busy} onClick={() => void cancel(item)}>Cancel request</Button>}
            </div>}
            {(item.budgets ?? []).map((line) => (
              <p key={line.name} className={cn("text-xs", line.would_exceed ? (line.enforcement === "block" ? "text-danger-text" : "text-warning-text") : "text-muted")}>
                Budget “{line.name}”: {formatMoney(Number(line.approved), line.currency)} of {formatMoney(Number(line.amount), line.currency)} approved
                {line.would_exceed ? ` · approving this takes it to ${formatMoney(Number(line.after_approval), line.currency)} (${line.enforcement === "block" ? "blocked" : "over budget"})` : ` · ${formatMoney(Number(line.amount) - Number(line.after_approval), line.currency)} left after this`}
              </p>
            ))}
            {(item.approval_steps?.length ?? 0) > 0 && <ApprovalTimeline item={item} isRequester={item.requester_user_id === user.id} onDecided={() => void load()} />}
            {item.approval && !item.approval.can_approve && <Alert tone="warning" title="You cannot approve this">
              {item.approval.reason}{item.approval.suggested_approver_email ? ` Suggested approver: ${item.approval.suggested_approver_email}.` : " Nobody up the reporting line has a high enough limit; an administrator can set one in People & access → Approval limits."}
            </Alert>}
            {item.approval?.can_approve && <p className="text-xs text-success-text">You can approve this: your limit is {formatMoney(Number(item.approval.your_limit), item.approval.limit_currency)}.</p>}
            {item.can_manage && !closedStatuses.includes(item.status) && <div className="service-request-triage grid gap-3 rounded-lg border border-dashed border-border-strong p-3 md:grid-cols-[repeat(3,minmax(0,1fr))_auto] md:items-end">
              <Field label="Update status"><Select aria-label={`Status for ${item.subject}`} value={state.status} onValueChange={(value) => setTriage((current) => ({ ...current, [item.id]: { ...state, status: value as typeof state.status } }))} options={(item.approval_steps?.length ?? 0) > 0 ? workflowStatuses.filter((option) => option.value !== "resolved" && option.value !== "rejected") : isFinancial(item.category) ? workflowStatuses.map((option) => option.value === "resolved" ? { ...option, label: "Approved" } : option) : workflowStatuses} /></Field>
              <Field label="Assign to"><Select aria-label={`Assignee for ${item.subject}`} placeholder="Unassigned" value={state.assignee} onValueChange={(value) => setTriage((current) => ({ ...current, [item.id]: { ...state, assignee: value } }))} options={[{ value: "", label: "Unassigned" }, ...options.map((candidate) => ({ value: candidate.id, label: candidate.email }))]} /></Field>
              <Field label={`Note ${noteRequired ? "(required)" : "(optional)"}`}><Input maxLength={2000} required={noteRequired} value={state.comment} onChange={(event) => setTriage((current) => ({ ...current, [item.id]: { ...state, comment: event.target.value } }))} /></Field>
              <Button variant="primary" disabled={busy || (noteRequired && !state.comment.trim()) || (state.status === item.status && state.assignee === (item.assigned_to_user_id ?? "") && !state.comment.trim())} onClick={() => void updateRequest(item)}>Save update</Button>
            </div>}
          </article>;
        })}</div>}
      </Card>
    </div>
  </>;
}

const closedStatuses: ServiceRequestStatus[] = ["resolved", "rejected", "cancelled"];
const labelClass = "grid min-w-0 gap-1.5 text-xs font-medium text-foreground-2";

function statusTone(status: ServiceRequestStatus) {
  if (status === "resolved") return "success" as const;
  if (status === "rejected" || status === "cancelled") return "neutral" as const;
  if (status === "waiting_requester") return "warning" as const;
  return "primary" as const;
}

function isFinancial(category: ServiceRequestCategory): boolean {
  return category === "purchase" || category === "expense";
}

/** The request's approval steps in order, with Approve / Reject for the person whose turn it is. */
function ApprovalTimeline({ item, isRequester, onDecided }: { item: ServiceRequestItem; isRequester: boolean; onDecided: () => void }) {
  const { toast } = useFeedback();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const steps = item.approval_steps ?? [];
  const pending = steps.find((step) => step.status === "pending");

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    try {
      const updated = await api.decideApprovalStep(item.id, decision, comment.trim());
      // Name the step actually decided: in a parallel stage it need not be the first pending one.
      const decided = (updated.approval_steps ?? []).filter((step) => step.decided_at).sort((a, b) => (b.decided_at ?? "").localeCompare(a.decided_at ?? ""))[0];
      toast("success", decision === "approve" ? "Step approved" : "Request rejected", decided?.label);
      setComment("");
      onDecided();
    } catch (err) { toast("error", "Could not record the decision", describeError(err)); }
    finally { setBusy(null); }
  }

  return (
    <div className="grid gap-2 rounded-lg border border-border bg-surface-2/40 p-3" aria-label="Approval steps">
      <p className="text-xs font-semibold text-foreground-2">Approval steps</p>
      <ol className="grid gap-2">
        {steps.map((step, index) => {
          const stage = step.stage ?? step.order, members = steps.filter((s) => (s.stage ?? s.order) === stage).length;
          const startsStage = index === 0 || (steps[index - 1].stage ?? steps[index - 1].order) !== stage;
          return (
          <li key={step.order} className={cn("flex items-start gap-2.5 text-[13px]", members > 1 && "border-l-2 border-primary/40 pl-2")}>
            <span className={cn("mt-0.5 grid size-5 shrink-0 place-items-center rounded-full", step.status === "approved" ? "bg-success-soft text-success-text" : step.status === "rejected" ? "bg-danger-soft text-danger-text" : step.status === "pending" ? "bg-primary-soft text-primary" : "bg-surface-2 text-muted")}>
              {step.status === "approved" ? <Check className="size-3.5" /> : step.status === "rejected" ? <X className="size-3.5" /> : step.status === "pending" ? <CircleDot className="size-3.5" /> : <Circle className="size-3" />}
            </span>
            <span className="min-w-0 flex-1">
              {startsStage && members > 1 && <span className="block text-[11px] font-semibold uppercase tracking-wide text-muted">Stage {stage} · {step.stage_mode === "any" ? "any one approves" : "all must approve"}</span>}
              <span className="font-medium text-foreground">{step.order}. {step.label}</span>{step.final && <span className="text-xs text-muted"> · final</span>}
              <span className="block text-xs text-muted">
                {step.status === "approved" || step.status === "rejected"
                  ? `${step.status === "approved" ? "Approved" : "Rejected"} by ${step.decided_by_email}${step.on_behalf_of_email ? ` for ${step.on_behalf_of_email}` : ""}${step.decided_at ? ` · ${formatDate(step.decided_at)}` : ""}${step.comment ? ` · “${step.comment}”` : ""}`
                  : step.status === "pending" ? `Waiting for ${step.approver}${step.pending_since ? ` since ${formatDate(step.pending_since)}` : ""}${step.reminder_count ? ` · reminded ${step.reminder_count}×` : ""}${step.escalated_at ? ` · escalated ${formatDate(step.escalated_at)}` : ""}` : step.status === "cancelled" ? "Not needed" : `Then ${step.approver}`}
              </span>
            </span>
          </li>
          );
        })}
      </ol>
      {pending && item.can_decide_step && (
        <div className="flex flex-wrap items-end gap-2 border-t border-border pt-2">
          <Input className="min-w-0 flex-1" maxLength={1500} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Note (required to reject)" aria-label={`Decision note for ${item.subject}`} />
          <Button size="sm" variant="primary" loading={busy === "approve"} disabled={busy !== null} onClick={() => void decide("approve")}>{busy !== "approve" && <Check />} Approve step</Button>
          <Button size="sm" variant="danger-ghost" loading={busy === "reject"} disabled={busy !== null || !comment.trim()} onClick={() => void decide("reject")}><X /> Reject</Button>
        </div>
      )}
      {pending && !item.can_decide_step && !isRequester && item.step_blocker && <p className="text-xs text-muted">{item.step_blocker}</p>}
    </div>
  );
}
