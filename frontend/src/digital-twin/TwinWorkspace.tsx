import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, CircleAlert, Clock3, Database, Network, RefreshCw, ShieldCheck, TrendingUp, Workflow } from "lucide-react";
import { describeError } from "../api";
import { graphApi } from "../company-graph/api";
import { GraphMap } from "../company-graph/GraphMap";
import type { Graph, GraphRecord, Tenant } from "../company-graph/types";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, PageHeader, Skeleton, Stat } from "../components/ui";
import { TWIN_KIND_TO_GRAPH_KINDS, TWIN_TEMPLATES, twinApi } from "./api";
import type { PredictAbsenceResult, TwinOverview, WhatIfResult } from "./types";

function moneyLabel(totals: Record<string, number>): string {
  const entries = Object.entries(totals);
  if (!entries.length) return "No money tracked";
  return entries.map(([currency, amount]) => `${currency} ${amount.toLocaleString()}`).join(" · ");
}

function stateTone(state: string): "danger" | "warning" | "neutral" {
  return state === "failed" ? "danger" : state === "degraded" ? "warning" : "neutral";
}

export function DigitalTwinWorkspace() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [overview, setOverview] = useState<TwinOverview | null>(null);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [selected, setSelected] = useState<GraphRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  const [templateId, setTemplateId] = useState<string>(TWIN_TEMPLATES[0].id);
  const [seedId, setSeedId] = useState("");
  const [horizonDays, setHorizonDays] = useState(7);
  const [rehearse, setRehearse] = useState(false);
  const [simulating, setSimulating] = useState(false);
  const [simulation, setSimulation] = useState<WhatIfResult | null>(null);
  const [simulationError, setSimulationError] = useState("");

  const [absenceId, setAbsenceId] = useState("");
  const [predicting, setPredicting] = useState(false);
  const [prediction, setPrediction] = useState<PredictAbsenceResult | null>(null);
  const [predictionError, setPredictionError] = useState("");

  useEffect(() => {
    let live = true;
    graphApi.tenants().then(({ items }) => { if (!live) return; setTenants(items); setTenantId(current => current || items[0]?.id || ""); })
      .catch(err => { if (live) setError(describeError(err)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    if (!tenantId) { setOverview(null); setGraph(null); setSimulation(null); setPrediction(null); return; }
    let live = true;
    setLoading(true); setError(""); setSelected(null); setSeedId(""); setAbsenceId("");
    Promise.all([twinApi.overview(tenantId), graphApi.map(tenantId)])
      .then(([nextOverview, nextGraph]) => { if (!live) return; setOverview(nextOverview); setGraph(nextGraph); })
      .catch(err => { if (live) setError(describeError(err)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [tenantId, revision]);

  const template = TWIN_TEMPLATES.find(item => item.id === templateId) ?? TWIN_TEMPLATES[0];
  const seedKinds = useMemo(
    () => template.seedKinds.flatMap(kind => TWIN_KIND_TO_GRAPH_KINDS[kind] ?? [kind]),
    [template],
  );
  const seedCandidates = useMemo(
    () => (graph?.entities ?? []).filter(entity => seedKinds.includes(entity.kind)),
    [graph, seedKinds],
  );
  const people = useMemo(
    () => (graph?.entities ?? []).filter(entity => entity.kind === "employee"),
    [graph],
  );

  const expand = useCallback(async (id: string) => {
    try { setGraph(await graphApi.neighbors(tenantId, id, 2)); }
    catch (err) { setError(describeError(err)); }
  }, [tenantId]);

  async function runSimulation() {
    if (!tenantId || !seedId) return;
    setSimulating(true); setSimulationError(""); setSimulation(null);
    try {
      setSimulation(await twinApi.whatIf(tenantId, {
        template_id: templateId,
        seed_ids: [seedId],
        horizon_days: horizonDays,
        rehearse,
      }));
    } catch (err) { setSimulationError(describeError(err)); }
    finally { setSimulating(false); }
  }

  async function runPrediction() {
    if (!tenantId || !absenceId) return;
    setPredicting(true); setPredictionError(""); setPrediction(null);
    try {
      setPrediction(await twinApi.predictAbsence(tenantId, { absent_ids: [absenceId], horizon_days: 7 }));
    } catch (err) { setPredictionError(describeError(err)); }
    finally { setPredicting(false); }
  }

  const sim = simulation?.simulation;
  return <div className="grid gap-5 pb-8">
    <PageHeader
      eyebrow="STEP K · COMPANY DIGITAL TWIN"
      title="Digital twin"
      description="A live, read-only model of the company: people, systems, suppliers and money flow. Simulations never change, approve or pay anything."
      actions={<Button variant="secondary" onClick={() => setRevision(value => value + 1)} disabled={loading}><RefreshCw className={loading ? "animate-spin" : ""} /> Refresh</Button>}
    />
    {error && <Alert tone="danger" title="Live twin data could not be loaded">{error}</Alert>}
    {loading && !overview && <Skeleton className="h-72 w-full" />}
    {!loading && !tenants.length && !error && <Card><EmptyState icon={<Network />} title="No twin workspace is available" description="This account has no company graph tenant. Create or grant a tenant in Company intelligence setup before twin data can appear." /></Card>}
    {!!tenants.length && <>
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface p-4 shadow-xs">
        <label className="grid min-w-60 gap-1 text-[13px] font-semibold text-foreground-2">Digital twin workspace<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={tenantId} onChange={event => setTenantId(event.target.value)}>{tenants.map(item => <option key={item.id} value={item.id}>{item.name} · {item.role}</option>)}</select></label>
        <Badge tone="success" dot>Tenant scope enforced · read-only</Badge>
      </section>
      {overview && <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Entities" value={overview.entity_count} hint="Visible in this twin scope" icon={<Network />} />
        <Stat label="Relationships" value={overview.relationship_count} hint="Current twin snapshot" icon={<Activity />} />
        <Stat label="Money at risk" value={moneyLabel(overview.money_at_risk.totals)} hint={`${overview.money_at_risk.batch_count} batches · ${overview.money_at_risk.payment_count} payments`} icon={<TrendingUp />} />
        <Stat label="Stale sources" value={overview.stale_sources.length} hint={overview.stale_sources.length ? overview.stale_sources.join(", ") : "All sources fresh"} icon={<CircleAlert />} />
      </div>}
      {overview && <Card><CardHeader title="Snapshot freshness" description={`Last confirmed ${new Date(overview.as_of).toLocaleString()} · fingerprint ${overview.fingerprint.slice(0, 16)}…`} icon={<Clock3 />} /><CardBody className="grid gap-2">{overview.sources.length ? overview.sources.map(source => <div key={source.name} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"><div className="min-w-0"><p className="truncate text-sm font-semibold">{source.name}</p><p className="text-xs text-muted">{source.kind}{source.last_synced_at ? ` · last synced ${new Date(source.last_synced_at).toLocaleString()}` : " · never synced"}</p></div><Badge tone={source.status === "fresh" ? "success" : source.status === "stale" ? "warning" : "neutral"} dot>{source.status}</Badge></div>) : <p className="text-sm text-muted">No sync sources are recorded for this twin yet.</p>}{overview.truncated && <Alert title="Snapshot truncated">The twin snapshot was truncated at the display limit. Counts above cover the full scope; the map below shows the truncated view.</Alert>}</CardBody></Card>}
      {graph && <Card><CardHeader title="Live company map" description={`${graph.entities.length} visible entities · ${graph.relationships.length} visible relationships · snapshot ${new Date(graph.as_of).toLocaleString()}`} icon={<Network />} /><div className="cg-workspace-map">{graph.entities.length ? <GraphMap graph={graph} selected={selected?.id} onSelect={record => setSelected(record)} onExpand={id => void expand(id)} /> : <EmptyState title="The graph is empty" description="Add approved source records to make company relationships visible here." />}</div></Card>}
      <div className="grid gap-4 xl:grid-cols-2">
        <Card><CardHeader title="What-if simulation" description="Ask what breaks if something stops. Simulation only — nothing is executed, approved or paid." icon={<Workflow />} /><CardBody className="grid gap-3">
          <label className="grid gap-1 text-[13px] font-semibold text-foreground-2">Scenario<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={templateId} onChange={event => { setTemplateId(event.target.value); setSeedId(""); const next = TWIN_TEMPLATES.find(item => item.id === event.target.value); if (next) setHorizonDays(next.defaultHorizonDays); }}>{TWIN_TEMPLATES.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select><span className="text-xs font-normal text-muted">{template.description}</span></label>
          <label className="grid gap-1 text-[13px] font-semibold text-foreground-2">Affected entity<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={seedId} onChange={event => setSeedId(event.target.value)}><option value="">Choose an entity…</option>{seedCandidates.map(item => <option key={item.id} value={item.id}>{item.name} · {item.kind}</option>)}</select>{!seedCandidates.length && <span className="text-xs font-normal text-muted">No matching entities are visible in this twin scope.</span>}</label>
          <label className="grid gap-1 text-[13px] font-semibold text-foreground-2">Horizon (days)<input type="number" min={1} max={30} className="h-10 w-28 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={horizonDays} onChange={event => setHorizonDays(Math.max(1, Math.min(30, Number(event.target.value) || 1)))} /></label>
          <label className="flex items-center gap-2 text-[13px] font-medium"><input type="checkbox" checked={rehearse} onChange={event => setRehearse(event.target.checked)} /> Rehearsal mode — numbered practice steps, no side effects</label>
          <div><Button variant="primary" onClick={() => void runSimulation()} loading={simulating} disabled={!seedId || simulating}>Run simulation</Button></div>
          {simulationError && <Alert tone="danger">{simulationError}</Alert>}
          {sim && <>
            <div className="flex flex-wrap items-center gap-2"><Badge tone="primary" dot>Simulation · dry run</Badge><span className="text-xs text-muted">{sim.affected.length} affected · {sim.failed.length} failed · {sim.degraded.length} degraded · {sim.exposed.length} exposed</span></div>
            <p className="text-sm"><strong>Money at risk:</strong> {moneyLabel(sim.money_at_risk.totals)}</p>
            <div className="grid gap-2">{sim.affected.slice(0, 12).map(item => <div key={item.entity.id} className="rounded-lg border border-border p-3"><div className="flex items-center justify-between gap-2"><span className="min-w-0 truncate text-sm font-semibold">{item.entity.name}</span><Badge tone={stateTone(item.state)}>{item.state}</Badge></div><p className="mt-1 text-xs text-muted">{item.entity.kind} · severity {item.severity} · confidence {Math.round(item.confidence * 100)}%{item.covered_by.length ? ` · covered by ${item.covered_by.map(person => person.name).join(", ")}` : ""}</p>{item.recovery_options[0] && <p className="mt-1 text-xs">Recovery: {item.recovery_options[0].action.replaceAll("_", " ")} — {item.recovery_options[0].owner}</p>}</div>)}</div>
            {sim.recovery_plan.waves.length > 0 && <div className="grid gap-2"><p className="text-[13px] font-semibold">Recovery waves</p>{sim.recovery_plan.waves.map((wave, index) => <div key={index} className="rounded-lg border border-border p-3"><p className="text-xs font-semibold">Wave {index + 1}</p><ul className="mt-1 grid gap-1">{wave.map(step => <li key={step.entity_id} className="text-xs text-muted">{step.entity_name} — {step.action.replaceAll("_", " ")} ({step.owner})</li>)}</ul></div>)}</div>}
            {sim.rehearsal && <div className="grid gap-2"><p className="text-[13px] font-semibold">Rehearsal steps (practice only)</p>{sim.rehearsal.steps.slice(0, 12).map(step => <p key={step.number} className="text-xs text-muted">{step.number}. {step.entity_name} — {step.action.replaceAll("_", " ")} ({step.owner})</p>)}</div>}
            {sim.assumptions.length > 0 && <p className="text-xs text-muted">Assumptions: {sim.assumptions.join(" · ")}</p>}
          </>}
        </CardBody></Card>
        <Card><CardHeader title="Absence prediction" description="Predict the impact if one person is away for 7 days. Advisory only — no decisions are made." icon={<ShieldCheck />} /><CardBody className="grid gap-3">
          <label className="grid gap-1 text-[13px] font-semibold text-foreground-2">Person<select className="h-10 rounded-md border border-border-strong bg-surface px-3 text-sm text-foreground" value={absenceId} onChange={event => setAbsenceId(event.target.value)}><option value="">Choose a person…</option>{people.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>{!people.length && <span className="text-xs font-normal text-muted">No people are visible in this twin scope.</span>}</label>
          <div><Button variant="secondary" onClick={() => void runPrediction()} loading={predicting} disabled={!absenceId || predicting}>Predict absence</Button></div>
          {predictionError && <Alert tone="danger">{predictionError}</Alert>}
          {prediction && <>
            <div className="flex flex-wrap items-center gap-2"><Badge tone={prediction.prediction.risk_score >= 80 ? "danger" : prediction.prediction.risk_score >= 60 ? "warning" : "success"} dot>Risk {prediction.prediction.risk_score} / 100</Badge><span className="text-xs text-muted">{prediction.prediction.absent_names.join(", ")} · {prediction.prediction.horizon_days} days · predicted {new Date(prediction.prediction.predicted_at).toLocaleString()}</span></div>
            <p className="text-sm">{prediction.prediction.affected.length} affected · {prediction.prediction.uncovered.length} uncovered · money at risk {moneyLabel(prediction.prediction.money_at_risk.totals)}</p>
            <div className="grid gap-2">{prediction.prediction.factors.map(factor => <p key={factor} className="rounded-lg border border-border p-3 text-xs">{factor}</p>)}</div>
          </>}
          <Alert title="Predictions are advisory">A prediction never schedules cover, reassigns work or approves anything. Confirm cover with the responsible manager before acting.</Alert>
        </CardBody></Card>
      </div>
      <Card><CardHeader title="What the twin cannot see" description="Visibility is limited by your tenant membership and the owner's finance-amount decision." icon={<Database />} /><CardBody><p className="text-sm text-muted">Entities you are not authorized to read never appear in counts, maps or simulations. Finance amounts stay owner-only until the owner decides otherwise. Every simulation is a dry run: it reads the snapshot and returns a result, it never writes, approves or pays.</p></CardBody></Card>
    </>}
  </div>;
}
