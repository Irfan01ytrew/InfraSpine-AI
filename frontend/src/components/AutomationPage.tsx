import { useCallback, useEffect, useMemo, useState } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { DepartmentGroup, User, WorkflowDefinition, WorkflowDryRun, WorkflowNotification, WorkflowRun, WorkflowTask } from "../types";
import { Alert, Button, PageHeader, SelectInput } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";

type WorkflowForm = {
  name: string;
  department_group_id: string;
  trigger_type: "manual" | "interval" | "service_request" | "request_sla";
  interval_minutes: string;
  action_type: "create_task" | "notify_member" | "assign_personal_task";
  assignee_user_id: string;
  task_title: string;
  task_body: string;
  due_days: string;
  notification_title: string;
  notification_body: string;
};

const emptyForm: WorkflowForm = {
  name: "", department_group_id: "", trigger_type: "manual", interval_minutes: "1440", action_type: "create_task",
  assignee_user_id: "", task_title: "", task_body: "", due_days: "", notification_title: "", notification_body: "",
};

const draftSteps = [
  { id: "trigger", kind: "Trigger", title: "Manual, scheduled or help-request event" },
  { id: "approval", kind: "Approval", title: "Independent review" },
  { id: "action", kind: "Action", title: "Run approved action" },
  { id: "completion", kind: "History", title: "Record completion" },
];
const draftEdges = [
  { source: "trigger", target: "approval" },
  { source: "approval", target: "action" },
  { source: "action", target: "completion" },
];

function WorkflowDiagram({
  nodes,
  edges,
  checks,
}: {
  nodes: WorkflowDryRun["nodes"];
  edges: WorkflowDryRun["edges"];
  checks?: WorkflowDryRun["checks"];
}) {
  return <div className="workflow-plan">
    <ol className="workflow-flow" aria-label="Workflow execution path">
      {nodes.map((node) => <li className="workflow-flow-step" key={node.id}>
        <div className="workflow-node">
          <span className="workflow-node-kind">{node.node_type === "action" ? "Action" : node.node_type}</span>
          <strong>{node.title}</strong>
          <span className="small muted">{node.description}</span>
        </div>
        {edges.some((edge) => edge.source === node.id) && <span className="workflow-flow-arrow" aria-hidden="true">→</span>}
      </li>)}
    </ol>
    {checks && <ul className="workflow-checks" aria-label="Workflow validation checks">
      {checks.map((check) => <li key={check.code} className={check.passed ? "is-passed" : "is-failed"}>
        <span aria-hidden="true">{check.passed ? "✓" : "!"}</span> {check.summary}
      </li>)}
    </ul>}
  </div>;
}

function dateLabel(value: string | null) {
  return value ? formatDate(value) : "—";
}

export function AutomationPage({ user }: { user: User }) {
  const isAdmin = user.role === "admin";
  const [groups, setGroups] = useState<DepartmentGroup[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowDefinition[]>([]);
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [tasks, setTasks] = useState<WorkflowTask[]>([]);
  const [notifications, setNotifications] = useState<WorkflowNotification[]>([]);
  const [members, setMembers] = useState<Array<{ user_id: string; email: string }>>([]);
  const [form, setForm] = useState<WorkflowForm>(emptyForm);
  const [selectedGroup, setSelectedGroup] = useState("");
  const [editing, setEditing] = useState<WorkflowDefinition | null>(null);
  const [rationales, setRationales] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dryRunBusy, setDryRunBusy] = useState<string | null>(null);
  const [dryRuns, setDryRuns] = useState<Record<string, WorkflowDryRun>>({});

  const load = useCallback(async (silent = false) => {
    if (!silent) setBusy(true);
    try {
      const groupsRequest = isAdmin ? api.adminGroups() : user.role === "manager" ? api.managedDepartments() : api.myDepartments();
      const [groupResult, workflowResult, runResult, taskResult, notificationResult] = await Promise.all([
        groupsRequest, api.automationWorkflows(), api.automationRuns(), api.automationTasks(), api.automationNotifications(),
      ]);
      const departments = groupResult.groups.filter((group) => group.kind === "department");
      setGroups(departments);
      setWorkflows(workflowResult.workflows);
      setRuns(runResult.runs);
      setTasks(taskResult.tasks);
      setNotifications(notificationResult.items);
      setSelectedGroup((current) => departments.some((group) => group.id === current) ? current : departments[0]?.id ?? "");
      setError(null);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }, [isAdmin, user.role]);

  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(() => load(true));

  useEffect(() => {
    let cancelled = false;
    if (!selectedGroup || !(isAdmin || user.role === "manager")) {
      setMembers([]);
      return;
    }
    const membersRequest = isAdmin
      ? api.adminUsers().then(({ users }) => ({ members: users
          .filter((candidate) => candidate.is_active && candidate.departments.some((department) => department.id === selectedGroup))
          .map((candidate) => ({ user_id: candidate.id, email: candidate.email })) }))
      : api.departmentMembers(selectedGroup);
    void membersRequest.then((result) => {
      if (!cancelled) setMembers(result.members);
    }).catch((err) => {
      if (!cancelled) setError(describeError(err));
    });
    return () => { cancelled = true; };
  }, [isAdmin, selectedGroup, user.role]);

  const manageableGroups = useMemo(() => new Set(groups.map((group) => group.id)), [groups]);
  const editable = isAdmin || user.role === "manager";
  const selectedMembers = members;

  function patchForm<K extends keyof WorkflowForm>(key: K, value: WorkflowForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function editWorkflow(workflow: WorkflowDefinition) {
    setEditing(workflow);
    setSelectedGroup(workflow.department_group_id);
    setForm({
      name: workflow.name,
      department_group_id: workflow.department_group_id,
      trigger_type: workflow.trigger_type,
      interval_minutes: String(workflow.interval_minutes ?? 1440),
      action_type: workflow.action_type,
      assignee_user_id: workflow.assignee_user_id ?? workflow.notification_recipient_user_id ?? "",
      task_title: workflow.task_title ?? "",
      task_body: workflow.task_body ?? "",
      due_days: workflow.due_days == null ? "" : String(workflow.due_days),
      notification_title: workflow.notification_title ?? "",
      notification_body: workflow.notification_body ?? "",
    });
    setNotice("Editing creates a new immutable version; prior runs retain their history.");
  }

  function clearForm() {
    setEditing(null);
    setForm({ ...emptyForm, department_group_id: selectedGroup, assignee_user_id: selectedMembers[0]?.user_id ?? "" });
  }

  async function saveWorkflow(event: React.FormEvent) {
    event.preventDefault();
    const departmentId = form.department_group_id || selectedGroup;
    if (!departmentId || !form.assignee_user_id || (form.action_type === "notify_member" && (!form.notification_title.trim() || !form.notification_body.trim()))) {
      setError("Choose a department and active member, then complete the selected action fields.");
      return;
    }
    setBusy(true);
    try {
      const commonValues = {
        name: form.name,
        department_group_id: departmentId,
        trigger_type: form.trigger_type,
        interval_minutes: form.trigger_type === "interval" ? Number(form.interval_minutes) : null,
      } as const;
      const actionValues = form.action_type !== "notify_member"
        ? {
            action_type: form.action_type,
            assignee_user_id: form.assignee_user_id,
            task_title: form.task_title,
            task_body: form.task_body,
            due_days: form.due_days === "" ? null : Number(form.due_days),
          } as const
        : {
            action_type: form.action_type,
            notification_recipient_user_id: form.assignee_user_id,
            notification_title: form.notification_title,
            notification_body: form.notification_body,
          } as const;
      if (editing) {
        await api.versionAutomationWorkflow(editing.id, {
          ...commonValues,
          ...actionValues,
          expected_version: editing.version,
        });
        setNotice("Workflow version saved.");
      } else {
        await api.createAutomationWorkflow({ ...commonValues, ...actionValues });
        setNotice("Workflow created. Runs require a separate department manager or administrator approval.");
      }
      clearForm();
      await load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function act(action: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError(null);
    try {
      await action();
      setNotice(message);
      await load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function previewWorkflow(workflow: WorkflowDefinition) {
    setDryRunBusy(workflow.id);
    setError(null);
    try {
      const result = await api.dryRunAutomationWorkflow(workflow.id);
      setDryRuns((current) => ({ ...current, [workflow.id]: result }));
      setNotice("Read-only validation passed. No run, task or audit entry was created.");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setDryRunBusy(null);
    }
  }

  return (
    <>
      <PageHeader
        title={<>Workflows & tasks</>}
        description="Department-scoped task automation with a second-person approval, durable run history and retry recovery."
        actions={<><Button onClick={() =>void load()} disabled={busy}>Refresh</Button></>}
      />
      {error && <Alert tone="danger" className="mb-4">{error}</Alert>}
      {notice && <Alert tone="success" className="mb-4">{notice}</Alert>}

      <section className="card automation-card" aria-label="Workflow notifications">
        <div className="card-title"><div><h2>Notifications</h2><p className="muted">Approval requests, decisions, assigned tasks and approved department messages.</p></div><span className="pill pill-sm">{notifications.filter((item) => !item.read_at).length} unread</span></div>
        {notifications.length === 0 ? <p className="empty">No workflow notifications.</p> : <ul className="workflow-notifications">
          {notifications.map((item) => <li key={item.id}>
            <span>
              <strong>{item.notification_type === "workflow_message" ? item.message_title : item.notification_type === "approval_required" ? "Workflow approval requested" : item.notification_type === "run_approved" ? "Workflow request approved" : item.notification_type === "run_rejected" ? "Workflow request rejected" : "Task assigned"}</strong>
              {item.notification_type === "workflow_message" && item.message_body && <span className="small">{item.message_body}</span>}
              <span className="small muted"> · Run {item.run_id.slice(0, 8)} · {dateLabel(item.created_at)}{item.read_at ? " · Read" : " · Unread"}</span>
            </span>
            {!item.read_at && <button className="btn btn-ghost" disabled={busy} onClick={() => void act(() => api.readAutomationNotification(item.id), "Notification marked read.")}>Mark read</button>}
          </li>)}
        </ul>}
      </section>

      {editable && (
        <section className="card automation-card">
          <div className="card-title"><div><h2>{editing ? `Version ${editing.version + 1}` : "Create a workflow"}</h2><p className="muted">Actions are limited to internal tasks, My Work assignments and department inbox messages; external delivery and arbitrary code are disabled.</p></div></div>
          {groups.length === 0 ? <p className="empty">You do not manage a department yet.</p> : <form className="automation-form" onSubmit={(event) => void saveWorkflow(event)}>
            <label className="field"><span className="field-label">Workflow name</span><input required maxLength={120} value={form.name} onChange={(event) => patchForm("name", event.target.value)} /></label>
            <label className="field"><span className="field-label">Department</span><SelectInput value={form.department_group_id || selectedGroup} disabled={Boolean(editing)} onChange={(event) => { setSelectedGroup(event.target.value); patchForm("department_group_id", event.target.value); patchForm("assignee_user_id", ""); }} required>
              {groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
            </SelectInput></label>
            <label className="field"><span className="field-label">Trigger</span><SelectInput value={form.trigger_type} onChange={(event) => patchForm("trigger_type", event.target.value as WorkflowForm["trigger_type"])}><option value="manual">Manual request</option><option value="interval">Scheduled interval</option><option value="service_request">When a department help request is submitted</option><option value="request_sla">When a department help request misses its response target</option></SelectInput></label>
            <label className="field"><span className="field-label">Approved action</span><SelectInput value={form.action_type} onChange={(event) => patchForm("action_type", event.target.value as WorkflowForm["action_type"])}><option value="create_task">Create a workflow task</option><option value="assign_personal_task">Assign a task to My Work</option><option value="notify_member">Send an in-app message</option></SelectInput></label>
            {(form.trigger_type === "service_request" || form.trigger_type === "request_sla") && <p className="automation-wide small muted">HR and requests without a department are excluded. Each match waits for a separate manager or administrator approval before the selected action runs.</p>}
            {form.trigger_type === "interval" && <label className="field"><span className="field-label">Repeat interval (minutes, 60–10080)</span><input type="number" min={60} max={10080} value={form.interval_minutes} onChange={(event) => patchForm("interval_minutes", event.target.value)} required /></label>}
            <label className="field"><span className="field-label">{form.action_type === "notify_member" ? "Message recipient" : "Task assignee"}</span><SelectInput value={form.assignee_user_id} onChange={(event) => patchForm("assignee_user_id", event.target.value)} required>
              <option value="">Select an active member</option>{selectedMembers.map((member) => <option key={member.user_id} value={member.user_id}>{member.email}</option>)}
            </SelectInput></label>
            {form.action_type !== "notify_member" ? <>
              <label className="field"><span className="field-label">{form.action_type === "assign_personal_task" ? "My Work task title" : "Task title"}</span><input required maxLength={180} value={form.task_title} onChange={(event) => patchForm("task_title", event.target.value)} /></label>
              <label className="field automation-wide"><span className="field-label">Task instructions</span><textarea required maxLength={4000} rows={3} value={form.task_body} onChange={(event) => patchForm("task_body", event.target.value)} /></label>
              <label className="field"><span className="field-label">Due in days (optional)</span><input type="number" min={0} max={365} value={form.due_days} onChange={(event) => patchForm("due_days", event.target.value)} /></label>
            </> : <>
              <label className="field"><span className="field-label">Message title</span><input required maxLength={180} value={form.notification_title} onChange={(event) => patchForm("notification_title", event.target.value)} /></label>
              <label className="field automation-wide"><span className="field-label">Message</span><textarea required maxLength={2000} rows={3} value={form.notification_body} onChange={(event) => patchForm("notification_body", event.target.value)} /></label>
            </>}
            <div className="automation-wide workflow-draft-preview">
              <div><h3>Flow preview</h3><p className="small muted">A run always waits for a separate approval before the internal task is created.</p></div>
              <ol className="workflow-flow" aria-label="Draft workflow flow">
                {draftSteps.map((step) => <li className="workflow-flow-step" key={step.id}>
                  <div className="workflow-node"><span className="workflow-node-kind">{step.kind}</span><strong>{step.id === "trigger" ? (form.trigger_type === "manual" ? "Manual request" : form.trigger_type === "interval" ? `Every ${form.interval_minutes || "…"} minutes` : form.trigger_type === "request_sla" ? "On missed response target" : "On help request submission") : step.id === "action" ? (form.action_type === "notify_member" ? form.notification_title.trim() || "Send in-app message" : form.task_title.trim() || (form.action_type === "assign_personal_task" ? "Assign a My Work task" : "Create workflow task")) : step.title}</strong></div>
                  {draftEdges.some((edge) => edge.source === step.id) && <span className="workflow-flow-arrow" aria-hidden="true">→</span>}
                </li>)}
              </ol>
            </div>
            <div className="automation-actions"><button className="btn btn-primary" disabled={busy || !selectedMembers.length}>{editing ? "Save new version" : "Create workflow"}</button>{editing && <button type="button" className="btn" onClick={clearForm}>Cancel edit</button>}</div>
          </form>}
        </section>
      )}

      <section className="card automation-card">
          <div className="card-title"><div><h2>Available workflows</h2><p className="muted">Requests enter a pending approval queue. The worker runs the selected action after approval.</p></div><span className="pill pill-sm">{workflows.length}</span></div>
        {workflows.length === 0 ? <p className="empty">No workflows are configured for your departments.</p> : <div className="automation-list">{workflows.map((workflow) => <article className="automation-item" key={workflow.id}>
          <div className="automation-item-heading"><div><h3>{workflow.name}</h3><p className="muted">{workflow.department_name} · {workflow.trigger_type === "manual" ? "Manual" : workflow.trigger_type === "interval" ? `Every ${workflow.interval_minutes} minutes` : workflow.trigger_type === "request_sla" ? "On missed response target" : "On department help request"} · v{workflow.version}</p></div><span className="pill pill-success">Enabled</span></div>
          {(isAdmin || workflow.can_manage) && <p className="automation-task-preview"><strong>{workflow.action_type === "notify_member" ? `Message: ${workflow.notification_title}` : workflow.task_title}</strong><br />{workflow.action_type === "notify_member" ? workflow.notification_body : workflow.task_body}</p>}
          {workflow.trigger_type === "interval" && <p className="small muted">Next scheduled request: {dateLabel(workflow.next_run_at)}</p>}
          <div className="automation-actions">
            <button className="btn btn-primary" disabled={busy} onClick={() => void act(() => api.requestAutomationRun(workflow.id), "Run requested and queued for approval.")}>Request run</button>
            {(isAdmin || workflow.can_manage) && <><button className="btn" disabled={busy || dryRunBusy === workflow.id} onClick={() => void previewWorkflow(workflow)}>{dryRunBusy === workflow.id ? "Validating…" : "Validate flow"}</button><button className="btn" disabled={busy} onClick={() => editWorkflow(workflow)}>Edit / version</button><button className="btn btn-ghost" disabled={busy} onClick={() => void act(() => api.disableAutomationWorkflow(workflow.id), "Workflow disabled; history is retained.")}>Disable</button></>}
          </div>
          {(isAdmin || workflow.can_manage) && dryRuns[workflow.id] && <div className="workflow-dry-run" aria-live="polite">
            <div className="workflow-dry-run-heading"><div><h4>Validated plan · version {dryRuns[workflow.id].workflow_version}</h4><p className="small muted">Read-only dry run. No workflow run, task or audit record was created.</p></div><span className="pill pill-success">{dryRuns[workflow.id].valid ? "Ready" : "Needs attention"}</span></div>
            <WorkflowDiagram nodes={dryRuns[workflow.id].nodes} edges={dryRuns[workflow.id].edges} checks={dryRuns[workflow.id].checks} />
            {dryRuns[workflow.id].task_preview?.title && <div className="workflow-task-preview"><strong>{dryRuns[workflow.id].task_preview?.title}</strong><p>{dryRuns[workflow.id].task_preview?.body}</p><span className="small muted">Assigned to {dryRuns[workflow.id].task_preview?.assignee_email}{dryRuns[workflow.id].task_preview?.due_days == null ? " · No due date" : ` · Due in ${dryRuns[workflow.id].task_preview?.due_days} days`}</span></div>}
            {dryRuns[workflow.id].notification_preview?.title && <div className="workflow-task-preview"><strong>{dryRuns[workflow.id].notification_preview?.title}</strong><p>{dryRuns[workflow.id].notification_preview?.body}</p><span className="small muted">Send an in-app message to {dryRuns[workflow.id].notification_preview?.recipient_email}</span></div>}
          </div>}
        </article>)}</div>}
      </section>

      <section className="card automation-card">
        <div className="card-title"><div><h2>Run history & approvals</h2><p className="muted">Approvers must manage the department and cannot approve their own request.</p></div><span className="pill pill-sm">{runs.length}</span></div>
        {runs.length === 0 ? <p className="empty">No workflow runs yet.</p> : <div className="automation-list">{runs.map((run) => <article className="automation-item" key={run.id}>
          <div className="automation-item-heading"><div><h3>{run.workflow_name}</h3><p className="muted">Requested {dateLabel(run.created_at)} · {run.attempts} worker attempt{run.attempts === 1 ? "" : "s"}</p></div><span className={`pill ${run.status === "completed" ? "pill-success" : run.status === "failed" ? "pill-warning" : ""}`}>{run.status.replaceAll("_", " ")}</span></div>
          {run.approval_decision && <p className="automation-task-preview"><strong>{run.approval_decision === "approved" ? "Approved" : "Rejected"}</strong> · {dateLabel(run.approval_decided_at)}<br />{run.approval_rationale}</p>}
          {run.last_error_code && <p className="small muted">Last worker error code: {run.last_error_code}</p>}
          {run.can_approve && <div className="automation-approval"><label className="field"><span className="field-label">Decision rationale</span><textarea rows={2} minLength={3} maxLength={2000} value={rationales[run.id] ?? ""} onChange={(event) => setRationales((current) => ({ ...current, [run.id]: event.target.value }))} /></label><div className="automation-actions"><button className="btn btn-primary" disabled={busy || (rationales[run.id] ?? "").trim().length < 3} onClick={() => void act(() => api.decideAutomationRun(run.id, true, rationales[run.id] ?? ""), "Run approved and queued for the worker.")}>Approve</button><button className="btn btn-ghost" disabled={busy || (rationales[run.id] ?? "").trim().length < 3} onClick={() => void act(() => api.decideAutomationRun(run.id, false, rationales[run.id] ?? ""), "Run rejected; the decision is retained.")}>Reject</button></div></div>}
          <div className="automation-actions">{run.can_cancel && <button className="btn btn-ghost" disabled={busy} onClick={() => void act(() => api.cancelAutomationRun(run.id), "Run cancelled.")}>Cancel</button>}{run.can_retry && <button className="btn" disabled={busy} onClick={() => void act(() => api.retryAutomationRun(run.id), "Run requeued for a safe retry.")}>Retry</button>}</div>
        </article>)}</div>}
      </section>

      <section className="card automation-card">
        <div className="card-title"><div><h2>Department tasks</h2><p className="muted">Employees see tasks assigned to them. Managers see tasks in managed departments.</p></div><span className="pill pill-sm">{tasks.length}</span></div>
        {tasks.length === 0 ? <p className="empty">No tasks are available.</p> : <div className="automation-list">{tasks.map((task) => <article className="automation-item" key={task.id}>
          <div className="automation-item-heading"><div><h3>{task.title}</h3><p className="muted">Due {dateLabel(task.due_at)} · Created {dateLabel(task.created_at)}</p></div><span className={`pill ${task.status === "completed" ? "pill-success" : ""}`}>{task.status}</span></div>
          <p className="automation-task-preview">{task.body}</p>
          {task.status === "open" && task.assignee_user_id === user.id && <button className="btn btn-primary" disabled={busy} onClick={() => void act(() => api.completeAutomationTask(task.id), "Task marked complete.")}>Complete task</button>}
        </article>)}</div>}
      </section>
      {!manageableGroups.size && editable && <p className="small muted">No managed departments are available for workflow setup.</p>}
    </>
  );
}
