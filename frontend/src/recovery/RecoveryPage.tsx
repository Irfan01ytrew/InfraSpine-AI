import { useCallback, useEffect, useState } from "react";
import { Activity, AlertTriangle, ArrowRight, Clock3, Layers, Plus, RefreshCw, Search, ShieldCheck } from "lucide-react";
import { describeError } from "../api";
import { Alert, Badge, Button, Card, CardBody, CardHeader } from "../components/ui";
import type { User } from "../types";
import { recovery, type Config, type Dashboard, type Detail, type Workspace } from "./api";
import { date, field, IncidentDetail, label, Status } from "./IncidentDetail";

export function RecoveryPage({ user }: { user: User }) {
  const [spaces, setSpaces] = useState<Workspace[]>([]);
  const [tenant, setTenant] = useState("");
  const [data, setData] = useState<Dashboard | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [selected, setSelected] = useState("");
  const [tab, setTab] = useState("incidents");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [severity, setSeverity] = useState("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const [workspaceName, setWorkspaceName] = useState("");
  const loadSpaces = useCallback(async () => { const result = await recovery.workspaces(); setSpaces(result.workspaces); setTenant(t => t || result.workspaces[0]?.id || ""); }, []);
  useEffect(() => { void loadSpaces().catch(e => setError(describeError(e))); }, [loadSpaces]);
  useEffect(() => {
    if (!tenant) return;
    const controller = new AbortController();
    let pending = false;
    const refresh = async () => {
      if (pending) return;
      pending = true;
      try {
        const query = new URLSearchParams({ search, offset: String(offset) });
        if (tab === "approvals") query.set("status", "awaiting_approval"); else if (status) query.set("status", status);
        if (severity) query.set("severity", severity);
        const [dashboard, configuration, current] = await Promise.all([
          recovery.dashboard(tenant, query.toString(), controller.signal), recovery.config(tenant, controller.signal),
          selected ? recovery.detail(tenant, selected, controller.signal) : Promise.resolve(null),
        ]);
        if (!controller.signal.aborted) { setData(dashboard); setConfig(configuration); setDetail(current); }
      } catch (e) { if (!controller.signal.aborted) setError(describeError(e)); }
      finally { pending = false; }
    };
    void refresh(); const timer = window.setInterval(() => void refresh(), 10000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [tenant, selected, search, status, severity, offset, tab, tick]);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await fn(); setTick(t => t + 1); }
    catch (e) { setError(describeError(e)); }
    finally { setBusy(false); }
  };
  const owner = config?.role === "owner";
  return <div className="space-y-6" data-testid="recovery-page">
    <header className="flex flex-wrap items-start justify-between gap-4"><div><div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-primary"><ShieldCheck size={15} />Operations / Module 07</div><h1 className="text-2xl font-semibold tracking-tight text-foreground">Exception & Recovery</h1><p className="mt-1 text-sm text-muted">Detect, investigate and restore with evidence at every step.</p></div><div className="flex items-center gap-2">{spaces.length > 0 && <select aria-label="Recovery workspace" className={field} value={tenant} onChange={e => { setTenant(e.target.value); setSelected(""); setDetail(null); setData(null); setConfig(null); setOffset(0); }}>{spaces.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select>}<Button aria-label="Refresh recovery" variant="secondary" disabled={busy} onClick={() => setTick(t => t + 1)}><RefreshCw size={16} /></Button></div></header>
    {error && <Alert tone="danger"><span role="alert">{error}</span></Alert>}
    {spaces.length === 0 && <Card><CardHeader title="A workspace for operational resilience" description="Recovery workspaces isolate incidents, resources, approvals and evidence by tenant membership." /><CardBody>{user.role === "admin" ? <form className="flex flex-wrap gap-3" onSubmit={e => { e.preventDefault(); void act(async () => { const row = await recovery.createWorkspace(workspaceName); await loadSpaces(); setTenant(row.id); }); }}><input aria-label="New recovery workspace" className={`${field} max-w-sm`} required minLength={2} maxLength={120} placeholder="Workspace name" value={workspaceName} onChange={e => setWorkspaceName(e.target.value)} /><Button type="submit" disabled={busy}><Plus size={15} />Create workspace</Button></form> : <p className="text-sm text-muted">Ask an administrator to provision a workspace and grant your recovery role.</p>}</CardBody></Card>}
    {tenant && !data && <p className="text-sm text-muted" role="status">Loading persisted incident state…</p>}
    {data && config && <>
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">{[
        { name: "Active incidents", value: data.metrics.active, detail: `${data.metrics.overdue} overdue SLA`, icon: AlertTriangle },
        { name: "Verified recovery", value: data.metrics.recovery_success_percent === null ? "—" : `${data.metrics.recovery_success_percent}%`, detail: "Success / terminal recovery commands", icon: ShieldCheck },
        { name: "Mean time to restore", value: data.metrics.mttr_seconds === null ? "—" : `${Math.round(data.metrics.mttr_seconds)}s`, detail: "Verified closures only", icon: Clock3 },
        { name: "Recurring incidents", value: data.metrics.recurring, detail: "Failures reopened after closure", icon: Activity },
      ].map(m => <Card key={m.name}><CardBody><div className="flex items-center justify-between text-xs text-muted"><span>{m.name}</span><m.icon size={16} /></div><p className="my-2 text-3xl font-semibold tabular-nums text-foreground">{m.value}</p><p className="text-xs text-muted">{m.detail}</p></CardBody></Card>)}</div>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3"><nav className="flex flex-wrap gap-2" aria-label="Recovery sections">{["incidents", "approvals", "playbooks", "services"].map(t => <Button key={t} variant={tab === t ? "primary" : "ghost"} onClick={() => { setTab(t); setOffset(0); }}>{label(t)}{t === "approvals" && <Badge>{data.metrics.by_state.awaiting_approval ?? 0}</Badge>}</Button>)}</nav><p className="text-xs text-muted">Observed {date(data.as_of)} · refresh 10s</p></div>
      {(tab === "incidents" || tab === "approvals") && <>
        <div className="flex flex-wrap items-center gap-3"><div className="relative min-w-48 flex-1"><Search size={15} className="absolute left-3 top-3 text-muted" /><input className={`${field} pl-9`} aria-label="Search incidents" placeholder="Search incidents…" value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} /></div><select aria-label="Incident severity" className={`${field} max-w-40`} value={severity} onChange={e => { setSeverity(e.target.value); setOffset(0); }}><option value="">All severities</option>{["critical", "high", "medium", "low"].map(s => <option key={s}>{s}</option>)}</select>{tab !== "approvals" && <select aria-label="Incident state" className={`${field} max-w-48`} value={status} onChange={e => { setStatus(e.target.value); setOffset(0); }}><option value="">All states</option>{["detected", "triaged", "investigating", "awaiting_approval", "recovering", "verifying", "resolved", "escalated", "reopened", "cancelled"].map(s => <option key={s} value={s}>{label(s)}</option>)}</select>}</div>
        <div className={`grid items-start gap-5 ${selected ? "xl:grid-cols-[minmax(280px,0.8fr)_minmax(0,1.2fr)]" : "grid-cols-1"}`}><Card><CardHeader title={tab === "approvals" ? "Human intervention queue" : "Incident register"} description={`${data.total} matching incidents`} /><CardBody className="space-y-2">{!data.incidents.length && <div className="py-10 text-center"><ShieldCheck className="mx-auto mb-3 text-muted" /><p className="text-sm">No matching incidents</p><p className="mt-1 text-xs text-muted">Connected monitors and authenticated event producers populate this register.</p></div>}{data.incidents.map(i => <button key={i.id} data-testid="incident-row" className={`w-full rounded-lg border p-3 text-left transition-colors hover:bg-surface-2 ${selected === i.id ? "border-primary bg-primary-soft" : "border-border"}`} onClick={() => { setDetail(null); setSelected(i.id); }}><div className="mb-2 flex flex-wrap items-center gap-2"><Status value={i.severity} /><Status value={i.status} /></div><div className="flex justify-between gap-3"><strong className="text-sm">{i.title}</strong><ArrowRight size={15} className="shrink-0 text-muted" /></div><p className="mt-2 text-xs text-muted">{data.targets.find(t => t.id === i.target_id)?.name ?? i.source} · {i.occurrences} events · {date(i.last_seen)}</p></button>)}<div className="flex items-center justify-between pt-3"><Button variant="ghost" disabled={offset === 0} onClick={() => setOffset(o => Math.max(0, o - 50))}>Previous</Button><span className="text-xs text-muted">{data.total ? offset + 1 : 0}–{Math.min(offset + 50, data.total)} / {data.total}</span><Button variant="ghost" disabled={offset + 50 >= data.total} onClick={() => setOffset(o => o + 50)}>Next</Button></div></CardBody></Card>
          {selected && (detail ? <IncidentDetail key={detail.incident.id} tenant={tenant} detail={detail} config={config} target={data.targets.find(t => t.id === detail.incident.target_id)} busy={busy} act={act} /> : <p className="text-sm text-muted">Loading verified evidence…</p>)}
        </div></>}
      {tab === "playbooks" && <div className="space-y-4">{owner && <PlaybookForm tenant={tenant} config={config} busy={busy} act={act} />}{config.playbooks.length === 0 && <p className="text-sm text-muted">No playbooks configured. Available actions come from connected owner modules.</p>}{config.playbooks.map(p => <Card key={p.id}><CardBody className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="font-semibold">{p.name}</h3><p className="mt-1 text-xs text-muted">{p.adapter} / {p.action} · {p.max_attempts} attempts · {p.verify_samples} healthy samples</p><div className="mt-2 flex gap-2"><Status value={p.risk} /><Badge>{p.preapproved ? "Preapproved policy" : "Separate human approval"}</Badge><Badge>{p.enabled ? "Enabled" : "Disabled"}</Badge></div></div>{owner && <Button variant="secondary" disabled={busy} onClick={() => void act(() => recovery.toggle(tenant, "playbooks", p.id, !p.enabled))}>{p.enabled ? "Disable" : "Enable"}</Button>}</CardBody></Card>)}</div>}
      {tab === "services" && <div className="space-y-4">{owner && <TargetForm tenant={tenant} config={config} busy={busy} act={act} />}<div className="grid gap-4 md:grid-cols-2">{data.targets.map(t => <Card key={t.id}><CardHeader title={t.name} icon={<Layers />} actions={<Badge tone={t.healthy === true ? "success" : t.healthy === false ? "danger" : "neutral"}>{!t.enabled ? "Paused" : !t.last_checked || Date.now() - Date.parse(t.last_checked) > t.check_interval * 2000 ? "Unknown / stale" : t.healthy ? "Healthy" : "Degraded"}</Badge>} /><CardBody><p className="text-xs text-muted">{t.adapter} · last observed {date(t.last_checked)}</p><p className="mt-2 text-xs text-muted">Checks every {t.check_interval}s · threshold {t.threshold} · SLA {t.sla_seconds}s</p>{owner && <Button className="mt-3" variant="secondary" disabled={busy} onClick={() => void act(() => recovery.toggle(tenant, "targets", t.id, !t.enabled))}>{t.enabled ? "Pause monitoring" : "Enable monitoring"}</Button>}</CardBody></Card>)}</div>{data.targets.length === 0 && <p className="text-sm text-muted">Connect an owned service to begin health monitoring.</p>}</div>}
    </>}
  </div>;
}

type FormProps = { tenant: string; config: Config; busy: boolean; act: (fn: () => Promise<unknown>) => Promise<void> };
function TargetForm({ tenant, config, busy, act }: FormProps) {
  const [adapter, setAdapter] = useState(config.adapters[0]?.id ?? "");
  return <Card><CardHeader title="Connect a monitored resource" description="Resource identity is validated by the owning adapter. Arbitrary URLs and commands are not accepted." /><CardBody><form className="grid gap-3 sm:grid-cols-2" onSubmit={e => { e.preventDefault(); const f = new FormData(e.currentTarget); void act(() => recovery.post(tenant, "targets", { name: f.get("name"), owner_id: f.get("owner"), adapter, resource: f.get("resource"), threshold: Number(f.get("threshold")), sla_seconds: Number(f.get("sla")), check_interval: Number(f.get("interval")) })); }}>
    <label className="text-xs">Service name<input name="name" className={field} required minLength={2} maxLength={120} /></label><label className="text-xs">Connected adapter<select className={field} value={adapter} onChange={e => setAdapter(e.target.value)}>{config.adapters.map(a => <option key={a.id}>{a.id}</option>)}</select></label><label className="text-xs">Resource identifier<input name="resource" className={field} required maxLength={160} placeholder={adapter === "database" ? "primary" : "Owned resource ID"} /></label><label className="text-xs">Resource owner<select name="owner" className={field} defaultValue={config.user_id}>{config.members.filter(m => m.active && m.role !== "observer").map(m => <option key={m.user_id} value={m.user_id}>{m.user_id === config.user_id ? "Me" : m.user_id} · {m.role}</option>)}</select></label><label className="text-xs">Failure threshold<input name="threshold" type="number" min={1} max={100} defaultValue={1} className={field} required /></label><label className="text-xs">SLA (seconds)<input name="sla" type="number" min={30} max={604800} defaultValue={3600} className={field} required /></label><label className="text-xs">Health interval (seconds)<input name="interval" type="number" min={10} max={86400} defaultValue={60} className={field} required /></label><div className="self-end"><Button type="submit" disabled={busy || !adapter}>Connect service</Button></div>
  </form></CardBody></Card>;
}
function PlaybookForm({ tenant, config, busy, act }: FormProps) {
  const [adapter, setAdapter] = useState(config.adapters[0]?.id ?? "");
  const actions = config.adapters.find(a => a.id === adapter)?.actions ?? [];
  return <Card><CardHeader title="Create an immutable recovery playbook" description="New definitions are versioned as separate records. Disabling a policy blocks dispatch and retries." /><CardBody><form className="grid gap-3 sm:grid-cols-2" onSubmit={e => { e.preventDefault(); const f = new FormData(e.currentTarget); void act(() => recovery.post(tenant, "playbooks", { name: f.get("name"), adapter, action: f.get("action"), preapproved: f.get("preapproved") === "on", max_attempts: Number(f.get("attempts")) })); }}><label className="text-xs">Playbook name<input className={field} name="name" required minLength={3} maxLength={120} /></label><label className="text-xs">Owner adapter<select className={field} value={adapter} onChange={e => setAdapter(e.target.value)}>{config.adapters.map(a => <option key={a.id}>{a.id}</option>)}</select></label><label className="text-xs">Authorized capability<select key={adapter} name="action" className={field} required>{actions.map(a => <option key={a}>{a}</option>)}</select></label><label className="text-xs">Execution attempt limit<input name="attempts" type="number" min={1} max={5} defaultValue={3} className={field} required /></label><label className="flex items-center gap-2 text-xs"><input type="checkbox" name="preapproved" />Preapprove eligible low-risk actions</label><div><Button type="submit" disabled={busy || !actions.length}>Save playbook</Button></div></form></CardBody></Card>;
}
