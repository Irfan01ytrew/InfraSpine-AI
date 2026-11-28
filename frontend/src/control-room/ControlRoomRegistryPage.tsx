// Step Q slice 4 — control-room page: agent registry, workflow-run roll-up,
// blockers first, and one-click pause / resume / assign controls.
//
// Every control is two steps, and the page never shortcuts them: first the
// operator requests an approval for the exact action (a different manager
// approves it in Approvals), then the operator executes it with the approval
// the backend issued. The backend refuses a reused, expired, or mismatched
// approval, and this page surfaces that refusal instead of retrying.
// The page is read-only when the scope does not grant workflow.manage.
//
// Merge note: the Step AF branch (agent/control-room-live) has its own
// frontend/src/control-room/ControlRoomPage.tsx (live charts + alerts).
// This file is deliberately named ControlRoomRegistryPage so the two pages
// coexist; at merge they become the two tabs of one Control Room view
// (Registry & controls / Charts & alerts) instead of one file overwriting
// the other.
import { useCallback, useEffect, useState } from "react";
import { Activity, AlertTriangle, Bot, Pause, Play, RefreshCw, UserPlus, Workflow } from "lucide-react";
import { describeError } from "../api";
import { useFeedback } from "../components/Feedback";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, PageHeader, SelectInput, Skeleton, Stat } from "../components/ui";
import {
  controlRoomApi,
  type AgentCommand,
  type AgentEntry,
  type ApprovalRequestOut,
  type ControlRoomOverview,
  type RunCommand,
  type RunSummary,
  type Scope,
} from "./api";
import {
  agentStatus,
  blockerTitle,
  canControlAgent,
  canControlRun,
  controlLabel,
  formatTokens,
  pendingKey,
  runRollup,
  sortAgents,
} from "./controlRoomView.mjs";

const POLL_MS = 30_000;

const RUN_TONE: Record<string, "neutral" | "primary" | "success" | "warning" | "danger"> = {
  completed: "success",
  failed: "danger",
  running: "primary",
  queued: "warning",
  paused: "warning",
  cancelled: "neutral",
  compensating: "warning",
  compensated: "neutral",
};

const AGENT_TONE: Record<string, "neutral" | "primary" | "success" | "warning" | "danger"> = {
  paused: "warning",
  failed: "danger",
  disabled: "neutral",
  running: "primary",
  queued: "warning",
  ready: "success",
  idle: "neutral",
};

function timeOf(iso: string | null) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}

interface PendingControl {
  approval: ApprovalRequestOut;
  command: AgentCommand | RunCommand;
  assigneeId?: string;
  assigneeEmail?: string;
}

export function ControlRoomRegistryPage() {
  const { confirm } = useFeedback();
  const [scopes, setScopes] = useState<Scope[] | null>(null);
  const [group, setGroup] = useState("");
  const [data, setData] = useState<ControlRoomOverview | null>(null);
  const [members, setMembers] = useState<{ id: string; email: string; role: string }[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState("");
  const [pending, setPending] = useState<Record<string, PendingControl>>({});
  const [assigneeByAgent, setAssigneeByAgent] = useState<Record<string, string>>({});

  useEffect(() => {
    controlRoomApi.scopes().then((result) => {
      setScopes(result.scopes);
      setGroup((current) => current || result.scopes[0]?.id || "");
    }).catch((err) => setError(describeError(err)));
  }, []);

  const scope = scopes?.find((item) => item.id === group);

  const load = useCallback(() => {
    if (!group) return;
    controlRoomApi.overview(group)
      .then((result) => {
        setData(result);
        setError("");
      })
      .catch((err) => setError(describeError(err)));
  }, [group]);

  useEffect(() => {
    setData(null);
    setPending({});
    setNotice("");
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, revision]);

  useEffect(() => {
    if (!group || !scope?.can_manage) {
      setMembers([]);
      return;
    }
    controlRoomApi.members(group)
      .then((result) => setMembers(result.members))
      .catch(() => setMembers([]));
  }, [group, scope?.can_manage]);

  const requestControl = async (
    kind: "run" | "agent",
    id: string,
    label: string,
    command: RunCommand | AgentCommand,
    assigneeId?: string,
  ) => {
    const ok = await confirm({
      title: `${controlLabel(command, kind)}?`,
      body: `This asks for approval to ${command} ${label}. A different manager must approve this exact action in Approvals before it can run — the approval expires and can be used once. Nothing changes until then.`,
      confirmLabel: "Request approval",
    });
    if (!ok) return;
    const key = pendingKey(kind, id);
    setBusy(key);
    setError("");
    setNotice("");
    try {
      const approval = kind === "run"
        ? await controlRoomApi.requestRunControl(group, id, command as RunCommand)
        : await controlRoomApi.requestAgentControl(group, id, command as AgentCommand, assigneeId);
      const assigneeEmail = assigneeId ? members.find((m) => m.id === assigneeId)?.email : undefined;
      setPending((current) => ({ ...current, [key]: { approval, command, assigneeId, assigneeEmail } }));
      setNotice(`Approval requested for ${label} (${controlLabel(command, kind)}). Ask a different manager to approve it in Approvals, then press Execute below.`);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy("");
    }
  };

  const executeControl = async (kind: "run" | "agent", id: string, label: string) => {
    const entry = pending[pendingKey(kind, id)];
    if (!entry) return;
    const key = pendingKey(kind, id);
    setBusy(key);
    setError("");
    setNotice("");
    try {
      if (kind === "run") {
        await controlRoomApi.executeRunControl(group, id, entry.command as RunCommand, entry.approval.id);
      } else {
        await controlRoomApi.executeAgentControl(group, id, entry.command as AgentCommand, entry.approval.id, entry.assigneeId);
      }
      setPending((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      setNotice(`${label}: ${controlLabel(entry.command, kind)} ran.`);
      load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy("");
    }
  };

  const rollup = data ? runRollup(data.runs_by_status) : null;
  const sortedAgents = data ? sortAgents(data.agents) : [];

  const runControls = (run: RunSummary) => {
    const key = pendingKey("run", run.id);
    const entry = pending[key];
    if (entry) {
      return <span className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">Approval {entry.approval.id.slice(0, 8)}… waiting</span>
        <Button size="sm" variant="primary" disabled={busy === key} onClick={() => executeControl("run", run.id, run.workflow_name)}>
          Execute {entry.command}
        </Button>
      </span>;
    }
    return <span className="flex flex-wrap items-center gap-2">
      {canControlRun(run, "pause") && <Button size="sm" disabled={busy === key} onClick={() => requestControl("run", run.id, `run “${run.workflow_name}”`, "pause")}>
        <Pause /> Pause
      </Button>}
      {canControlRun(run, "resume") && <Button size="sm" disabled={busy === key} onClick={() => requestControl("run", run.id, `run “${run.workflow_name}”`, "resume")}>
        <Play /> Resume
      </Button>}
    </span>;
  };

  const agentControls = (agent: AgentEntry) => {
    const key = pendingKey("agent", agent.id);
    const entry = pending[key];
    if (entry) {
      return <span className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">Approval {entry.approval.id.slice(0, 8)}… waiting{entry.assigneeEmail ? ` · to ${entry.assigneeEmail}` : ""}</span>
        <Button size="sm" variant="primary" disabled={busy === key} onClick={() => executeControl("agent", agent.id, agent.name)}>
          Execute {entry.command}
        </Button>
      </span>;
    }
    const candidates = members.filter((m) => m.id !== agent.owner_id);
    const assigneeId = assigneeByAgent[agent.id] || candidates[0]?.id || "";
    return <span className="flex flex-wrap items-center gap-2">
      {canControlAgent(agent, "pause") && <Button size="sm" disabled={busy === key} onClick={() => requestControl("agent", agent.id, `agent “${agent.name}”`, "pause")}>
        <Pause /> Pause
      </Button>}
      {canControlAgent(agent, "resume") && <Button size="sm" disabled={busy === key} onClick={() => requestControl("agent", agent.id, `agent “${agent.name}”`, "resume")}>
        <Play /> Resume
      </Button>}
      {canControlAgent(agent, "assign") && candidates.length > 0 && <span className="flex items-center gap-2">
        <SelectInput value={assigneeId} onChange={(event) => setAssigneeByAgent((current) => ({ ...current, [agent.id]: event.target.value }))} aria-label={`Reassign ${agent.name} to`}>
          {candidates.map((m) => <option key={m.id} value={m.id}>{m.email}</option>)}
        </SelectInput>
        <Button size="sm" disabled={busy === key || !assigneeId} onClick={() => requestControl("agent", agent.id, `agent “${agent.name}”`, "assign", assigneeId)}>
          <UserPlus /> Reassign
        </Button>
      </span>}
    </span>;
  };

  return <div className="grid gap-5 pb-8">
    <PageHeader eyebrow="CONTROL ROOM" title="Control room"
      description="Agents, workflow runs and blockers for one department, refreshed every 30 seconds. Blockers come first. Pause, resume and reassign always need a different manager's approval — this page only asks for it, it never acts alone."
      actions={<Button variant="secondary" onClick={() => setRevision((v) => v + 1)}><RefreshCw /> Refresh</Button>} />
    {error && <Alert tone="danger" title="Control room action failed">{error}</Alert>}
    {notice && <Alert tone="info" title="Control room">{notice}</Alert>}
    {scopes === null && !error && <Skeleton className="h-40 w-full" />}
    {scopes && scopes.length === 0 && <Card><EmptyState icon={<Activity />} title="No workflow scope yet"
      description="Join a department to see its control room." /></Card>}
    {scopes && scopes.length > 0 && <>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Department" className="min-w-64">
          <SelectInput value={group} onChange={(event) => setGroup(event.target.value)} aria-label="Department">
            {scopes.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </SelectInput>
        </Field>
        <div className="ml-auto pb-1 text-xs text-muted">
          {scope?.can_manage ? "You manage this department · " : "Read-only — you cannot control this department · "}
          {data ? "Updated just now · refreshes every 30s" : "Loading…"}
        </div>
      </div>

      {data && <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Stat label="Workflow engine" value={data.worker.engine_on ? "On" : "Off"}
          hint={data.worker.engine_on ? "Worker is enabled" : "Worker is off — runs will queue"}
          icon={<Activity className="size-4" />} />
        <Stat label="Runs in total" value={String(rollup?.total ?? 0)} hint={`${rollup?.active ?? 0} active right now`} icon={<Workflow className="size-4" />} />
        <Stat label="Paused runs" value={String(rollup?.paused ?? 0)} hint="Paused runs need a resume" />
        <Stat label="Failed runs" value={String(rollup?.failed ?? 0)} hint="Failed runs can be resumed" />
        <Stat label="Blockers" value={String(data.blockers.length)} hint="Runs that need a human" icon={<AlertTriangle className="size-4" />} />
      </div>}

      {data && data.blockers.length > 0 && <Card>
        <CardHeader title="Blockers first" description="Runs that need a human — paused, failed, or waiting for approval. Newest first." />
        <CardBody>
          <div className="grid gap-2">
            {data.blockers.map((blocker) => {
              const run = data.recent_runs.find((item) => item.id === blocker.run_id);
              return <div key={`${blocker.kind}:${blocker.run_id}`} className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm">
                <AlertTriangle className="size-4 shrink-0 text-warning" />
                <span className="font-medium">{blockerTitle(blocker)}</span>
                <span className="text-muted">{run?.workflow_name ?? "Run"}</span>
                {blocker.reason && <span className="text-muted">· {blocker.reason}</span>}
                <span className="text-xs text-muted">{blocker.origin} · {timeOf(blocker.created_at)}</span>
                {blocker.kind === "run_awaiting_approval"
                  ? <span className="ml-auto text-xs text-muted">Decide it in Approvals</span>
                  : <span className="ml-auto">{run && scope?.can_manage ? runControls(run) : null}</span>}
              </div>;
            })}
          </div>
        </CardBody>
      </Card>}

      {data && <Card>
        <CardHeader title="Agent registry" description="Every Agent Studio agent owned by a member of this department, attention first." icon={<Bot className="size-4" />} />
        <CardBody>
          {sortedAgents.length === 0 ? <EmptyState icon={<Bot />} title="No agents in this department" description="Agents owned by department members will appear here." />
            : <div className="grid gap-2">
              {sortedAgents.map((agent) => <div key={agent.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm">
                <span className="font-medium">{agent.name}</span>
                <Badge tone={AGENT_TONE[agentStatus(agent)]}>{agentStatus(agent)}</Badge>
                {agent.latest_run ? <span className="text-muted">
                  latest run {agent.latest_run.status}
                  {agent.latest_run.failure_code ? ` · ${agent.latest_run.failure_code}` : ""}
                  {` · ${formatTokens(agent.latest_run.prompt_tokens + agent.latest_run.completion_tokens)} tokens`}
                </span> : <span className="text-muted">no runs yet</span>}
                <span className="ml-auto">{scope?.can_manage ? agentControls(agent) : <span className="text-xs text-muted">Read-only</span>}</span>
              </div>)}
            </div>}
        </CardBody>
      </Card>}

      {data && <Card>
        <CardHeader title="Recent runs" description="Latest workflow runs in this department, with the controls each run allows." />
        <CardBody>
          {data.recent_runs.length === 0 ? <EmptyState icon={<Workflow />} title="No runs yet" description="Workflow runs for this department will appear here." />
            : <div className="grid gap-2">
              {data.recent_runs.map((run) => <div key={run.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm">
                <span className="font-medium">{run.workflow_name}</span>
                <Badge tone={RUN_TONE[run.status] ?? "neutral"}>{run.status.replace(/_/g, " ")}</Badge>
                <span className="text-muted">{run.phase} · {run.origin}</span>
                {run.blocked_reason && <span className="text-muted">· {run.blocked_reason}</span>}
                {run.failure_code && <span className="text-muted">· {run.failure_code}</span>}
                <span className="text-xs text-muted">{timeOf(run.created_at)}</span>
                <span className="ml-auto">{scope?.can_manage ? runControls(run) : null}</span>
              </div>)}
            </div>}
          {Object.keys(data.runs_by_origin).length > 0 && <p className="mt-3 text-xs text-muted">
            Runs by origin: {Object.entries(data.runs_by_origin).map(([origin, count]) => `${origin} · ${count}`).join(" · ")}
          </p>}
        </CardBody>
      </Card>}
    </>}
  </div>;
}
