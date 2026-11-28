import { useCallback, useEffect, useState } from "react";
import { BadgeCheck, Pause, Play, Square, Zap } from "lucide-react";
import { describeError } from "../api";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Table } from "../components/ui";
import { workflowApi } from "./api";
import type { Run, RunDetail } from "./types";

const TONE: Record<string, "neutral" | "primary" | "success" | "warning" | "danger"> = {
  completed: "success", running: "primary", queued: "primary", paused: "warning", waiting: "warning", action: "warning",
  failed: "danger", cancelled: "neutral", stopped: "neutral", rejected: "danger", skipped: "neutral", ready: "primary",
};

/** Runs of the unified engine (automations, approvals, finance, studio) with plan → execute → verify → recover phases. */
export function RunsPanel({ scopeId, revision }: { scopeId: string; revision: number }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<RunDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try { setRuns((await workflowApi.runs(scopeId, status)).runs); setError(""); }
    catch (err) { setError(describeError(err)); }
  }, [scopeId, status]);
  useEffect(() => { void load(); }, [load, revision]);
  async function show(id: string) {
    try { setOpen(await workflowApi.run(scopeId, id)); } catch (err) { setError(describeError(err)); }
  }
  async function act(fn: () => Promise<unknown>, id: string) {
    setBusy(true);
    try { await fn(); await load(); await show(id); } catch (err) { setError(describeError(err)); } finally { setBusy(false); }
  }
  return <Card>
    <CardHeader title="Runs" description="Every automation, approval and finance workflow runs here. Pause, stop or resume from the last verified step." icon={<Zap />}
      actions={<select aria-label="Run status" className="h-8 rounded-md border border-border bg-surface px-2 text-xs" value={status} onChange={(e) => setStatus(e.target.value)}>
        {["", "running", "queued", "paused", "failed", "completed", "cancelled"].map((s) => <option key={s} value={s}>{s || "All statuses"}</option>)}</select>} />
    <CardBody className="grid gap-3">
      {error && <Alert tone="danger">{error}</Alert>}
      {runs.length === 0 ? <p className="text-sm text-muted">No runs in this scope.</p> : <Table>
        <thead><tr><th>Run</th><th>Origin</th><th>Status</th><th>Phase</th><th>Last verified</th><th>Started</th></tr></thead>
        <tbody>{runs.map((run) => <tr key={run.id} className="cursor-pointer" onClick={() => void show(run.id)}>
          <td className="font-mono text-xs">{run.id.slice(0, 8)}{run.simulation && <Badge className="ml-1">simulation</Badge>}</td>
          <td className="text-xs">{run.origin ?? "workflow"}{run.origin_ref ? ` · ${run.origin_ref}` : ""}</td>
          <td><Badge tone={TONE[run.status] ?? "neutral"}>{run.status}</Badge>{run.blocked_reason && <div className="text-xs text-warning-text">{run.blocked_reason}</div>}</td>
          <td className="text-xs">{run.phase ?? "—"}</td><td className="text-xs">{run.last_verified_node ?? "—"}</td>
          <td className="text-xs">{new Date(run.started_at ?? run.created_at).toLocaleString()}</td>
        </tr>)}</tbody>
      </Table>}
      {open && <div className="grid gap-2 rounded-xl border border-border p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm font-semibold">Run {open.run.id.slice(0, 8)} · <Badge tone={TONE[open.run.status] ?? "neutral"}>{open.run.status}</Badge> · phase {open.run.phase ?? "—"}
            {open.audit_verified && <Badge tone="success" className="ml-2"><BadgeCheck /> audit chain verified</Badge>}</div>
          <div className="flex gap-2">
            {["running", "queued", "waiting"].includes(open.run.status) && <Button size="sm" variant="secondary" disabled={busy} onClick={() => act(() => workflowApi.control(scopeId, open.run.id, "pause"), open.run.id)}><Pause /> Pause</Button>}
            {open.run.status === "paused" && <Button size="sm" variant="secondary" disabled={busy} onClick={() => act(() => workflowApi.control(scopeId, open.run.id, "resume"), open.run.id)}><Play /> Resume</Button>}
            {!["completed", "failed", "cancelled", "stopped"].includes(open.run.status) && <Button size="sm" variant="secondary" disabled={busy} onClick={() => act(() => workflowApi.control(scopeId, open.run.id, "stop"), open.run.id)}><Square /> Stop</Button>}
          </div>
        </div>
        {open.run.plan && <p className="text-xs text-muted">Plan: {open.run.plan.map((item) => item.label || item.node_id).join(" → ")}</p>}
        <Table>
          <thead><tr><th>Step</th><th>Status</th><th>Phase</th><th>Attempts</th><th>Verified</th><th /></tr></thead>
          <tbody>{open.steps.map((step) => <tr key={step.id}>
            <td>{step.node_id}</td><td><Badge tone={TONE[step.status] ?? "neutral"}>{step.status}</Badge>{step.failure_code && <div className="text-xs text-danger-text">{step.failure_code}</div>}</td>
            <td className="text-xs">{step.phase ?? "—"}</td><td>{step.attempts}</td>
            <td className="text-xs">{step.verified_at ? new Date(step.verified_at).toLocaleString() : "—"}</td>
            <td>{step.status === "action" && <Button size="sm" disabled={busy} onClick={() => act(() => workflowApi.perform(scopeId, open.run.id, step.id), open.run.id)}>Perform as me</Button>}</td>
          </tr>)}</tbody>
        </Table>
      </div>}
    </CardBody>
  </Card>;
}
