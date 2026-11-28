import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { RunsPanel } from "../workflows/RunsPanel";
import { FlowDiagram } from "../workflows/FlowDiagram";
import { Activity, ArrowRight, Boxes, Check, CircleAlert, Clock3, Database, Gauge, Layers3, Network, RefreshCw, ShieldCheck, Sparkles, Workflow } from "lucide-react";
import { ApiError, describeError, request } from "../api";
import { bankLabel, needsAttentionCount, pendingApprovals } from "../integrations/connectorsView.mjs";
import { graphApi } from "../company-graph/api";
import { DigitalTwinWorkspace } from "../digital-twin/TwinWorkspace";
import { GraphMap } from "../company-graph/GraphMap";
import type { Graph, GraphRecord, Quality, Tenant } from "../company-graph/types";
import { twinApi } from "../digital-twin/api";
import type { TwinFinding, TwinHealth, TwinHealthAlert, TwinProposal, TwinTrust, TwinVersion } from "../digital-twin/types";
import { findingTone, gaugeSegments, inboxRows, proposalStatusTone, queueSummary, shortFingerprint, timelineRows } from "../digital-twin/twinDashboard.mjs";
import { workflowApi } from "../workflows/api";
import { templateDraftState, templateSummary } from "../workflows/templatePicker";
import type { Dashboard as WorkflowDashboard, Scope, Spec, Template, Workflow as WorkflowRow } from "../workflows/types";
import { bc } from "../continuity/api";
import { WhatIfPanel } from "../whatif/WhatIfPanel";
import type { AjDashboardSummary, Dashboard as ContinuityDashboard, DrillComparison, DrillSeedResult, GraphMergeStatus, HandoverCheck, Workspace } from "../continuity/types";
import { AJ_REFRESH_MS, healthTone, overallStatusLabel, overallStatusTone, progressLabel, severityTone, timeAgo } from "../continuity/ajDashboard.mjs";
import { summarizeDrillComparison } from "../continuity/drillSummary.mjs";
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
    case "digital-twin": return <TwinWorkspace />;
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
        <label className="grid min-w-60 gap-1 text-[13px] font-semibold text-foreground-2">Company graph workspace<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={tenantId} onChange={event => setTenantId(event.target.value)}>{tenants.map(item => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}</select></label>
        <Badge tone="success" dot>Tenant scope enforced</Badge>
      </section>
      {quality && <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="Entities" value={quality.entity_count} hint="Visible in this graph scope" icon={<Network />} /><Stat label="Relationships" value={quality.relationship_count} hint="Current graph snapshot" icon={<Activity />} /><Stat label="Ownership gaps" value={quality.ownership_gaps.length} hint="Require a responsible owner" icon={<CircleAlert />} /><Stat label="Missing backups" value={quality.missing_backups.length} hint="No authorized backup found" icon={<ShieldCheck />} /></div>}
      {graph && <Card><CardHeader title="Live company map" description={`${graph.entities.length} visible entities · ${graph.relationships.length} visible relationships · snapshot ${new Date(graph.as_of).toLocaleString()}`} icon={<Network />} /><div className="cg-workspace-map">{graph.entities.length ? <GraphMap graph={graph} selected={selected?.id} onSelect={record => setSelected(record)} onExpand={id => void expand(id)} /> : <EmptyState title="The graph is empty" description="Add approved source records to make company relationships visible here." />}</div></Card>}
      {graph && <WhatIfPanel tenantId={tenantId} graph={graph} />}
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
  const [draft, setDraft] = useState<{ spec: Spec; errors: { code: string; message: string; node_id: string | null }[]; requires_review: boolean; workflowId?: string; baseVersion?: number } | null>(null);
  const [draftName, setDraftName] = useState("New workflow draft");
  const [selectedNode, setSelectedNode] = useState("");
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
  async function validateDraft() {
    if (!scopeId || !draft) return;
    setBusy(true); setError("");
    try { const result = await workflowApi.validate(scopeId, draft.spec); setDraft({ ...draft, errors: result.errors }); }
    catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }
  async function useTemplate(key: string) {
    if (!scopeId) return;
    setBusy(true); setError("");
    try {
      const result = await workflowApi.templateDraft(scopeId, key);
      const created = templateDraftState(result);
      setDraft({ spec: created.spec, errors: created.errors, requires_review: created.requiresReview, workflowId: created.workflowId, baseVersion: created.baseVersion });
      setDraftName(result.workflow.name);
      setSelectedNode("");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }
  async function saveDraft() {
    if (!scopeId || !draft) return;
    setBusy(true); setError("");
    try { await workflowApi.save(scopeId, draftName.trim() || "Workflow draft", draft.workflowId ? "Template draft. Review the definition and approvals before publishing." : "AI-assisted draft. Review the definition and approvals before publishing.", draft.spec, draft.workflowId, draft.baseVersion); setDraft(null); setRequirements(""); setRevision(value => value + 1); }
    catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M05" eyebrow="MODULE 05 · OPERATIONS" title="Universal workflow engine" description="Draft, validate and review tenant-scoped workflows. Execution requires a reviewed published version and an enabled worker." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)}><RefreshCw /> Refresh</Button>} />
    {error && <ErrorAlert error={error} />}
    {scopes.length > 0 && <label className="grid max-w-lg gap-1 text-[13px] font-semibold text-foreground-2">Workflow scope<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={scopeId} onChange={event => setScopeId(event.target.value)}>{scopes.map(item => <option key={item.id} value={item.id}>{item.name}{item.can_manage ? " · manager" : ""}</option>)}</select></label>}
    {!scopes.length && !error && <EmptyModule title="No authorized workflow scope" description="Workflow lists are limited to the signed-in user's company scopes. Ask an administrator to grant a workflow role." />}
    {scopeId && <>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="Total runs" value={dashboard?.total_runs ?? "—"} hint="Persisted workflow history" icon={<Activity />} /><Stat label="Completion rate" value={dashboard?.completion_rate == null ? "—" : `${Math.round(dashboard.completion_rate * 100)}%`} hint="Recorded successful runs" icon={<Check />} /><Stat label="Waiting for review" value={dashboard?.waiting_count ?? "—"} hint="Human task or approval queue" icon={<Clock3 />} /><Stat label="Overdue steps" value={dashboard?.overdue_steps ?? "—"} hint="Requires authorized review" icon={<CircleAlert />} /></div>
      <Card><CardHeader title="Draft a workflow from instructions" description="This creates a draft for review. It never publishes or executes the generated workflow." icon={<Sparkles />} /><CardBody><form className="grid gap-3" onSubmit={event => void createDraft(event)}><Textarea value={requirements} onChange={event => setRequirements(event.target.value)} rows={3} maxLength={8000} minLength={10} placeholder="Describe the process, people involved, decision points and required evidence…" /><div className="flex flex-wrap items-center justify-between gap-3"><span className="text-xs text-muted">Available templates: {templates.length} · adapters: review permissions in scope</span><Button type="submit" variant="primary" loading={busy} disabled={requirements.trim().length < 10}>Create reviewed draft</Button></div></form>
        {draft && <div className="mt-4 rounded-xl border border-primary/20 bg-primary-soft/30 p-4"><div className="flex flex-wrap items-end justify-between gap-3"><div className="grid min-w-60 gap-2"><Badge tone="primary">Draft · review required</Badge><label className="grid gap-1 text-[13px] font-medium">Draft name<Input value={draftName} onChange={event => setDraftName(event.target.value)} maxLength={160} /></label><p className="text-sm font-semibold">{draft.spec.nodes.length} steps · {draft.errors.length} validation findings</p></div><Button variant="primary" onClick={() => void saveDraft()} loading={busy} disabled={!draft.spec.nodes.length || !draftName.trim()}>Save draft to workflow list</Button></div><div className="mt-3 grid gap-2">{draft.errors.map((item, index) => <Alert key={`${item.code}-${index}`} tone={item.code === "error" ? "danger" : "warning"}>{item.message}</Alert>)}</div>
        <div className="mt-4"><p className="mb-2 text-xs font-medium">Workflow diagram — drag steps from the palette onto the canvas, drag nodes to move them, use a node's port to connect it. Validate the edited diagram before saving.</p><FlowDiagram spec={draft.spec} onChange={next => setDraft({ ...draft, spec: next })} selected={selectedNode} onSelect={setSelectedNode} errors={draft.errors} /><div className="mt-2 flex flex-wrap gap-2"><Button variant="secondary" onClick={() => void validateDraft()} loading={busy}>Validate diagram</Button></div></div>
        <details className="mt-3"><summary className="cursor-pointer text-[13px] font-medium">Review generated workflow definition</summary><pre className="mt-2 max-h-72 overflow-auto rounded-lg bg-surface p-3 text-xs">{JSON.stringify(draft.spec, null, 2)}</pre></details></div>}
      </CardBody></Card>
      <Card><CardHeader title="Template library" description="One click creates a draft from a template and opens it in the canvas below. Drafts are never published or executed — review stays a separate, owner-gated step." icon={<Workflow />} /><CardBody className="grid gap-2">{templates.length ? templates.map(item => { const summary = templateSummary(item); return <div key={item.key} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><p className="font-semibold">{summary.name}</p><Badge tone="neutral">{summary.category}</Badge><Badge>{summary.stepCount} steps</Badge></div><p className="mt-1 text-sm text-muted">{summary.description}</p>{summary.requiredInputs.length > 0 && <p className="mt-1 text-xs text-muted">Needs: {summary.requiredInputs.join(" · ")}</p>}</div><Button variant="secondary" onClick={() => void useTemplate(item.key)} loading={busy} disabled={!scopeId}>Use template</Button></div>; }) : <p className="text-sm text-muted">No templates are available in this scope yet.</p>}</CardBody></Card>
      <Card><CardHeader title="Workflow definitions" description="Review draft and published versions before enabling event or scheduled execution." icon={<Workflow />} /><CardBody className="grid gap-2">{workflows.length ? workflows.map(item => <div key={item.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="font-semibold">{item.name}</p><p className="text-xs text-muted">Version {item.latest_version} · updated {new Date(item.updated_at).toLocaleString()}</p></div><div className="flex items-center gap-2"><Badge tone={item.enabled ? "success" : "neutral"}>{item.enabled ? "Enabled" : item.latest_status ?? "Draft"}</Badge><Badge>{item.latest_status ?? "unknown"}</Badge></div></div>) : <p className="text-sm text-muted">No workflow definitions are visible in this scope yet.</p>}</CardBody></Card>
      <RunsPanel scopeId={scopeId} revision={revision} />
      {Object.keys(counts).length > 0 && <Card><CardHeader title="Run queue" description={`Current workflow snapshot: ${dashboard?.as_of ? new Date(dashboard.as_of).toLocaleString() : "loading"}.`} icon={<Layers3 />} /><CardBody className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{Object.entries(counts).map(([status, count]) => <div key={status} className="rounded-lg border border-border p-3"><p className="text-xs capitalize text-muted">{status.replaceAll("_", " ")}</p><p className="mt-1 text-xl font-semibold">{count}</p></div>)}</CardBody></Card>}
    </>}
  </div>;
}

/* ------------------------------------------------- Step AJ dashboard --- */
function AjDashboardSection({ summary }: { summary: AjDashboardSummary }) {
  const health = summary.health && typeof summary.health === "object" ? summary.health : null;
  const healthStatus = typeof health?.status === "string" ? health.status : null;
  return <div className="grid gap-4">
    <Alert tone={overallStatusTone(summary.overall_status)} title={`Continuity status: ${overallStatusLabel(summary.overall_status)}`}>
      {summary.highlights.length ? <ul className="list-disc pl-4">{summary.highlights.map(item => <li key={item}>{item}</li>)}</ul> : <p>No open risks, incidents, approvals or recoveries.</p>}
      <p className="mt-1 text-xs opacity-80">Updated {timeAgo(summary.generated_at)} · refreshes every 30 seconds</p>
    </Alert>
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Stat label="Open risks" value={summary.risks.total} hint={`${summary.risks.uncovered_critical} critical without a plan`} icon={<CircleAlert />} />
      <Stat label="Active incidents" value={summary.incidents.total_active} hint={`${summary.incidents.overdue} overdue their SLA`} icon={<Clock3 />} />
      <Stat label="Approvals pending" value={summary.approvals.pending} hint={`${summary.approvals.expiring_soon} expiring within 24 hours`} icon={<Check />} />
      <Stat label="Recovery progress" value={summary.recovery.average_progress_percent == null ? "—" : `${summary.recovery.average_progress_percent}%`} hint={`${summary.recovery.active} active · ${summary.recovery.at_risk} past RTO`} icon={<Activity />} />
    </div>
    <div className="grid gap-4 xl:grid-cols-2">
      <Card><CardHeader title="Risks needing attention" description="Worst first, up to 5. Detail is in the resources and plans below." icon={<CircleAlert />} /><CardBody className="grid gap-2">{summary.risks.top.length ? summary.risks.top.map(risk => <div key={risk.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{risk.title}</p><p className="text-xs text-muted">{risk.category} · {risk.source}</p></div><span className="flex shrink-0 items-center gap-1"><Badge tone={severityTone(risk.severity)}>{risk.severity}</Badge>{!risk.has_plan && <Badge tone="danger">No plan</Badge>}</span></div>) : <p className="text-sm text-muted">No open risks.</p>}</CardBody></Card>
      <Card><CardHeader title="Active incidents" description="Overdue incidents first, up to 5." icon={<Clock3 />} actions={<Button size="sm" variant="secondary" onClick={() => { window.location.hash = "/recovery"; }}>Open recovery</Button>} /><CardBody className="grid gap-2">{summary.incidents.top.length ? summary.incidents.top.map(incident => <div key={incident.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{incident.title}</p><p className="text-xs text-muted">{incident.status}</p></div><span className="flex shrink-0 items-center gap-1"><Badge tone={severityTone(incident.severity)}>{incident.severity}</Badge>{incident.overdue && <Badge tone="warning">Overdue</Badge>}</span></div>) : <p className="text-sm text-muted">No active incidents.</p>}</CardBody></Card>
    </div>
    <div className="grid gap-4 xl:grid-cols-2">
      <Card><CardHeader title="Recovery in progress" description="Verified steps and RTO risk per recovery." icon={<Activity />} /><CardBody className="grid gap-2">{summary.recovery.items.length ? summary.recovery.items.map(item => <div key={item.id} className="grid gap-1 rounded-lg border border-border p-3"><div className="flex items-center justify-between gap-3"><p className="min-w-0 truncate text-sm font-semibold">{item.title}</p><span className="flex shrink-0 items-center gap-1"><Badge>{item.status}</Badge>{item.at_risk && <Badge tone="danger">Past RTO</Badge>}</span></div><div className="h-2 overflow-hidden rounded-full bg-surface-2"><div className="h-2 rounded-full bg-primary" style={{ width: `${item.progress_percent ?? 0}%` }} /></div><p className="text-xs text-muted">{progressLabel(item.progress_percent)} · {item.verified_steps} of {item.total_steps} steps verified</p></div>) : <p className="text-sm text-muted">No recoveries in progress.</p>}</CardBody></Card>
      <Card><CardHeader title="Approvals" description="Pending decisions in this scope." icon={<Check />} actions={<Button size="sm" variant="secondary" onClick={() => { window.location.hash = "/approvals"; }}>Open approvals</Button>} /><CardBody className="grid gap-2"><div className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="text-sm">Pending</span><span className="text-sm font-semibold">{summary.approvals.pending}</span></div><div className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="text-sm">Expiring within 24 hours</span><span className="text-sm font-semibold">{summary.approvals.expiring_soon}</span></div><div className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="text-sm">Requested by you</span><span className="text-sm font-semibold">{summary.approvals.own_requests}</span></div>{summary.approvals.pending === 0 && <p className="text-sm text-muted">No approvals pending.</p>}</CardBody></Card>
    </div>
    {healthStatus
      ? <Alert tone={healthTone(healthStatus)} title={`System health: ${healthStatus}`}>The Step G health roll-up for this scope.</Alert>
      : <p className="text-xs text-muted">Health roll-up not available yet — it arrives with the Step G merge.</p>}
  </div>;
}

export function ContinuityWorkspace() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [dashboard, setDashboard] = useState<ContinuityDashboard | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [aj, setAj] = useState<AjDashboardSummary | null>(null);
  const [ajNote, setAjNote] = useState("");
  const ajUnavailableRef = useRef(false);
  const [checkScenarioId, setCheckScenarioId] = useState("");
  const [check, setCheck] = useState<HandoverCheck | null>(null);
  const [checkBusy, setCheckBusy] = useState(false);
  const [drillSeed, setDrillSeed] = useState<DrillSeedResult | null>(null);
  const [drillBusy, setDrillBusy] = useState(false);
  const [comparison, setComparison] = useState<DrillComparison | null>(null);
  const [comparisonBusy, setComparisonBusy] = useState("");
  const [cgTenantId, setCgTenantId] = useState("");
  const [mergeStatus, setMergeStatus] = useState<GraphMergeStatus | null>(null);
  const [mergeMessage, setMergeMessage] = useState("");
  const [mergeBusy, setMergeBusy] = useState(false);
  useEffect(() => { let live = true; bc.workspaces().then(result => { if (live) { setWorkspaces(result.items); setWorkspaceId(current => current || result.items[0]?.id || ""); } }).catch(err => { if (live) setError(describeError(err)); }).finally(() => { if (live) setLoading(false); }); return () => { live = false; }; }, []);
  useEffect(() => { if (!workspaceId) return; let live = true; setLoading(true); bc.dashboard(workspaceId).then(result => { if (live) setDashboard(result); }).catch(err => { if (live) setError(describeError(err)); }).finally(() => { if (live) setLoading(false); }); return () => { live = false; }; }, [workspaceId, revision]);
  // Step AJ: rolled-up dashboard summary for the selected scope (scope id == tenant id).
  // Polls every 30 s (AJ_REFRESH_MS). A 403/404 means the viewer's role or scope may
  // not open the dashboard — stop polling and say so; the Step G dashboard below
  // still works. Other failures show once and keep polling silently.
  useEffect(() => {
    if (!workspaceId) return;
    let live = true;
    ajUnavailableRef.current = false;
    setAj(null);
    setAjNote("");
    const controller = new AbortController();
    const load = async (silent: boolean) => {
      if (ajUnavailableRef.current) return;
      try {
        const result = await bc.ajDashboard(workspaceId, controller.signal);
        if (live) setAj(result);
      } catch (err) {
        if (!live) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof ApiError && (err.status === 403 || err.status === 404)) {
          ajUnavailableRef.current = true;
          setAjNote(err.status === 403 ? "The continuity dashboard is available to owner, approver and planner roles." : "No continuity dashboard is available for this workspace.");
        } else if (!silent) {
          setAjNote(describeError(err));
        }
      }
    };
    void load(false);
    const timer = setInterval(() => { void load(true); }, AJ_REFRESH_MS);
    return () => { live = false; controller.abort(); clearInterval(timer); };
  }, [workspaceId, revision]);
  const metrics = dashboard?.metrics;
  const drillScenarios = (dashboard?.scenarios ?? []).filter(item => item.source_key?.startsWith("drill:"));
  const drillRuns = (dashboard?.runs ?? []).filter(item => item.mode === "drill");
  async function runHandoverCheck() { if (!workspaceId || !checkScenarioId) return; setCheckBusy(true); setError(""); try { setCheck(await bc.handoverCheck(workspaceId, checkScenarioId)); } catch (err) { setError(describeError(err)); } finally { setCheckBusy(false); } }
  async function seedDrills() { if (!workspaceId) return; setDrillBusy(true); setError(""); try { setDrillSeed(await bc.seedDrills(workspaceId)); setRevision(value => value + 1); } catch (err) { setError(describeError(err)); } finally { setDrillBusy(false); } }
  async function loadComparison(runId: string) { if (!workspaceId) return; setComparisonBusy(runId); setError(""); try { setComparison(await bc.drillComparison(workspaceId, runId)); } catch (err) { setError(describeError(err)); } finally { setComparisonBusy(""); } }
  async function seedMerge() { if (!workspaceId || !cgTenantId.trim()) return; setMergeBusy(true); setError(""); try { const result = await bc.seedGraphMerge(workspaceId, cgTenantId.trim()); setMergeMessage(`${Number(result.links_created ?? 0)} graph link(s) created · ${Number((result.proposal_ids as unknown[])?.length ?? 0)} proposal(s) awaiting approval`); setMergeStatus(null); } catch (err) { setError(describeError(err)); } finally { setMergeBusy(false); } }
  async function loadMergeStatus() { if (!workspaceId || !cgTenantId.trim()) return; setMergeBusy(true); setError(""); try { setMergeStatus(await bc.graphMergeStatus(workspaceId, cgTenantId.trim())); setMergeMessage(""); } catch (err) { setError(describeError(err)); } finally { setMergeBusy(false); } }
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M08" eyebrow="MODULE 08 · RESILIENCE" title="Business continuity" description="Review business impact, critical resources, continuity plans, approvals and verified drills." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)} disabled={loading}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>} />
    {error && <ErrorAlert error={error} />}
    {workspaces.length > 0 && <label className="grid max-w-lg gap-1 text-[13px] font-semibold text-foreground-2">Continuity workspace<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={workspaceId} onChange={event => setWorkspaceId(event.target.value)}>{workspaces.map(item => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}</select></label>}
    {!workspaces.length && !loading && !error && <EmptyModule title="No continuity workspace is available" description="Continuity data is tenant-scoped. A workspace must be created and access granted before resources or plans can be shown." />}
    {workspaceId && aj && <AjDashboardSection summary={aj} />}
    {workspaceId && !aj && !ajNote && <Skeleton className="h-40 w-full" />}
    {ajNote && <Alert tone="info" title="Continuity dashboard unavailable">{ajNote}</Alert>}
    {metrics && <><div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="Critical resources" value={metrics.critical_resources} hint="Recorded in this workspace" icon={<Database />} /><Stat label="BIA assessed" value={metrics.bia_assessed} hint="Resources with impact assessment" icon={<Gauge />} /><Stat label="Plan coverage" value={metrics.plan_coverage_percent == null ? "—" : `${metrics.plan_coverage_percent}%`} hint={`${metrics.plan_covered} resources covered`} icon={<ShieldCheck />} /><Stat label="Verified drills" value={metrics.verified_drills} hint={`${metrics.active_recoveries} active recoveries`} icon={<Check />} /></div>
      {metrics.pending_approvals > 0 && <Alert tone="warning" title={`${metrics.pending_approvals} decision(s) need review`}>The current continuity scope has pending approval work.</Alert>}
      <div className="grid gap-4 xl:grid-cols-2"><Card><CardHeader title="Resources and dependencies" description={`${dashboard.nodes.length} resources · ${dashboard.links.length} dependency links`} icon={<Network />} /><CardBody className="grid gap-2">{dashboard.nodes.slice(0, 8).map(node => <div key={node.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="min-w-0 truncate text-sm">{node.name}</span><Badge tone={node.data.critical ? "warning" : "neutral"}>{node.data.critical ? "Critical" : node.kind}</Badge></div>)}{!dashboard.nodes.length && <p className="text-sm text-muted">No resources are recorded in this scope.</p>}</CardBody></Card><Card><CardHeader title="Continuity plans" description="Draft, approval and active status comes from saved plans." icon={<ShieldCheck />} /><CardBody className="grid gap-2">{dashboard.plans.length ? dashboard.plans.slice(0, 8).map(plan => <div key={plan.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{plan.title}</p><p className="text-xs text-muted">Version {plan.version} · review due {new Date(plan.review_due_at).toLocaleDateString()}</p></div><Badge>{plan.status}</Badge></div>) : <p className="text-sm text-muted">No continuity plans are available in this scope.</p>}</CardBody></Card></div>
      <Alert title="Evidence scope">{dashboard.metric_note}</Alert>
      <div className="grid gap-4 xl:grid-cols-2"><Card><CardHeader title="Handover permission check" description="A recorded backup is not enough: the replacement must actually hold the permissions the work needs." icon={<ShieldCheck />} /><CardBody className="grid gap-2"><label className="grid max-w-lg gap-1 text-[13px] font-semibold text-foreground-2">Scenario<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={checkScenarioId} onChange={event => { setCheckScenarioId(event.target.value); setCheck(null); }}><option value="">Select a scenario</option>{dashboard.scenarios.map(item => <option key={item.id} value={item.id}>{item.name} · {item.kind}</option>)}</select></label><div><Button variant="secondary" onClick={() => void runHandoverCheck()} disabled={!checkScenarioId || checkBusy}>{checkBusy ? "Checking…" : "Check handover"}</Button></div>{check && <>{check.resources.length ? check.resources.map(resource => <div key={resource.resource_id} className="rounded-lg border border-border p-3"><div className="flex items-center justify-between gap-3"><span className="text-sm font-semibold">{resource.resource_name}</span><Badge tone={resource.eligible_candidates.length > 0 ? "success" : "warning"}>{resource.eligible_candidates.length > 0 ? "Eligible replacement" : "No eligible replacement"}</Badge></div><p className="mt-1 text-xs text-muted">{resource.permission_check === "not_applicable" ? "No department scope to check — eligibility rests on the backup relationship and skills." : `Requires ${resource.required_permissions.join(", ") || "no platform permissions"}`}</p>{resource.candidates.map(candidate => <p key={candidate.node_id} className="mt-1 text-xs text-muted">{candidate.eligible ? "Replacement holds the required permissions." : candidate.gaps.map(gap => gap.detail).join(" ")}</p>)}</div>) : <p className="text-sm text-muted">No handover recommendations for this scenario.</p>}</>}</CardBody></Card><Card><CardHeader title="Disruption drills" description="Seed the five drills, then compare a drill run with what the simulation forecast." icon={<Activity />} /><CardBody className="grid gap-2"><div><Button variant="secondary" onClick={() => void seedDrills()} disabled={drillBusy}>{drillBusy ? "Seeding…" : "Seed the five drills"}</Button></div>{drillSeed && <p className="text-xs text-muted">{drillSeed.created.length} drill scenario(s) created · {drillSeed.existing.length} already existed · {drillSeed.skipped.length} skipped (no suitable resource).</p>}{drillScenarios.length ? drillScenarios.map(item => <div key={item.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="text-sm">{item.name}</span><Badge>{item.kind}</Badge></div>) : <p className="text-sm text-muted">No drill scenarios yet.</p>}{drillRuns.length > 0 && <><p className="text-xs font-semibold text-foreground-2">Drill runs</p>{drillRuns.slice(0, 5).map(run => <div key={run.id} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="text-sm">{run.status} · {new Date(run.disruption_at).toLocaleDateString()}</span><Button size="sm" variant="secondary" onClick={() => void loadComparison(run.id)} disabled={comparisonBusy === run.id}>{comparisonBusy === run.id ? "Loading…" : "Compare"}</Button></div>)}</>}{comparison && <div className="rounded-lg border border-border p-3"><p className="text-sm font-semibold">{summarizeDrillComparison(comparison).headline}</p><p className="mt-1 text-xs text-muted">Affected {comparison.affected} · recovered {comparison.recovered} · simulated {comparison.simulated}</p>{comparison.differences.slice(0, 5).map((difference, index) => <p key={`${difference.code}-${index}`} className="mt-1 text-xs text-muted">{difference.code.replaceAll("_", " ")} — {difference.detail}</p>)}</div>}</CardBody></Card></div>
      <Card><CardHeader title="Company graph merge" description="Seed the company graph from continuity memberships. Records are proposed, never written directly — approval is required." icon={<Network />} /><CardBody className="grid gap-2"><label className="grid max-w-lg gap-1 text-[13px] font-semibold text-foreground-2">Company graph tenant ID<input className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={cgTenantId} onChange={event => setCgTenantId(event.target.value)} placeholder="Company graph tenant UUID" /></label><div className="flex flex-wrap gap-2"><Button variant="secondary" onClick={() => void seedMerge()} disabled={!cgTenantId.trim() || mergeBusy}>{mergeBusy ? "Working…" : "Seed graph merge"}</Button><Button variant="secondary" onClick={() => void loadMergeStatus()} disabled={!cgTenantId.trim() || mergeBusy}>Show merge status</Button></div>{mergeMessage && <p className="text-xs text-muted">{mergeMessage}</p>}{mergeStatus && <div className="grid gap-2 sm:grid-cols-3"><Stat label="Continuity nodes linked" value={`${mergeStatus.bc_nodes_linked} / ${mergeStatus.bc_nodes}`} hint="By one shared identity" icon={<Network />} /><Stat label="Company records linked" value={`${mergeStatus.cg_records_linked} / ${mergeStatus.cg_records}`} hint="Visible to you" icon={<Database />} /><Stat label="Stale sources" value={mergeStatus.stale_sources.length} hint={mergeStatus.stale_sources.length ? mergeStatus.stale_sources.map(source => source.name).join(", ") : "All sources recently synced"} icon={<Clock3 />} /></div>}</CardBody></Card></>}
    {loading && !dashboard && <Skeleton className="h-56 w-full" />}
  </div>;
}

export function IntegrationsWorkspace() {
  const [tenants, setTenants] = useState<{ id: string; name: string; role: string }[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [catalog, setCatalog] = useState<{ id: string; name?: string; provider?: string; description?: string; operations?: string[]; scopes?: string[] }[]>([]);
  const [overview, setOverview] = useState<{ role: string; connectors: { id: string; name: string; provider: string; enabled: boolean; health: string; credential_configured: boolean; last_sync_at: string | null; verified_at: string | null }[]; jobs: { id: string; connector_id: string; operation: string; status: string; digest: string; approval_expires_at: string | null; created_at: string; last_error: string | null }[] } | null>(null);
  const [bank, setBank] = useState<{ active: string | null; available: boolean; source: string; admin_mode: string | null; environment_mode: string; http_configured: boolean; http_host: string | null; is_mock: boolean; real_bank: boolean; problem: string | null } | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyJobId, setBusyJobId] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => { let live = true; Promise.all([request<{ items: typeof tenants }>("GET", "/api/integrations/tenants"), request<{ items: typeof catalog }>("GET", "/api/integrations/catalog"), request<typeof bank>("GET", "/api/finance/connector").catch(() => null)]).then(([a, b, c]) => { if (live) { setTenants(a.items); setTenantId(current => current || a.items[0]?.id || ""); setCatalog(b.items); setBank(c); } }).catch(err => { if (live) setError(describeError(err)); }); return () => { live = false; }; }, []);
  useEffect(() => { if (!tenantId) return; let live = true; request<typeof overview>("GET", `/api/integrations/${encodeURIComponent(tenantId)}/overview`).then(result => { if (live) setOverview(result); }).catch(err => { if (live) setError(describeError(err)); }); return () => { live = false; }; }, [tenantId, revision]);
  const ready = overview?.connectors.filter(item => item.enabled && item.health === "healthy").length ?? 0;
  const pendingJobs = pendingApprovals(overview?.jobs ?? []);
  const bankInfo = bankLabel(bank);
  async function decideJob(jobId: string, jobDigest: string, approve: boolean) {
    setBusyJobId(jobId); setError(""); setNotice("");
    try {
      await request("POST", `/api/integrations/${encodeURIComponent(tenantId)}/jobs/${encodeURIComponent(jobId)}/decision`, { digest: jobDigest, approve });
      setNotice(approve ? "Action approved and queued for the worker." : "Action rejected.");
      setRevision(value => value + 1);
    } catch (err) { setError(describeError(err)); } finally { setBusyJobId(null); }
  }
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M10" eyebrow="MODULE 10 · CONNECTIVITY" title="Integrations & communication" description="Review tenant-owned connectors, sync jobs and provider health. External dispatch stays approval-gated and worker-controlled." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)}><RefreshCw /> Refresh</Button>} />
    {error && <ErrorAlert error={error} />}
    {notice && <Alert tone="success" title={notice} />}
    {tenants.length > 0 && <label className="grid max-w-lg gap-1 text-[13px] font-semibold text-foreground-2">Integration tenant<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={tenantId} onChange={event => setTenantId(event.target.value)}>{tenants.map(item => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}</select></label>}
    {!tenants.length && !error && <EmptyModule title="No integration tenant is available" description="Connectors are isolated by company tenant and owner. Create or grant a company graph tenant before configuring integrations." />}
    {overview && <><div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><Stat label="Configured connectors" value={overview.connectors.length} hint={`Visible to ${overview.role}`} icon={<Boxes />} /><Stat label="Healthy & enabled" value={ready} hint="Only verified healthy connectors" icon={<Check />} /><Stat label="Recent jobs" value={overview.jobs.length} hint="Latest saved job records" icon={<Activity />} /><Stat label="Needs attention" value={overview ? needsAttentionCount(overview.jobs) : 0} hint="Review before retrying or dispatch" icon={<CircleAlert />} /></div>
      <Card><CardHeader title="Pending approvals" description="Actions waiting for a human decision. Approving queues the action for the worker; rejecting cancels it. Nothing executes without this decision." icon={<Clock3 />} /><CardBody className="grid gap-2">{pendingJobs.length ? pendingJobs.map(job => <div key={job.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="text-sm font-medium">{job.operation.replaceAll("_", " ")}</p><p className="text-xs text-muted">Requested {new Date(job.created_at).toLocaleString()}{job.approval_expires_at ? ` · approval expires ${new Date(job.approval_expires_at).toLocaleString()}` : ""}</p></div><div className="flex items-center gap-2"><Button variant="primary" disabled={busyJobId === job.id} onClick={() => void decideJob(job.id, job.digest, true)}>Approve</Button><Button variant="secondary" disabled={busyJobId === job.id} onClick={() => void decideJob(job.id, job.digest, false)}>Reject</Button></div></div>) : <p className="text-sm text-muted">No actions are waiting for approval.</p>}</CardBody></Card>
      <Card><CardHeader title="Connected services" description="Credentials stay encrypted server-side and are never shown in this dashboard." icon={<Boxes />} /><CardBody className="grid gap-2">{overview.connectors.length ? overview.connectors.map(connector => <div key={connector.id} className="grid gap-2 rounded-lg border border-border p-3 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center"><div className="min-w-0"><p className="truncate text-sm font-semibold">{connector.name}</p><p className="text-xs text-muted">{connector.provider} · credentials {connector.credential_configured ? "configured" : "not configured"}</p></div><Badge tone={connector.health === "healthy" && connector.enabled ? "success" : connector.health === "unverified" ? "warning" : "neutral"} dot>{connector.enabled ? connector.health : "disabled"}</Badge><span className="text-xs text-muted">{connector.last_sync_at ? `Synced ${new Date(connector.last_sync_at).toLocaleString()}` : "No sync recorded"}</span></div>) : <p className="text-sm text-muted">No connectors are configured for this tenant.</p>}</CardBody></Card>
      {bank && bankInfo && <Card><CardHeader title="Bank / payment connector" description="Payment connector in force, from Finance settings. Transfers stay approval-gated; change the connector in Finance settings, not here." icon={<Database />} /><CardBody className="grid gap-2"><div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="text-sm font-semibold">{bankInfo.title}</p><p className="text-xs text-muted">{bankInfo.detail}</p>{bank.problem && <p className="mt-1 text-xs text-danger-text">{bank.problem}</p>}</div><Badge tone={bankInfo.tone} dot>{bankInfo.state}</Badge></div></CardBody></Card>}
      <Card><CardHeader title="Available connector catalog" description={`${catalog.length} provider definitions available in the current application build.`} icon={<Activity />} /><CardBody className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{catalog.map(item => <div key={item.id} className="rounded-lg border border-border p-3"><p className="text-sm font-semibold">{item.name ?? item.id}</p><p className="mt-1 text-xs text-muted">{item.description ?? item.provider ?? "Provider configuration available"}</p>{item.operations?.length ? <p className="mt-1 text-xs text-muted">Operations: {item.operations.join(", ")}</p> : null}{item.scopes?.length ? <p className="mt-1 text-xs text-muted">Scopes: {item.scopes.join(", ")}</p> : null}</div>)}</CardBody></Card>
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
    {data && <><div className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface p-4"><Badge tone={data.report.decision === "GO" ? "success" : data.report.decision === "NO-GO" ? "danger" : "warning"} dot>{data.report.decision}</Badge><span className="text-sm font-semibold">Release evidence</span><span className="max-w-full truncate font-mono text-xs text-muted" title={data.report.snapshot_sha256}>snapshot {data.report.snapshot_sha256.slice(0, 16)}…</span><span className="ml-auto text-xs text-muted">{new Date(data.report.evaluated_at).toLocaleString()}</span></div>
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
      { label: "Decision memory", description: "Review org decisions, their evidence and review dates.", view: "decisions" },
      { label: "Knowledge quality", description: "Review traces, refusals and citation evidence.", view: "traces" },
      { label: "People & access", description: "Manage people, roles and scoped permissions.", view: "people" },
      { label: "Account security", description: "Inspect sign-in and multi-factor security settings.", view: "security" },
      { label: "Audit history", description: "Review immutable account and system activity records.", view: "logs" },
    ].map(item => <button key={item.view} className="module-launch-card" onClick={() => onNavigate(item.view)}><span><strong>{item.label}</strong><span>{item.description}</span></span><ArrowRight className="size-4 shrink-0" /></button>)}</CardBody></Card>
    <Alert title="Authorization stays in force">Each workspace continues to enforce its own owner, department, reviewer and administrator permissions. This page does not grant access.</Alert>
  </div>;
}

async function optional<T>(promise: Promise<T>): Promise<T | null> {
  // Health and trust return 404 when a tenant has no recorded
  // versions yet — an empty state, not an error.
  try { return await promise; }
  catch (err) { if (err instanceof ApiError && err.status === 404) return null; throw err; }
}

export function TwinWorkspace() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [versions, setVersions] = useState<TwinVersion[]>([]);
  const [health, setHealth] = useState<TwinHealth | null>(null);
  const [trust, setTrust] = useState<TwinTrust | null>(null);
  const [findings, setFindings] = useState<TwinFinding[]>([]);
  const [findingCounts, setFindingCounts] = useState<Record<string, number> | null>(null);
  const [proposals, setProposals] = useState<TwinProposal[]>([]);
  const [proposalCounts, setProposalCounts] = useState<Record<string, number> | null>(null);
  const [alerts, setAlerts] = useState<TwinHealthAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true;
    graphApi.tenants().then(({ items }) => { if (!live) return; setTenants(items); setTenantId(current => current || items[0]?.id || ""); })
      .catch(err => { if (live) setError(describeError(err)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    if (!tenantId) return;
    let live = true;
    setLoading(true); setError("");
    (async () => {
      try {
        const [versionResult, healthResult, trustResult, findingResult, findingCountResult, proposalResult, proposalCountResult, alertResult] = await Promise.all([
          twinApi.versions(tenantId),
          optional(twinApi.health(tenantId)),
          optional(twinApi.trust(tenantId)),
          twinApi.findings(tenantId),
          twinApi.findingCounts(tenantId),
          twinApi.proposals(tenantId),
          twinApi.proposalCounts(tenantId),
          twinApi.healthAlerts(tenantId),
        ]);
        if (!live) return;
        setVersions(versionResult.versions);
        setHealth(healthResult?.health ?? null);
        setTrust(trustResult?.trust ?? null);
        setFindings(findingResult.findings);
        setFindingCounts(findingCountResult.counts);
        setProposals(proposalResult.proposals);
        setProposalCounts(proposalCountResult.counts);
        setAlerts(alertResult.alerts);
      } catch (err) {
        if (live) setError(describeError(err));
      } finally {
        if (live) setLoading(false);
      }
    })();
    return () => { live = false; };
  }, [tenantId, revision]);
  async function decide(proposal: TwinProposal, decision: "approve" | "reject") {
    setBusy(`${proposal.id}:${decision}`); setError("");
    try { await twinApi.decideProposal(tenantId, proposal.id, decision, proposal.action_hash); setRevision(value => value + 1); }
    catch (err) { setError(describeError(err)); }
    finally { setBusy(""); }
  }
  async function execute(proposal: TwinProposal) {
    setBusy(`${proposal.id}:execute`); setError("");
    try { await twinApi.executeProposal(tenantId, proposal.id); setRevision(value => value + 1); }
    catch (err) { setError(describeError(err)); }
    finally { setBusy(""); }
  }
  async function acknowledge(finding: TwinFinding) {
    setBusy(`${finding.id}:ack`); setError("");
    try { await twinApi.acknowledgeFinding(tenantId, finding.id); setRevision(value => value + 1); }
    catch (err) { setError(describeError(err)); }
    finally { setBusy(""); }
  }
  const rows = timelineRows(versions);
  const inbox = inboxRows(proposals);
  const gaugeFill: Record<string, string> = { critical: "var(--color-danger)", degraded: "var(--color-warning)", healthy: "var(--color-success)" };
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="K" eyebrow="STEP K · DIGITAL TWIN" title="Digital twin" description="Drift, health, versions and the proposal inbox for this graph tenant. The twin only proposes — nothing here executes until a human approves it." actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)} disabled={loading}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>} />
    {error && <ErrorAlert error={error} />}
    {loading && !versions.length && !tenants.length && <Skeleton className="h-56 w-full" />}
    {!loading && !tenants.length && !error && <EmptyModule title="No graph tenant is available" description="Twin data is tenant-scoped. Create or grant a company graph tenant before drift, health or proposals can appear." />}
    {!!tenants.length && <>
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface p-4 shadow-xs">
        <label className="grid min-w-60 gap-1 text-[13px] font-semibold text-foreground-2">Digital twin tenant<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={tenantId} onChange={event => setTenantId(event.target.value)}>{tenants.map(item => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}</select></label>
        <Badge tone="success" dot>Tenant scope enforced</Badge>
      </section>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Health score" value={health ? health.health_score : "—"} hint={health ? `Band ${health.band}` : "No versions recorded yet"} icon={<Gauge />} />
        <Stat label="Trust score" value={trust?.score ?? "—"} hint={trust ? `Band ${trust.band} · coverage ${Math.round(trust.coverage * 100)}%` : "No versions recorded yet"} icon={<ShieldCheck />} />
        <Stat label="Versions" value={versions.length} hint="Recorded twin snapshots" icon={<Layers3 />} />
        <Stat label="Actionable proposals" value={proposalCounts?.actionable ?? "—"} hint={`${proposalCounts?.pending_approval ?? 0} pending approval`} icon={<Clock3 />} />
      </div>
      {health && <Card><CardHeader title="Health gauge" description={`Latest version ${shortFingerprint(health.fingerprint)} · as of ${health.as_of ? new Date(health.as_of).toLocaleString() : "—"}`} icon={<Gauge />} /><CardBody className="grid gap-3">
        <div className="flex items-center gap-3"><span className="text-3xl font-semibold">{health.health_score}</span><Badge tone={health.band === "healthy" ? "success" : health.band === "degraded" ? "warning" : "danger"}>{health.band}</Badge><span className="text-sm text-muted">Drift {health.drift_score ?? "—"} · trust {health.trust ?? "—"}</span></div>
        <div className="flex h-3 w-full overflow-hidden rounded-full border border-border bg-surface" role="img" aria-label={`Health ${health.health_score} out of 100, ${health.band}`}>
          {gaugeSegments(health.health_score).map(segment => <div key={segment.band} className="relative h-full" style={{ width: `${segment.to - segment.from}%` }}><div className="h-full" style={{ width: `${(segment.filled / (segment.to - segment.from)) * 100}%`, background: gaugeFill[segment.band] }} /></div>)}
        </div>
        <p className="text-sm text-muted">Deductions — drift {health.components.drift} · money {health.components.money} · stale sources {health.components.freshness}{health.stale_sources.length ? ` · stale: ${health.stale_sources.join(", ")}` : ""}</p>
      </CardBody></Card>}
      {trust && <Card><CardHeader title="Multi-source trust" description={`Coverage ${Math.round(trust.coverage * 100)}% of source weight · band ${trust.band}`} icon={<ShieldCheck />} /><CardBody className="grid gap-2">{trust.sources.map(source => <div key={source.source} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><span className="min-w-0 truncate text-sm">{source.source.replaceAll("_", " ")}{source.known_source ? "" : " (unlisted)"}</span><div className="flex items-center gap-2"><span className="text-xs text-muted">weight {source.weight}</span><Badge tone={source.status === "fresh" ? "success" : source.status === "stale" ? "danger" : "neutral"}>{source.status}</Badge><span className="text-sm font-semibold">{source.score ?? "—"}</span></div></div>)}</CardBody></Card>}
      <div className="grid gap-4 xl:grid-cols-2">
        <Card><CardHeader title="Drift timeline" description="Newest first — one row per recorded twin version." icon={<Activity />} /><CardBody className="grid gap-2">{rows.length ? rows.slice(0, 10).map(row => <div key={row.fingerprint} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{row.label}</p><p className="text-xs text-muted">{new Date(row.asOf).toLocaleString()} · {shortFingerprint(row.fingerprint)}</p></div><div className="flex items-center gap-2">{row.trust && <Badge tone={row.trust === "trustworthy" ? "success" : row.trust === "review" ? "warning" : "danger"}>{row.trust}</Badge>}{row.staleSources.length > 0 && <Badge tone="warning">stale: {row.staleSources.join(", ")}</Badge>}</div></div>) : <p className="text-sm text-muted">No twin versions are recorded for this tenant yet.</p>}</CardBody></Card>
        <Card><CardHeader title="Version history" description="Recorded snapshots — summaries only, never the full snapshot." icon={<Layers3 />} /><CardBody className="grid gap-2">{versions.length ? versions.slice(0, 10).map(version => <div key={version.fingerprint} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">Version {version.seq} · {shortFingerprint(version.fingerprint)}</p><p className="text-xs text-muted">{new Date(version.as_of).toLocaleString()} · {version.entity_count ?? "—"} entities · {version.relationship_count ?? "—"} relationships</p></div><Badge tone={version.drift_score == null ? "neutral" : version.drift_score <= 25 ? "success" : version.drift_score <= 60 ? "warning" : "danger"}>{version.is_first_version ? "first" : `drift ${version.drift_score}`}</Badge></div>) : <p className="text-sm text-muted">No twin versions are recorded for this tenant yet.</p>}</CardBody></Card>
      </div>
      <Card><CardHeader title="Proposal inbox" description="Read-only until approval — decide the exact action shown, then execute. The twin never applies anything itself." icon={<Clock3 />} /><CardBody className="grid gap-2">
        {inbox.length ? inbox.map(row => <div key={row.id} className="grid gap-2 rounded-lg border border-border p-3">
          <div className="flex flex-wrap items-center justify-between gap-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{row.actionType.replaceAll("_", " ")} · {row.code.replaceAll("_", " ")}</p><p className="text-xs text-muted">Approver: {row.approverClass} · {row.requiredPermission}</p></div><Badge tone={proposalStatusTone(row.status)}>{row.status.replaceAll("_", " ")}</Badge></div>
          <p className="text-sm text-muted">{row.detail}</p>
          {row.moneyRedacted && <p className="text-xs text-muted">Money amounts hidden — owner only.</p>}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="primary" onClick={() => void decide(proposals.find(item => item.id === row.id)!, "approve")} loading={busy === `${row.id}:approve`} disabled={!row.canDecide || Boolean(busy)}>Approve</Button>
            <Button size="sm" variant="secondary" onClick={() => void decide(proposals.find(item => item.id === row.id)!, "reject")} loading={busy === `${row.id}:reject`} disabled={!row.canDecide || Boolean(busy)}>Reject</Button>
            <Button size="sm" variant="secondary" onClick={() => void execute(proposals.find(item => item.id === row.id)!)} loading={busy === `${row.id}:execute`} disabled={!row.canExecute || Boolean(busy)}>Execute</Button>
          </div>
        </div>) : <p className="text-sm text-muted">No twin proposals for this tenant yet.</p>}
      </CardBody></Card>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card><CardHeader title="Review queue" description={queueSummary(findingCounts)} icon={<CircleAlert />} /><CardBody className="grid gap-2">{findings.length ? findings.slice(0, 8).map(finding => <div key={finding.id} className="grid gap-2 rounded-lg border border-border p-3"><div className="flex flex-wrap items-center justify-between gap-3"><span className="min-w-0 truncate text-sm font-semibold">{finding.code.replaceAll("_", " ")}</span><Badge tone={findingTone(finding.severity)}>{finding.severity}</Badge></div><p className="text-sm text-muted">{finding.detail}</p><div className="flex flex-wrap items-center gap-2"><Badge tone={finding.acknowledged ? "success" : "neutral"}>{finding.acknowledged ? "Acknowledged" : "Unacknowledged"}</Badge>{!finding.acknowledged && <Button size="sm" variant="secondary" onClick={() => void acknowledge(finding)} loading={busy === `${finding.id}:ack`} disabled={Boolean(busy)}>Acknowledge</Button>}</div></div>) : <p className="text-sm text-muted">No reconciliation findings for this tenant yet.</p>}</CardBody></Card>
        <Card><CardHeader title="Health alerts" description="Watch alerts from the latest health evaluation." icon={<Gauge />} /><CardBody className="grid gap-2">{alerts.length ? alerts.slice(0, 8).map(alert => <div key={alert.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{alert.message}</p><p className="text-xs text-muted">Rule {alert.rule_id} · {new Date(alert.evaluated_at).toLocaleString()}</p></div><Badge tone={alert.severity === "critical" ? "danger" : "warning"}>{alert.severity}</Badge></div>) : <p className="text-sm text-muted">No health alerts for this tenant yet.</p>}</CardBody></Card>
      </div>
      <Alert title="The twin proposes, humans decide">Feed changes, findings and proposals are suggestions only. Applying one needs an approved platform action bound to that exact change, and the twin never writes the graph, grants access, restores data, switches a supplier or pays anything.</Alert>
    </>}
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
    { label: "Digital twin", detail: "Drift, health, versions, proposals", view: "digital-twin" },
  ], []);
  return <div className="grid gap-5 pb-8">
    <ModulePageHeader module="M09" eyebrow="MODULE 09 · PRODUCT EXPERIENCE" title="Frontend & UX workspace" description="A direct index into InfraSpine's daily work surfaces, with the shared pine and sage visual system." />
    <section className="module-ux-hero"><div><Badge tone="success" dot>12 modules · one control desk</Badge><h2>Move from overview to action in one step</h2><p>Choose a work area below or send a scoped instruction to its module specialist. Answers and approvals return to the Master Control Center.</p></div><div className="module-ux-hero-mark"><Boxes aria-hidden /><span>IRFANIX</span></div></section>
    <Card><CardHeader title="Open an application workspace" description="These links open live product views backed by the current app APIs." icon={<Sparkles />} /><CardBody className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{modules.map(item => <button key={item.view} className="module-launch-card" onClick={() => onNavigate(item.view)}><span><strong>{item.label}</strong><span>{item.detail}</span></span><ArrowRight className="size-4 shrink-0" /></button>)}</CardBody></Card>
    <Card><CardHeader title="Interface conventions" description="The design tokens are shared across the app and respect a configured company brand color." icon={<Activity />} /><CardBody className="grid gap-3 sm:grid-cols-3"><div className="module-design-note"><span className="module-swatch module-swatch-green" /><strong>Evergreen actions</strong><p>Primary actions, active navigation and focus states.</p></div><div className="module-design-note"><span className="module-swatch module-swatch-sage" /><strong>Sage surfaces</strong><p>Quiet backgrounds and clear layers keep dense screens readable.</p></div><div className="module-design-note"><span className="module-swatch module-swatch-amber" /><strong>Review states</strong><p>Amber and red remain reserved for pending decisions and errors.</p></div></CardBody></Card>
  </div>;
}
