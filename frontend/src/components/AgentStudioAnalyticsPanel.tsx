import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, Cpu, RefreshCw, Timer, TriangleAlert } from "lucide-react";
import { api, describeError } from "../api";
import type { AgentStudioAnalytics } from "../types";
import { Alert, Badge, Button, Card, CardBody, CardHeader } from "./ui";

function number(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : new Intl.NumberFormat().format(value);
}

function percent(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : `${value.toFixed(1)}%`;
}

function duration(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
  return `${(value / 60_000).toFixed(1)} min`;
}

function Metric({ label, value, note }: { label: string; value: string; note?: string }) {
  return <div className="min-w-0 rounded-lg border border-border bg-surface-2/50 p-3">
    <p className="text-xs text-muted">{label}</p>
    <p className="mt-1 truncate text-xl font-semibold tabular-nums">{value}</p>
    {note && <p className="mt-1 text-[11px] text-muted">{note}</p>}
  </div>;
}

export function AgentStudioAnalyticsPanel() {
  const [data, setData] = useState<AgentStudioAnalytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const result = await api.agentStudioAnalytics(30);
      setData(result);
      setError(null);
    } catch (err) {
      setError(describeError(err));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const interval = window.setInterval(() => { void load(true); }, 15_000);
    return () => window.clearInterval(interval);
  }, [load]);

  const maxDaily = useMemo(() => Math.max(1, ...(data?.daily.map((row) => row.completed + row.failed) ?? [])), [data]);
  const maxAgent = useMemo(() => Math.max(1, ...(data?.agents.map((agent) => agent.total_runs) ?? [])), [data]);

  if (loading && !data) return <Card><CardBody className="py-8 text-sm text-muted">Loading measured execution analytics…</CardBody></Card>;
  if (error && !data) return <Alert tone="danger" title="Analytics could not be loaded">{error}</Alert>;
  if (!data) return null;

  const totals = data.totals;
  const system = data.system;
  return <div className="grid gap-4">
    {error && <Alert tone="warning" title="Showing the last successful snapshot">{error}</Alert>}
    {!data.worker_enabled && <Alert tone="warning" title="Task worker is disabled">
      New tasks are saved safely in the queue but will wait until <code>AGENT_STUDIO_WORKER_ENABLED=true</code> is configured on the designated API worker process.
    </Alert>}
    {data.worker_enabled && !data.worker_running && <Alert tone="warning" title="Task worker is not running">
      The worker is enabled in configuration but is not active in this process. Check that this instance owns the worker role and review its startup logs.
    </Alert>}

    <Card>
      <CardHeader title="Agent performance" description={`Database-backed results for the last ${data.period_days} days.`} icon={<Activity />} actions={<div className="flex items-center gap-2"><Badge tone={data.worker_running ? "success" : "warning"} dot>{data.worker_running ? "Worker running" : "Worker inactive"}</Badge><Button size="sm" variant="secondary" onClick={() => void load()}><RefreshCw /> Refresh</Button></div>} />
      <CardBody className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Tasks completed" value={number(totals.completed)} note={`${percent(totals.success_rate)} of terminal tasks`} />
        <Metric label="Tasks failed" value={number(totals.failed)} note={`${percent(totals.failure_rate)} of terminal tasks`} />
        <Metric label="Open tasks" value={number(totals.queued + totals.running + totals.awaiting_approval)} note={`${totals.queued} queued · ${totals.running} running · ${totals.awaiting_approval} need approval`} />
        <Metric label="Average execution time" value={duration(totals.average_execution_ms)} note={`Queue wait ${duration(totals.average_queue_latency_ms)}`} />
        <Metric label="LLM calls" value={number(totals.llm_calls)} />
        <Metric label="Prompt tokens" value={number(totals.prompt_tokens)} />
        <Metric label="Completion tokens" value={number(totals.completion_tokens)} />
        <Metric label="Estimated model cost" value={totals.estimated_cost_usd === null ? "Not configured" : `$${totals.estimated_cost_usd.toFixed(4)}`} note={`${number(totals.runs_without_cost)} runs have no configured rate`} />
      </CardBody>
      {totals.estimated_cost_usd === null && <p className="px-5 pb-4 text-xs text-muted">Provider invoice costs are not inferred from token counts. Add an exact provider:model rate card in <code>LLM_MODEL_PRICING_JSON</code> to show a cost estimate.</p>}
    </Card>

    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(320px,0.75fr)]">
      <Card>
        <CardHeader title="Completed and failed tasks by day" description="Each bar is calculated from saved run records. Hover or focus a day to inspect its count." icon={<Timer />} />
        <CardBody>
          {totals.total_runs === 0 ? <p className="py-8 text-center text-sm text-muted">No runs were recorded in this period.</p> : <>
            <div className="flex items-center gap-4 pb-2 text-xs text-muted"><span className="inline-flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-success" /> Completed</span><span className="inline-flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-danger" /> Failed</span></div>
            <svg className="h-40 w-full overflow-visible" viewBox={`0 0 ${data.daily.length * 18} 132`} preserveAspectRatio="none" role="img" aria-label="Daily completed and failed task counts">
              <line x1="0" y1="130" x2={data.daily.length * 18} y2="130" stroke="currentColor" className="text-border" />
              {data.daily.map((day, index) => {
                const x = index * 18 + 3;
                const doneHeight = day.completed ? Math.max(2, (day.completed / maxDaily) * 112) : 0;
                const failedHeight = day.failed ? Math.max(2, (day.failed / maxDaily) * 112) : 0;
                return <g key={day.date} tabIndex={0} aria-label={`${day.date}: ${day.completed} completed, ${day.failed} failed`}>
                  <title>{`${day.date}: ${day.completed} completed, ${day.failed} failed`}</title>
                  <rect x={x} y={130 - doneHeight} width="5" height={doneHeight} rx="2" className="fill-success" />
                  <rect x={x + 6} y={130 - failedHeight} width="5" height={failedHeight} rx="2" className="fill-danger" />
                </g>;
              })}
            </svg>
            <div className="flex justify-between pt-1 text-[10px] text-muted"><span>{data.daily[0]?.date}</span><span>{data.daily.at(-1)?.date}</span></div>
          </>}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Agent comparison" description="Run volume and success rate for each owner-scoped agent." icon={<Cpu />} />
        <CardBody className="grid gap-3">
          {data.agents.length ? data.agents.map((agent) => <div key={agent.id} className="grid gap-1.5">
            <div className="flex items-center justify-between gap-3 text-xs"><strong className="truncate">{agent.name}</strong><span className="shrink-0 text-muted">{number(agent.total_runs)} runs · {percent(agent.success_rate)}</span></div>
            <div className="h-2 overflow-hidden rounded-full bg-surface-2" title={`${agent.completed} completed, ${agent.failed} failed`}>
              <div className="flex h-full" style={{ width: `${Math.max(agent.total_runs ? 4 : 0, (agent.total_runs / maxAgent) * 100)}%` }}>
                <div className="h-full bg-success" style={{ width: `${agent.total_runs ? (agent.completed / agent.total_runs) * 100 : 0}%` }} />
                <div className="h-full bg-danger" style={{ width: `${agent.total_runs ? (agent.failed / agent.total_runs) * 100 : 0}%` }} />
              </div>
            </div>
            <p className="text-[11px] text-muted">{duration(agent.average_execution_ms)} average · {number(agent.prompt_tokens + agent.completion_tokens)} tokens</p>
          </div>) : <p className="py-6 text-sm text-muted">Create an agent and run a task to start comparison.</p>}
        </CardBody>
      </Card>
    </div>

    <div className="grid items-start gap-4 xl:grid-cols-2">
      <Card>
        <CardHeader title="Usage & cost coverage" description="Measured token totals come from provider responses, including delegated agent calls." />
        <CardBody className="grid gap-3 sm:grid-cols-2">
          <Metric label="LLM calls recorded" value={number(totals.llm_calls)} />
          <Metric label="Runs without a cost rate" value={number(totals.runs_without_cost)} />
          <p className="sm:col-span-2 text-xs leading-relaxed text-muted">Cost is shown only when an exact provider:model rate is configured. The app does not guess provider prices or treat token counts as invoice data.</p>
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Host resources" description="Live process and hardware measurements from this API process." />
        <CardBody>
          {system?.available ? <div className="grid grid-cols-2 gap-3">
            <Metric label="Host CPU" value={percent(system.host_cpu_percent)} />
            <Metric label="API process CPU" value={percent(system.process_cpu_percent)} />
            <Metric label="Host RAM used" value={percent(system.host_memory_percent)} note={`Available ${system.host_memory_available_bytes === null ? "—" : `${(system.host_memory_available_bytes / 1024 ** 3).toFixed(1)} GiB`}`} />
            <Metric label="API process RAM" value={system.process_rss_bytes === null ? "—" : `${(system.process_rss_bytes / 1024 ** 3).toFixed(2)} GiB`} />
            <div className="col-span-2 rounded-lg border border-border bg-surface-2/50 p-3 text-xs">
              <p className="font-medium">GPU</p>
              {system.gpu_available ? <p className="mt-1 text-muted">{percent(system.gpu_utilization_percent)} utilization · {system.gpu_memory_used_bytes === null || system.gpu_memory_total_bytes === null ? "memory unavailable" : `${(system.gpu_memory_used_bytes / 1024 ** 3).toFixed(1)} / ${(system.gpu_memory_total_bytes / 1024 ** 3).toFixed(1)} GiB`}</p> : <p className="mt-1 text-muted">No NVIDIA GPU telemetry is available on this host.</p>}
            </div>
          </div> : <div className="flex items-start gap-2 rounded-lg border border-border p-3 text-sm text-muted"><TriangleAlert className="mt-0.5 size-4 shrink-0" /> Host resource collection is unavailable in this environment.</div>}
        </CardBody>
      </Card>
    </div>
  </div>;
}
