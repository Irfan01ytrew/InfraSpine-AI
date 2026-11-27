import { useMemo, useState } from "react";
import { Activity, Search, Square, ArrowUpRight } from "lucide-react";
import type { AgentStudioAgent, AgentStudioRun } from "../types";
import { formatDate } from "../format";
import { Badge, Button, Card, CardBody, CardHeader, EmptyState, Input } from "./ui";

export function AgentRunMonitor({ runs, agents, counts, oldestQueued, onOpen, onCancel, busy }: {
  runs: AgentStudioRun[]; agents: AgentStudioAgent[];
  counts: Record<string, number> | null; oldestQueued: string | null;
  onOpen: (run: AgentStudioRun) => void; onCancel: (run: AgentStudioRun) => void; busy: boolean;
}) {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [agentId, setAgentId] = useState("");
  const filtered = useMemo(() => runs.filter(run => (!status || run.status === status)
    && (!agentId || run.agent_id === agentId)
    && (!search || `${run.prompt} ${run.failure_code ?? ""}`.toLowerCase().includes(search.toLowerCase()))),
    [runs, search, status, agentId]);
  return <div className="grid gap-4">
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {[["Queued", "queued"], ["Running", "running"], ["Failed", "failed"], ["Needs recovery", "recovery_required"]].map(([label, key]) =>
        <Card key={key}><CardBody><p className="text-xs text-muted">{label}</p><strong className="text-2xl">{counts ? counts[key] ?? 0 : "—"}</strong></CardBody></Card>)}
    </div>
    <Card>
      <CardHeader title="Task monitor" icon={<Activity />} description="Filter the 100 most recent tasks. Counters include your full history." />
      <CardBody className="grid gap-4">
        {oldestQueued && <p className="rounded-lg border border-border bg-surface-2 p-3 text-sm">Oldest queued task: {formatDate(oldestQueued)}. Tasks wait here until an Agent Studio worker is available.</p>}
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="flex items-center gap-2"><Search size={16} /><Input aria-label="Search agent tasks" placeholder="Search task or failure code" value={search} onChange={e => setSearch(e.target.value)} /></div>
          <select aria-label="Filter task status" className="rounded-md border border-border bg-surface p-2 text-sm" value={status} onChange={e => setStatus(e.target.value)}>
            <option value="">All statuses</option>{["queued", "running", "awaiting_approval", "completed", "failed", "cancelled", "recovery_required"].map(s => <option key={s} value={s}>{s.replaceAll("_", " ")}</option>)}
          </select>
          <select aria-label="Filter task agent" className="rounded-md border border-border bg-surface p-2 text-sm" value={agentId} onChange={e => setAgentId(e.target.value)}>
            <option value="">All agents</option>{agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </div>
        <p className="text-xs text-muted" aria-live="polite">{filtered.length} matching tasks</p>
        {filtered.length ? filtered.map(run => <article key={run.id} className="grid gap-3 rounded-lg border border-border p-4 sm:grid-cols-[1fr_auto]">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><Badge tone={run.status === "completed" ? "success" : ["failed", "recovery_required"].includes(run.status) ? "danger" : "neutral"}>{run.status.replaceAll("_", " ")}</Badge><strong className="text-sm">{agents.find(a => a.id === run.agent_id)?.name ?? "Agent"}</strong></div>
            <p className="mt-2 break-words text-sm">{run.prompt}</p>
            <p className="mt-2 text-xs text-muted">{formatDate(run.created_at)} · {run.attempts} worker attempt{run.attempts === 1 ? "" : "s"} · {run.steps.length} recorded steps</p>
            {run.failure_code && <p className="mt-1 text-xs text-muted">Reason: {run.failure_code.replaceAll("_", " ")}</p>}
            {run.status === "recovery_required" && <p className="mt-2 text-sm">Execution was interrupted after tool dispatch. Review the execution log and actual effects before submitting replacement work; this task will not replay automatically.</p>}
            {run.cancel_requested && run.status === "running" && <p className="mt-2 text-sm" role="status">Cancellation requested. The worker will stop at the next execution boundary; an active tool may finish.</p>}
          </div>
          <div className="flex items-start gap-2"><Button size="sm" variant="secondary" onClick={() => onOpen(run)}><ArrowUpRight /> Open task</Button>
            {run.durable && ["queued", "running", "awaiting_approval"].includes(run.status) && <Button size="sm" variant="danger-ghost" disabled={busy || run.cancel_requested} onClick={() => onCancel(run)}><Square /> {run.cancel_requested ? "Stopping" : "Cancel"}</Button>}
          </div>
        </article>) : <EmptyState icon={<Activity />} title="No matching tasks" description="Adjust the filters or start a task in the agent workspace." />}
      </CardBody>
    </Card>
  </div>;
}
