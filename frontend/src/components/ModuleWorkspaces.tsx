import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { RunsPanel } from "../workflows/RunsPanel";
import { Activity, ArrowRight, Boxes, Check, CircleAlert, Clock3, Database, Gauge, Layers3, Network, RefreshCw, ShieldCheck, Sparkles, Workflow } from "lucide-react";
import { describeError, request } from "../api";
import { graphApi } from "../company-graph/api";
import { DigitalTwinWorkspace } from "../digital-twin/TwinWorkspace";
import { GraphMap } from "../company-graph/GraphMap";
import type { Graph, GraphRecord, Quality, Tenant } from "../company-graph/types";
import { workflowApi } from "../workflows/api";
import type { Dashboard as WorkflowDashboard, Scope, Spec, Template, Workflow as WorkflowRow } from "../workflows/types";
import { bc } from "../continuity/api";
import type { Dashboard as ContinuityDashboard, Workspace } from "../continuity/types";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Input, PageHeader, Skeleton, Stat, Textarea } from "./ui";

export type WorkspaceView = "company-graph" | "digital-twin" | "workflow-engine" | "governance" | "continuity" | "frontend-ux" | "integrations" | "infrastructure" | "quality";
type Navigate = (view: string) => void;

export function ModuleWorkspacesPage({ view, onNavigate }: { view: WorkspaceView; onNavigate: Navigate }) {
  switch (view) {
    case "company-graph": return <CompanyGraphWorkspace />;
    case "digital-twin": return <DigitalTwinWorkspace />;
    case "workflow-engine": return <WorkflowEngineWorkspace />;
    case "governance": return <GovernanceWorkspace onNavigate={onNavigate} />;
    case "continuity": return <ContinuityWorkspace />;
    case "frontend-ux": return <FrontendUxWorkspace onNavigate={onNavigate} />;
    case "integrations": return <IntegrationsWorkspace />;
    case "infrastructure": return <InfrastructureWorkspace />;
    case "quality": return <QualityWorkspace />;
  }
  return <EmptyModule title="Module workspace unavailable" description="This module has no configured workspace route." />;
}

function InstructionButton({ module }: { module: string }) {
  return <Button variant="primary" onClick={() => { window.location.hash = `/module-control-center?module=${module}`; }}><Sparkles /> Give module an instruction</Button>;
}

function ModulePageHeader({ eyebrow, title, description, module, actions }: { eyebrow: string; title: string; description: string; module: string; actions?: ReactNode }) {
  return <PageHeader eyebrow={eyebrow} title={title} description={description} actions={<>{actions}<InstructionButton module={module} /></>} />;
}

function ErrorAlert({ error }: { error: string }) {
  return <Alert tone="danger" title="Live module data could not be loaded">{error}</Alert>;
}

function EmptyModule({ title, description }: { title: string; description: string }) {
  return <Card><EmptyState icon={<Boxes />} title={title} description={description} /></Card>;
}

export function CompanyGraphWorkspace() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [graph, setGraph] = useState<Graph | null>(null);
  const [quality, setQuality] = useState<Quality | null>(null);
  const [changes, setChanges] = useState<{ id: string; status: string; change_type: string; reason: string | null }[]>([]);
  const [selected, setSelected] = useState<GraphRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true;
    graphApi.tenants().then(({ items }) => { if (!live) return; setTenants(items); setTenantId(current => current || items[0]?.id || ""); })
      .catch(err => { if (live) setError(describeError(err)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    if (!tenantId) { setGraph(null); setQuality(null); setChanges([]); return; }
    let live = true;
    setLoading(true); setError(""); setSelected(null);
    Promise.all([graphApi.map(tenantId), graphApi.quality(tenantId), graphApi.changes(tenantId)])
      .then(([nextGraph, nextQuality, nextChanges]) => { if (!live) return; setGraph(nextGraph); setQuality(nextQuality); setChanges(nextChanges.items); })
      .catch(err => { if (live) setError(describeError(err)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [tenantId, revision]);
  const expand = useCallback(async (id: string) => {
    try { setGraph(await graphApi.neighbors(tenantId, id, 2)); }
    catch (err) { setError(describeError(err)); }
  }, [tenantId]);
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M03" eyebrow="MODULE 03 · COMPANY INTELLIGENCE" title="Company intelligence graph" description="Inspect tenant-scoped company entities, relationships, source quality and proposed changes." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)} disabled={loading}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>} />
    {error && <ErrorAlert error={error} />}
    {loading && !graph && <Skeleton className="h-72 w-full" />}
    {!loading && !tenants.length && !error && <EmptyModule title="No graph workspace is available" description="This account has no company graph tenant. Create or grant a tenant in Company intelligence setup before graph data can appear." />}
    {!!tenants.length && <>
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface p-4 shadow-xs">
        <label className="grid min-w-60 gap-1 text-xs font-semibold text-foreground-2">Company graph workspace<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={tenantId} onChange={event => setTenantId(event.target.value)}>{tenants.map(item => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}</select></label>
        <Badge tone="success" dot>Tenant scope enforced</Badge>
      </section>
      {quality && <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="Entities" value={quality.entity_count} hint="Visible in this graph scope" icon={<Network />} /><Stat label="Relationships" value={quality.relationship_count} hint="Current graph snapshot" icon={<Activity />} /><Stat label="Ownership gaps" value={quality.ownership_gaps.length} hint="Require a responsible owner" icon={<CircleAlert />} /><Stat label="Missing backups" value={quality.missing_backups.length} hint="No authorized backup found" icon={<ShieldCheck />} /></div>}
      {graph && <Card><CardHeader title="Live company map" description={`${graph.entities.length} visible entities · ${graph.relationships.length} visible relationships · snapshot ${new Date(graph.as_of).toLocaleString()}`} icon={<Network />} /><div className="cg-workspace-map">{graph.entities.length ? <GraphMap graph={graph} selected={selected?.id} onSelect={record => setSelected(record)} onExpand={id => void expand(id)} /> : <EmptyState title="The graph is empty" description="Add approved source records to make company relationships visible here." />}</div></Card>}
      <div className="grid gap-4 xl:grid-cols-[1fr_0.8fr]">
        <Card><CardHeader title="Proposed changes" description="Changes remain pending until an authorized reviewer decides." icon={<Clock3 />} /><CardBody className="grid gap-2">{changes.length ? changes.slice(0, 8).map(change => <div key={change.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="min-w-0 truncate text-sm">{change.change_type.replaceAll("_", " ")}{change.reason ? ` · ${change.reason}` : ""}</span><Badge tone={change.status === "pending" ? "warning" : change.status === "approved" ? "success" : "neutral"}>{change.status}</Badge></div>) : <p className="text-sm text-muted">No proposed changes in the visible queue.</p>}</CardBody></Card>
        <Card><CardHeader title="Selected record" description="Select a graph node to inspect its visible details." icon={<Database />} /><CardBody>{selected ? <div className="grid gap-2 text-sm"><div className="flex items-center justify-between gap-2"><strong>{selected.name}</strong><Badge>{selected.kind.replaceAll("_", " ")}</Badge></div><p className="text-muted">{selected.record_type} · {selected.visibility} · confidence {Math.round(selected.confidence * 100)}%</p><p className="break-all font-mono text-xs text-muted">{selected.external_key}</p><Button variant="secondary" onClick={() => void expand(selected.id)}>Expand relationships <ArrowRight /></Button></div> : <p className="text-sm text-muted">Choose a node in the map. Double-click it to expand the nearby graph.</p>}</CardBody></Card>
      </div>
    </>}
  </div>;
}

export function WorkflowEngineWorkspace() {
  const [scopes, setScopes] = useState<Scope[]>([]);
  const [scopeId, setScopeId] = useState("");
  const [dashboard, setDashboard] = useState<WorkflowDashboard | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowRow[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [requirements, setRequirements] = useState("");
  const [draft, setDraft] = useState<{ spec: Spec; errors: { code: string; message: string; node_id: string | null }[]; requires_review: boolean } | null>(null);
  const [draftName, setDraftName] = useState("New workflow draft");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true;
    Promise.all([workflowApi.scopes(), workflowApi.templates()]).then(([scopeResult, templateResult]) => {
      if (!live) return; setScopes(scopeResult.scopes); setScopeId(current => current || scopeResult.scopes[0]?.id || ""); setTemplates(templateResult.templates);
    }).catch(err => { if (live) setError(describeError(err)); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    if (!scopeId) return;
    let live = true;
    Promise.all([workflowApi.dashboard(scopeId), workflowApi.workflows(scopeId)])
      .then(([nextDashboard, list]) => { if (live) { setDashboard(nextDashboard); setWorkflows(list.workflows); } })
      .catch(err => { if (live) setError(describeError(err)); });
    return () => { live = false; };
  }, [scopeId, revision]);
  const counts = dashboard?.counts ?? {};
  async function createDraft(event: FormEvent) {
    event.preventDefault(); if (!scopeId || requirements.trim().length < 10) return;
    setBusy(true); setError("");
    try { setDraft(await workflowApi.draft(scopeId, requirements.trim())); }
    catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }
  async function saveDraft() {
    if (!scopeId || !draft) return;
    setBusy(true); setError("");
    try { await workflowApi.save(scopeId, draftName.trim() || "Workflow draft", "AI-assisted draft. Review the definition and approvals before publishing.", draft.spec); setDraft(null); setRequirements(""); setRevision(value => value + 1); }
    catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M05" eyebrow="MODULE 05 · OPERATIONS" title="Universal workflow engine" description="Draft, validate and review tenant-scoped workflows. Execution requires a reviewed published version and an enabled worker." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)}><RefreshCw /> Refresh</Button>} />
    {error && <ErrorAlert error={error} />}
    {scopes.length > 0 && <label className="grid max-w-lg gap-1 text-xs font-semibold text-foreground-2">Workflow scope<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={scopeId} onChange={event => setScopeId(event.target.value)}>{scopes.map(item => <option key={item.id} value={item.id}>{item.name}{item.can_manage ? " · manager" : ""}</option>)}</select></label>}
    {!scopes.length && !error && <EmptyModule title="No authorized workflow scope" description="Workflow lists are limited to the signed-in user's company scopes. Ask an administrator to grant a workflow role." />}
    {scopeId && <>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="Total runs" value={dashboard?.total_runs ?? "—"} hint="Persisted workflow history" icon={<Activity />} /><Stat label="Completion rate" value={dashboard?.completion_rate == null ? "—" : `${Math.round(dashboard.completion_rate * 100)}%`} hint="Recorded successful runs" icon={<Check />} /><Stat label="Waiting for review" value={dashboard?.waiting_count ?? "—"} hint="Human task or approval queue" icon={<Clock3 />} /><Stat label="Overdue steps" value={dashboard?.overdue_steps ?? "—"} hint="Requires authorized review" icon={<CircleAlert />} /></div>
      <Card><CardHeader title="Draft a workflow from instructions" description="This creates a draft for review. It never publishes or executes the generated workflow." icon={<Sparkles />} /><CardBody><form className="grid gap-3" onSubmit={event => void createDraft(event)}><Textarea value={requirements} onChange={event => setRequirements(event.target.value)} rows={3} maxLength={8000} minLength={10} placeholder="Describe the process, people involved, decision points and required evidence…" /><div className="flex flex-wrap items-center justify-between gap-3"><span className="text-xs text-muted">Available templates: {templates.length} · adapters: review permissions in scope</span><Button type="submit" variant="primary" loading={busy} disabled={requirements.trim().length < 10}>Create reviewed draft</Button></div></form>
        {draft && <div className="mt-4 rounded-xl border border-primary/20 bg-primary-soft/30 p-4"><div className="flex flex-wrap items-end justify-between gap-3"><div className="grid min-w-60 gap-2"><Badge tone="primary">Draft · review required</Badge><label className="grid gap-1 text-xs font-medium">Draft name<Input value={draftName} onChange={event => setDraftName(event.target.value)} maxLength={160} /></label><p className="text-sm font-semibold">{draft.spec.nodes.length} steps · {draft.errors.length} validation findings</p></div><Button variant="primary" onClick={() => void saveDraft()} loading={busy} disabled={!draft.spec.nodes.length || !draftName.trim()}>Save draft to workflow list</Button></div><div className="mt-3 grid gap-2">{draft.errors.map((item, index) => <Alert key={`${item.code}-${index}`} tone={item.code === "error" ? "danger" : "warning"}>{item.message}</Alert>)}</div><details className="mt-3"><summary className="cursor-pointer text-xs font-medium">Review generated workflow definition</summary><pre className="mt-2 max-h-72 overflow-auto rounded-lg bg-surface p-3 text-xs">{JSON.stringify(draft.spec, null, 2)}</pre></details></div>}
      </CardBody></Card>
      <Card><CardHeader title="Workflow definitions" description="Review draft and published versions before enabling event or scheduled execution." icon={<Workflow />} /><CardBody className="grid gap-2">{workflows.length ? workflows.map(item => <div key={item.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="font-semibold">{item.name}</p><p className="text-xs text-muted">Version {item.latest_version} · updated {new Date(item.updated_at).toLocaleString()}</p></div><div className="flex items-center gap-2"><Badge tone={item.enabled ? "success" : "neutral"}>{item.enabled ? "Enabled" : item.latest_status ?? "Draft"}</Badge><Badge>{item.latest_status ?? "unknown"}</Badge></div></div>) : <p className="text-sm text-muted">No workflow definitions are visible in this scope yet.</p>}</CardBody></Card>
      <RunsPanel scopeId={scopeId} revision={revision} />
      {Object.keys(counts).length > 0 && <Card><CardHeader title="Run queue" description={`Current workflow snapshot: ${dashboard?.as_of ? new Date(dashboard.as_of).toLocaleString() : "loading"}.`} icon={<Layers3 />} /><CardBody className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{Object.entries(counts).map(([status, count]) => <div key={status} className="rounded-lg border border-border p-3"><p className="text-xs capitalize text-muted">{status.replaceAll("_", " ")}</p><p className="mt-1 text-xl font-semibold">{count}</p></div>)}</CardBody></Card>}
    </>}
  </div>;
}

export function ContinuityWorkspace() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [dashboard, setDashboard] = useState<ContinuityDashboard | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => { let live = true; bc.workspaces().then(result => { if (live) { setWorkspaces(result.items); setWorkspaceId(current => current || result.items[0]?.id || ""); } }).catch(err => { if (live) setError(describeError(err)); }).finally(() => { if (live) setLoading(false); }); return () => { live = false; }; }, []);
  useEffect(() => { if (!workspaceId) return; let live = true; setLoading(true); bc.dashboard(workspaceId).then(result => { if (live) setDashboard(result); }).catch(err => { if (live) setError(describeError(err)); }).finally(() => { if (live) setLoading(false); }); return () => { live = false; }; }, [workspaceId, revision]);
  const metrics = dashboard?.metrics;
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M08" eyebrow="MODULE 08 · RESILIENCE" title="Business continuity" description="Review business impact, critical resources, continuity plans, approvals and verified drills." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)} disabled={loading}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>} />
    {error && <ErrorAlert error={error} />}
    {workspaces.length > 0 && <label className="grid max-w-lg gap-1 text-xs font-semibold text-foreground-2">Continuity workspace<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={workspaceId} onChange={event => setWorkspaceId(event.target.value)}>{workspaces.map(item => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}</select></label>}
    {!workspaces.length && !loading && !error && <EmptyModule title="No continuity workspace is available" description="Continuity data is tenant-scoped. A workspace must be created and access granted before resources or plans can be shown." />}
    {metrics && <><div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="Critical resources" value={metrics.critical_resources} hint="Recorded in this workspace" icon={<Database />} /><Stat label="BIA assessed" value={metrics.bia_assessed} hint="Resources with impact assessment" icon={<Gauge />} /><Stat label="Plan coverage" value={metrics.plan_coverage_percent == null ? "—" : `${metrics.plan_coverage_percent}%`} hint={`${metrics.plan_covered} resources covered`} icon={<ShieldCheck />} /><Stat label="Verified drills" value={metrics.verified_drills} hint={`${metrics.active_recoveries} active recoveries`} icon={<Check />} /></div>
      {metrics.pending_approvals > 0 && <Alert tone="warning" title={`${metrics.pending_approvals} decision(s) need review`}>The current continuity scope has pending approval work.</Alert>}
      <div className="grid gap-4 xl:grid-cols-2"><Card><CardHeader title="Resources and dependencies" description={`${dashboard.nodes.length} resources · ${dashboard.links.length} dependency links`} icon={<Network />} /><CardBody className="grid gap-2">{dashboard.nodes.slice(0, 8).map(node => <div key={node.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="min-w-0 truncate text-sm">{node.name}</span><Badge tone={node.data.critical ? "warning" : "neutral"}>{node.data.critical ? "Critical" : node.kind}</Badge></div>)}{!dashboard.nodes.length && <p className="text-sm text-muted">No resources are recorded in this scope.</p>}</CardBody></Card><Card><CardHeader title="Continuity plans" description="Draft, approval and active status comes from saved plans." icon={<ShieldCheck />} /><CardBody className="grid gap-2">{dashboard.plans.length ? dashboard.plans.slice(0, 8).map(plan => <div key={plan.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{plan.title}</p><p className="text-xs text-muted">Version {plan.version} · review due {new Date(plan.review_due_at).toLocaleDateString()}</p></div><Badge>{plan.status}</Badge></div>) : <p className="text-sm text-muted">No continuity plans are available in this scope.</p>}</CardBody></Card></div>
      <Alert title="Evidence scope">{dashboard.metric_note}</Alert></>}
    {loading && !dashboard && <Skeleton className="h-56 w-full" />}
  </div>;
}

export function IntegrationsWorkspace() {
  const [tenants, setTenants] = useState<{ id: string; name: string; role: string }[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [catalog, setCatalog] = useState<{ id: string; name?: string; provider?: string; description?: string }[]>([]);
  const [overview, setOverview] = useState<{ role: string; connectors: { id: string; name: string; provider: string; enabled: boolean; health: string; credential_configured: boolean; last_sync_at: string | null }[]; jobs: { id: string; operation: string; status: string; created_at: string; last_error: string | null }[] } | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => { let live = true; Promise.all([request<{ items: typeof tenants }>("GET", "/api/integrations/tenants"), request<{ items: typeof catalog }>("GET", "/api/integrations/catalog")]).then(([a, b]) => { if (live) { setTenants(a.items); setTenantId(current => current || a.items[0]?.id || ""); setCatalog(b.items); } }).catch(err => { if (live) setError(describeError(err)); }); return () => { live = false; }; }, []);
  useEffect(() => { if (!tenantId) return; let live = true; request<typeof overview>("GET", `/api/integrations/${encodeURIComponent(tenantId)}/overview`).then(result => { if (live) setOverview(result); }).catch(err => { if (live) setError(describeError(err)); }); return () => { live = false; }; }, [tenantId, revision]);
  const ready = overview?.connectors.filter(item => item.enabled && item.health === "healthy").length ?? 0;
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M10" eyebrow="MODULE 10 · CONNECTIVITY" title="Integrations & communication" description="Review tenant-owned connectors, sync jobs and provider health. External dispatch stays approval-gated and worker-controlled." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)}><RefreshCw /> Refresh</Button>} />
    {error && <ErrorAlert error={error} />}
    {tenants.length > 0 && <label className="grid max-w-lg gap-1 text-xs font-semibold text-foreground-2">Integration tenant<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={tenantId} onChange={event => setTenantId(event.target.value)}>{tenants.map(item => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}</select></label>}
    {!tenants.length && !error && <EmptyModule title="No integration tenant is available" description="Connectors are isolated by company tenant and owner. Create or grant a company graph tenant before configuring integrations." />}
    {overview && <><div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="Configured connectors" value={overview.connectors.length} hint={`Visible to ${overview.role}`} icon={<Boxes />} /><Stat label="Healthy & enabled" value={ready} hint="Only verified healthy connectors" icon={<Check />} /><Stat label="Recent jobs" value={overview.jobs.length} hint="Latest saved job records" icon={<Activity />} /><Stat label="Needs attention" value={overview.jobs.filter(job => ["dead_letter", "uncertain", "pending_approval"].includes(job.status)).length} hint="Review before retrying or dispatch" icon={<CircleAlert />} /></div>
      <Card><CardHeader title="Connected services" description="Credentials stay encrypted server-side and are never shown in this dashboard." icon={<Boxes />} /><CardBody className="grid gap-2">{overview.connectors.length ? overview.connectors.map(connector => <div key={connector.id} className="grid gap-2 rounded-lg border border-border p-3 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center"><div className="min-w-0"><p className="truncate text-sm font-semibold">{connector.name}</p><p className="text-xs text-muted">{connector.provider} · credentials {connector.credential_configured ? "configured" : "not configured"}</p></div><Badge tone={connector.health === "healthy" && connector.enabled ? "success" : connector.health === "unverified" ? "warning" : "neutral"} dot>{connector.enabled ? connector.health : "disabled"}</Badge><span className="text-xs text-muted">{connector.last_sync_at ? `Synced ${new Date(connector.last_sync_at).toLocaleString()}` : "No sync recorded"}</span></div>) : <p className="text-sm text-muted">No connectors are configured for this tenant.</p>}</CardBody></Card>
      <Card><CardHeader title="Available connector catalog" description={`${catalog.length} provider definitions available in the current application build.`} icon={<Activity />} /><CardBody className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{catalog.map(item => <div key={item.id} className="rounded-lg border border-border p-3"><p className="text-sm font-semibold">{item.name ?? item.id}</p><p className="mt-1 text-xs text-muted">{item.description ?? item.provider ?? "Provider configuration available"}</p></div>)}</CardBody></Card>
      {overview.jobs.length > 0 && <Card><CardHeader title="Recent integration jobs" description="Ambiguous or failed outcomes are visible so they are not blindly replayed." icon={<Clock3 />} /><CardBody className="grid gap-2">{overview.jobs.slice(0, 10).map(job => <div key={job.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-3"><div><p className="text-sm font-medium">{job.operation.replaceAll("_", " ")}</p><p className="text-xs text-muted">{new Date(job.created_at).toLocaleString()}</p>{job.last_error && <p className="mt-1 text-xs text-danger-text">{job.last_error}</p>}</div><Badge tone={job.status === "completed" ? "success" : ["uncertain", "dead_letter", "pending_approval"].includes(job.status) ? "warning" : "neutral"}>{job.status.replaceAll("_", " ")}</Badge></div>)}</CardBody></Card>}</>}
  </div>;
}

type InfrastructureSnapshot = {
  status: string; observed_at: string; scope: string;
  release: { version: string; revision: string; environment: string };
  resources: { status?: string; cpu_percent?: number; logical_cpus?: number; memory_percent?: number; process_rss_bytes?: number; process_uptime_seconds?: number; disk?: { status: string; percent?: number; free_bytes?: number }; gpu?: { status: string; devices?: unknown[] } };
  database: { status: string; latency_ms?: number; server_version?: string; role_superuser?: boolean; workers?: { online: number; stale: number; active_jobs: number }; vectors?: { version_mismatches: number; expired_leases: number }; queues?: Record<string, Record<string, number>>; migration_heads?: string[] };
  alerts: { code: string; severity: string; message: string }[];
  services: Record<string, string>; security: Record<string, boolean>; recovery: { status: string; message: string };
  background_tasks: { name: string; status: string }[];
};

function bytes(value?: number) { if (!Number.isFinite(value)) return "—"; const amount = value as number; const unit = amount >= 1024 ** 3 ? "GB" : "MB"; return `${(amount / (unit === "GB" ? 1024 ** 3 : 1024 ** 2)).toFixed(1)} ${unit}`; }

export function InfrastructureWorkspace() {
  const [snapshot, setSnapshot] = useState<InfrastructureSnapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const reload = useCallback(async () => { setBusy(true); try { setSnapshot(await request<InfrastructureSnapshot>("GET", "/api/admin/infrastructure")); setError(""); } catch (err) { setError(describeError(err)); } finally { setBusy(false); } }, []);
  useEffect(() => { void reload(); }, [reload, revision]);
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M11" eyebrow="MODULE 11 · PLATFORM OPERATIONS" title="Infrastructure & deployment" description="Read-only operational telemetry from this installation. Recovery status reflects verified drills, not the presence of backups." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)} disabled={busy}><RefreshCw className={busy ? "animate-spin" : ""} /> Refresh</Button>} />
    {error && <ErrorAlert error={error} />}
    {snapshot && <><div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-surface p-4"><Badge tone={snapshot.status === "observed" ? "success" : "warning"} dot>{snapshot.status}</Badge><span className="text-sm font-semibold">{snapshot.release.version}</span><span className="text-xs text-muted">{snapshot.release.environment} · {snapshot.scope} · observed {new Date(snapshot.observed_at).toLocaleString()}</span></div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="CPU" value={snapshot.resources.cpu_percent == null ? "—" : `${snapshot.resources.cpu_percent.toFixed(1)}%`} hint={`${snapshot.resources.logical_cpus ?? "—"} logical CPUs · OS visible`} icon={<Activity />} /><Stat label="Memory" value={snapshot.resources.memory_percent == null ? "—" : `${snapshot.resources.memory_percent.toFixed(1)}%`} hint={`API process ${bytes(snapshot.resources.process_rss_bytes)}`} icon={<Gauge />} /><Stat label="Disk" value={snapshot.resources.disk?.percent == null ? "—" : `${snapshot.resources.disk.percent.toFixed(1)}%`} hint={`${bytes(snapshot.resources.disk?.free_bytes)} free`} icon={<Database />} /><Stat label="Database" value={snapshot.database.status} hint={snapshot.database.latency_ms == null ? "Probe unavailable" : `${snapshot.database.latency_ms} ms · PostgreSQL ${snapshot.database.server_version ?? ""}`} icon={<Activity />} /></div>
      <div className="grid gap-4 xl:grid-cols-2"><Card><CardHeader title="Readiness alerts" description="Measurements are bounded, read-only probes." icon={<CircleAlert />} /><CardBody className="grid gap-2">{snapshot.alerts.length ? snapshot.alerts.map(item => <Alert key={item.code} tone={item.severity === "critical" ? "danger" : "warning"} title={item.code.replaceAll("_", " ")}>{item.message}</Alert>) : <Alert tone="success" title="No active telemetry alerts">No monitored threshold was reached on this snapshot.</Alert>}</CardBody></Card><Card><CardHeader title="Runtime services" description="Current process information only; provider availability is not probed." icon={<Layers3 />} /><CardBody className="grid gap-2">{Object.entries(snapshot.services).map(([name, status]) => <div key={name} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="capitalize">{name.replaceAll("_", " ")}</span><Badge tone={status === "responding" || status === "owner" || status === "artifact_present" ? "success" : "neutral"}>{status.replaceAll("_", " ")}</Badge></div>)}</CardBody></Card></div>
      <Card><CardHeader title="Recovery verification" description="Production recovery readiness cannot be inferred without a successful restore drill." icon={<ShieldCheck />} /><CardBody><Alert tone="warning" title={snapshot.recovery.status}>{snapshot.recovery.message}</Alert></CardBody></Card>
    </>}
    {!snapshot && busy && <Skeleton className="h-64 w-full" />}
  </div>;
}

type QualityOverview = {
  report: { decision: string; snapshot_sha256: string; blockers: string[]; failed_suites: string[]; counts: Record<string, number>; evaluated_at: string };
  suites: { id: string; name: string; kind: string; module: number; timeout: number; verifies: string[] }[];
  runs: { id: string; suite_id: string; status: string; created_at: string; results?: { test_id: string; status: string; classification?: string; message?: string }[]; metrics?: Record<string, number> }[];
  defects: { id: string; test_id: string; module: number; severity: string; status: string; occurrences: number }[];
  scope: string;
};

export function QualityWorkspace() {
  const [data, setData] = useState<QualityOverview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);
  const reload = useCallback(async () => { try { setData(await request<QualityOverview>("GET", "/api/admin/quality")); setError(""); } catch (err) { setError(describeError(err)); } }, []);
  useEffect(() => { void reload(); }, [reload, revision]);
  async function queue(suiteId: string) { setBusy(suiteId); setError(""); try { await request("POST", "/api/admin/quality/runs", { suite_id: suiteId }); setRevision(value => value + 1); } catch (err) { setError(describeError(err)); } finally { setBusy(""); } }
  async function cancel(runId: string) { setBusy(runId); setError(""); try { await request("POST", `/api/admin/quality/runs/${encodeURIComponent(runId)}/cancel`, {}); setRevision(value => value + 1); } catch (err) { setError(describeError(err)); } finally { setBusy(""); } }
  const current = data?.runs ?? [];
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M12" eyebrow="MODULE 12 · QUALITY EVIDENCE" title="QA & benchmarking" description="Review source-bound test evidence and release blockers. Queueing is limited to reviewed repository suites; QA workers must use a dedicated disposable test database." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)} disabled={Boolean(busy)}><RefreshCw /> Refresh evidence</Button>} />
    {error && <ErrorAlert error={error} />}
    {data && <><div className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface p-4"><Badge tone={data.report.decision === "GO" ? "success" : data.report.decision === "NO-GO" ? "danger" : "warning"} dot>{data.report.decision}</Badge><span className="text-sm font-semibold">Release evidence</span><span className="max-w-full truncate font-mono text-[10px] text-muted" title={data.report.snapshot_sha256}>snapshot {data.report.snapshot_sha256.slice(0, 16)}…</span><span className="ml-auto text-xs text-muted">{new Date(data.report.evaluated_at).toLocaleString()}</span></div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="Mandatory blockers" value={data.report.blockers.length} hint="Requirements without current passing evidence" icon={<CircleAlert />} /><Stat label="Verified requirements" value={data.report.counts.Verified ?? 0} hint="Evidence bound to this source snapshot" icon={<Check />} /><Stat label="Reviewed test suites" value={data.suites.length} hint="Allowlisted in the QA catalog" icon={<Layers3 />} /><Stat label="Open defects" value={data.defects.filter(defect => defect.status === "open").length} hint="Scoped to this administrator and tenant" icon={<Activity />} /></div>
      <Alert tone="warning" title="Benchmark evidence is bounded">A green test run does not prove answer quality, live provider availability, or production readiness. Each report is tied to its source digest and expiry policy.</Alert>
      <Card><CardHeader title="Reviewed QA suites" description="Start one of these suites only after verifying the QA runner's dedicated test database configuration." icon={<Check />} /><CardBody className="grid gap-2">{data.suites.map(suite => <div key={suite.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><p className="font-semibold">{suite.name}</p><Badge>Module {String(suite.module).padStart(2, "0")}</Badge><Badge tone={suite.kind === "browser" ? "warning" : "neutral"}>{suite.kind}</Badge></div><p className="mt-1 text-xs text-muted">Timeout {suite.timeout}s · validates {suite.verifies.length || "no release checklist rows"} requirements</p></div><Button variant="secondary" onClick={() => void queue(suite.id)} loading={busy === suite.id} disabled={Boolean(busy)}>Queue suite</Button></div>)}</CardBody></Card>
      <Card><CardHeader title="Run history" description="Status and timestamps come from persisted QA evidence." icon={<Clock3 />} /><CardBody className="grid gap-2">{current.length ? current.slice(0, 20).map(run => <div key={run.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div><p className="text-sm font-semibold">{run.suite_id}</p><p className="text-xs text-muted">{new Date(run.created_at).toLocaleString()}</p>{run.results?.some(result => result.status === "failed") && <p className="mt-1 text-xs text-danger-text">{run.results.filter(result => result.status === "failed").length} failed result(s)</p>}</div><div className="flex items-center gap-2"><Badge tone={run.status === "passed" ? "success" : ["failed", "blocked"].includes(run.status) ? "danger" : "warning"}>{run.status}</Badge>{run.status === "queued" && <Button size="sm" variant="secondary" onClick={() => void cancel(run.id)} loading={busy === run.id} disabled={Boolean(busy)}>Cancel</Button>}</div></div>) : <p className="text-sm text-muted">No QA runs have been recorded in this scope.</p>}</CardBody></Card>
      {data.report.blockers.length > 0 && <Card><CardHeader title="Release blockers" description="Requirements that still need current passing evidence." icon={<CircleAlert />} /><CardBody className="flex flex-wrap gap-2">{data.report.blockers.slice(0, 40).map(item => <Badge key={item} tone="warning">{item}</Badge>)}</CardBody></Card>}</>}
  </div>;
}

export function GovernanceWorkspace({ onNavigate }: { onNavigate: Navigate }) {
  const [counts, setCounts] = useState<{ label: string; count: number }[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    Promise.all([
      request<{ items: unknown[] }>("GET", "/api/governance/claims"),
      request<{ items: unknown[] }>("GET", "/api/governance/knowledge"),
      request<{ items: unknown[] }>("GET", "/api/governance/knowledge/stale"),
      request<{ items: unknown[] }>("GET", "/api/governance/knowledge/conflicts"),
      request<{ items: unknown[] }>("GET", "/api/governance/policy-candidates"),
    ]).then(results => { if (live) setCounts(results.map((row, index) => ({ label: ["Claims awaiting review", "Knowledge drafts", "Stale knowledge", "Knowledge conflicts", "Policy candidates"][index], count: row.items.length }))); })
      .catch(err => { if (live) setError(describeError(err)); });
    return () => { live = false; };
  }, []);
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M06" eyebrow="MODULE 06 · TRUST & CONTROL" title="Governance & security" description="Inspect role-scoped knowledge review, claims, policy decisions and access controls from existing authorized workspaces." />
    {error && <ErrorAlert error={error} />}
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{counts.map(item => <Stat key={item.label} label={item.label} value={item.count} hint="Visible to your current review permissions" icon={<ShieldCheck />} />)}</div>
    <Card><CardHeader title="Governance workspaces" description="Open the established application workflow for each decision type." icon={<ShieldCheck />} /><CardBody className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{[
      { label: "Knowledge claims", description: "Review submitted knowledge contributions and evidence.", view: "claims" },
      { label: "Approval queue", description: "Inspect and decide approval-gated business actions.", view: "approvals" },
      { label: "Knowledge quality", description: "Review traces, refusals and citation evidence.", view: "traces" },
      { label: "People & access", description: "Manage people, roles and scoped permissions.", view: "people" },
      { label: "Account security", description: "Inspect sign-in and multi-factor security settings.", view: "security" },
      { label: "Audit history", description: "Review immutable account and system activity records.", view: "logs" },
    ].map(item => <button key={item.view} className="module-launch-card" onClick={() => onNavigate(item.view)}><span><strong>{item.label}</strong><span>{item.description}</span></span><ArrowRight className="size-4 shrink-0" /></button>)}</CardBody></Card>
    <Alert title="Authorization stays in force">Each workspace continues to enforce its own owner, department, reviewer and administrator permissions. This page does not grant access.</Alert>
  </div>;
}

export function FrontendUxWorkspace({ onNavigate }: { onNavigate: Navigate }) {
  const modules = useMemo(() => [
    { label: "Knowledge & AI Brain", detail: "Documents, citations and conversation workbench", view: "knowledge" },
    { label: "AI Agent Studio", detail: "Agent tools, approvals and run histories", view: "agent-studio" },
    { label: "Company graph", detail: "Entity and relationship map", view: "company-graph" },
    { label: "Digital twin", detail: "Live model of teams, money flow, and what breaks", view: "digital-twin" },
    { label: "Operations and workflows", detail: "Process discovery and workflow runs", view: "process-intelligence" },
    { label: "Quality evidence", detail: "Reviewed tests and release blockers", view: "quality" },
    { label: "Platform health", detail: "Read-only infrastructure telemetry", view: "infrastructure" },
  ], []);
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M09" eyebrow="MODULE 09 · PRODUCT EXPERIENCE" title="Frontend & UX workspace" description="A direct index into InfraSpine's daily work surfaces, with the shared pine and sage visual system." />
    <section className="module-ux-hero"><div><Badge tone="success" dot>12 modules · one control desk</Badge><h2>Move from overview to action in one step</h2><p>Choose a work area below or send a scoped instruction to its module specialist. Answers and approvals return to the Master Control Center.</p></div><div className="module-ux-hero-mark"><Boxes aria-hidden /><span>IRFANIX</span></div></section>
    <Card><CardHeader title="Open an application workspace" description="These links open live product views backed by the current app APIs." icon={<Sparkles />} /><CardBody className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{modules.map(item => <button key={item.view} className="module-launch-card" onClick={() => onNavigate(item.view)}><span><strong>{item.label}</strong><span>{item.detail}</span></span><ArrowRight className="size-4 shrink-0" /></button>)}</CardBody></Card>
    <Card><CardHeader title="Interface conventions" description="The design tokens are shared across the app and respect a configured company brand color." icon={<Activity />} /><CardBody className="grid gap-3 sm:grid-cols-3"><div className="module-design-note"><span className="module-swatch module-swatch-green" /><strong>Evergreen actions</strong><p>Primary actions, active navigation and focus states.</p></div><div className="module-design-note"><span className="module-swatch module-swatch-sage" /><strong>Sage surfaces</strong><p>Quiet backgrounds and clear layers keep dense screens readable.</p></div><div className="module-design-note"><span className="module-swatch module-swatch-amber" /><strong>Review states</strong><p>Amber and red remain reserved for pending decisions and errors.</p></div></CardBody></Card>
  </div>;
}
