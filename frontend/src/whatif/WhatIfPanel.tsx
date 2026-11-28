import { useEffect, useState } from "react";
import { describeError } from "../api";
import type { Graph } from "../company-graph/types";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, NativeSelect, Textarea } from "../components/ui";
import { whatIfApi } from "./api";
import type { DrillComparison, Rehearsal, ScenarioTemplate, SimulationResult } from "./types";
import { comparisonHeadline, nextPendingStep, rehearsalProgress, seedOptions, summarizeSimulation } from "./whatIfPanel.mjs";

/**
 * What-if panel (Step AL slice 6). Lives under the company map so the
 * visible entities can be picked as scenario seeds.
 *
 * Everything here is practice only: simulations run over a snapshot,
 * rehearsals are numbered practice steps, and the drill comparison only
 * reads a Step G drill report. Nothing is executed, dispatched or paid.
 */
export function WhatIfPanel({ tenantId, graph }: { tenantId: string; graph: Graph | null }) {
  const [scenarios, setScenarios] = useState<ScenarioTemplate[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [seeds, setSeeds] = useState<string[]>([]);
  const [result, setResult] = useState<SimulationResult | null>(null);
  const [rehearsal, setRehearsal] = useState<Rehearsal | null>(null);
  const [rehearsals, setRehearsals] = useState<Rehearsal[]>([]);
  const [finding, setFinding] = useState("");
  const [skipReason, setSkipReason] = useState("");
  const [summary, setSummary] = useState("");
  const [bcTenantId, setBcTenantId] = useState("");
  const [runId, setRunId] = useState("");
  const [comparison, setComparison] = useState<DrillComparison | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    whatIfApi.scenarios()
      .then(({ scenarios: items }) => { if (!live) return; setScenarios(items); setTemplateId(current => current || items[0]?.template_id || ""); })
      .catch(err => { if (live) setError(describeError(err)); });
    whatIfApi.rehearsals(tenantId)
      .then(({ rehearsals: items }) => { if (live) setRehearsals(items); })
      .catch(err => { if (live) setError(describeError(err)); });
    return () => { live = false; };
  }, [tenantId]);

  const options = seedOptions(graph);
  const progress = rehearsal ? rehearsalProgress(rehearsal) : null;
  const pending = rehearsal ? nextPendingStep(rehearsal) : null;

  async function runSimulation() {
    if (!templateId || !seeds.length) { setError("Pick a scenario and at least one starting point."); return; }
    setBusy(true); setError(""); setComparison(null);
    try {
      setResult(await whatIfApi.simulate(tenantId, { template_id: templateId, seed_ids: seeds, rehearse: true }));
    } catch (err) { setError(describeError(err)); } finally { setBusy(false); }
  }

  async function startRehearsal() {
    if (!templateId || !seeds.length) return;
    setBusy(true); setError("");
    try {
      const created = await whatIfApi.startRehearsal(tenantId, { template_id: templateId, seed_ids: seeds });
      setRehearsal(created);
      setRehearsals(items => [created, ...items]);
    } catch (err) { setError(describeError(err)); } finally { setBusy(false); }
  }

  async function checkOff() {
    if (!rehearsal || !pending) return;
    setBusy(true); setError("");
    try {
      const updated = await whatIfApi.checkOff(tenantId, rehearsal.id, { step_number: pending.number, finding: finding || undefined });
      setRehearsal(updated); setFinding("");
      setRehearsals(items => items.map(item => item.id === updated.id ? updated : item));
    } catch (err) { setError(describeError(err)); } finally { setBusy(false); }
  }

  async function skipStep() {
    if (!rehearsal || !pending || !skipReason.trim()) { setError("Give a reason for skipping the step."); return; }
    setBusy(true); setError("");
    try {
      const updated = await whatIfApi.skip(tenantId, rehearsal.id, { step_number: pending.number, reason: skipReason.trim() });
      setRehearsal(updated); setSkipReason("");
      setRehearsals(items => items.map(item => item.id === updated.id ? updated : item));
    } catch (err) { setError(describeError(err)); } finally { setBusy(false); }
  }

  async function completeRehearsal() {
    if (!rehearsal) return;
    setBusy(true); setError("");
    try {
      const updated = await whatIfApi.complete(tenantId, rehearsal.id, { summary: summary || undefined });
      setRehearsal(updated); setSummary("");
      setRehearsals(items => items.map(item => item.id === updated.id ? updated : item));
    } catch (err) { setError(describeError(err)); } finally { setBusy(false); }
  }

  async function compareDrill() {
    if (!templateId || !seeds.length || !bcTenantId.trim() || !runId.trim()) {
      setError("Pick a scenario and seeds, then give the continuity tenant and drill run ids.");
      return;
    }
    setBusy(true); setError("");
    try {
      setComparison(await whatIfApi.compareDrill(tenantId, runId.trim(), {
        continuity_tenant_id: bcTenantId.trim(), template_id: templateId, seed_ids: seeds,
      }));
    } catch (err) { setError(describeError(err)); } finally { setBusy(false); }
  }

  const summaryText = result ? summarizeSimulation(result) : null;
  const selectedScenario = scenarios.find(item => item.template_id === templateId);

  return <Card>
    <CardHeader title="What-if simulations" description="Ask what happens if something fails, then practise the recovery. Nothing here runs, changes or pays anything — every simulation and rehearsal is practice only." />
    <CardBody className="grid gap-4">
      {error && <Alert tone="danger" title="What-if request failed">{error}</Alert>}
      <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
        <Field label="Scenario">
          <NativeSelect value={templateId} onChange={event => setTemplateId(event.target.value)} disabled={busy || !scenarios.length}>
            {scenarios.map(item => <option key={item.template_id} value={item.template_id}>{item.name}</option>)}
          </NativeSelect>
        </Field>
        <Field label="Starting points (visible entities)" hint="Tick the systems, people or processes the disruption starts from.">
          <div className="grid max-h-40 gap-1 overflow-y-auto rounded-md border border-border p-2">
            {options.length ? options.map(option => (
              <label key={option.id} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={seeds.includes(option.id)} disabled={busy}
                  onChange={event => setSeeds(current => event.target.checked ? [...current, option.id] : current.filter(id => id !== option.id))} />
                <span className="min-w-0 truncate">{option.name}</span>
                <Badge tone="neutral">{option.kind.replaceAll("_", " ")}</Badge>
              </label>
            )) : <p className="text-sm text-muted">No visible entities to start from.</p>}
          </div>
        </Field>
        <Button variant="primary" onClick={() => void runSimulation()} disabled={busy || !seeds.length}>Run simulation</Button>
      </div>
      {selectedScenario && <p className="text-sm text-muted">{selectedScenario.description} · Looks {selectedScenario.horizon_days} day(s) ahead.</p>}

      {result && summaryText && <>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div className="rounded-lg border border-border p-3"><p className="text-xs font-semibold text-muted">Affected</p><p className="text-lg font-semibold">{summaryText.affectedCount} ({summaryText.failedCount} failed, {summaryText.degradedCount} degraded)</p></div>
          <div className="rounded-lg border border-border p-3"><p className="text-xs font-semibold text-muted">Money at risk</p><p className="text-lg font-semibold">{summaryText.moneyText}</p></div>
          <div className="rounded-lg border border-border p-3"><p className="text-xs font-semibold text-muted">Recovery plan</p><p className="text-lg font-semibold">{summaryText.stepCount} step(s) in {summaryText.waveCount} wave(s)</p></div>
          <div className="rounded-lg border border-border p-3"><p className="text-xs font-semibold text-muted">Blocked by a cycle</p><p className="text-lg font-semibold">{summaryText.blockedCount}</p></div>
        </div>
        {result.recovery_plan.waves.map((wave, waveIndex) => (
          <div key={waveIndex} className="grid gap-2">
            <p className="text-sm font-semibold">Wave {waveIndex + 1}</p>
            {wave.map(step => <div key={step.entity_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-3">
              <span className="text-sm"><strong>{step.entity_name}</strong> — {step.action.replaceAll("_", " ")} · owner: {step.owner}</span>
              <Badge tone={step.state === "failed" ? "danger" : step.state === "degraded" ? "warning" : "neutral"}>{step.state}</Badge>
            </div>)}
          </div>
        ))}
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={() => void startRehearsal()} disabled={busy}>Start a practice run of this plan</Button>
        </div>
      </>}

      {rehearsal && progress && <div className="grid gap-3 rounded-xl border border-border bg-surface-muted p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <strong>Practice run: {rehearsal.title}</strong>
          <Badge tone={rehearsal.status === "completed" ? "success" : "warning"}>{rehearsal.status === "completed" ? "Completed" : "In progress"}</Badge>
        </div>
        <p className="text-sm text-muted">{progress.headline} Practice only — no step is executed.</p>
        {rehearsal.steps.map(step => <div key={step.number} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-surface p-3">
          <span className="text-sm"><strong>{step.number}. {step.entity_name}</strong> — {step.action.replaceAll("_", " ")} · owner: {step.owner}</span>
          <Badge tone={step.status === "done" ? "success" : step.status === "skipped" ? "neutral" : "warning"}>{step.status}</Badge>
        </div>)}
        {rehearsal.findings.length > 0 && <div className="grid gap-1"><p className="text-sm font-semibold">Findings so far</p>{rehearsal.findings.map((item, index) => <p key={index} className="text-sm text-muted">· {item}</p>)}</div>}
        {pending && <div className="grid gap-2">
          <p className="text-sm font-semibold">Next step: {pending.number}. {pending.entity_name} — {pending.action.replaceAll("_", " ")}</p>
          <Field label="What did the practice find? (optional)" hint="Recorded on the step when you mark it done.">
            <Textarea value={finding} onChange={event => setFinding(event.target.value)} disabled={busy} rows={2} />
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" onClick={() => void checkOff()} disabled={busy}>Mark step done</Button>
            <Input value={skipReason} onChange={event => setSkipReason(event.target.value)} disabled={busy} placeholder="Reason for skipping this step" />
            <Button variant="secondary" onClick={() => void skipStep()} disabled={busy || !skipReason.trim()}>Skip step</Button>
          </div>
        </div>}
        {progress.allDone && rehearsal.status !== "completed" && <div className="grid gap-2">
          <Field label="Practice summary (optional)">
            <Textarea value={summary} onChange={event => setSummary(event.target.value)} disabled={busy} rows={2} />
          </Field>
          <div><Button variant="primary" onClick={() => void completeRehearsal()} disabled={busy}>Complete practice run</Button></div>
        </div>}
      </div>}

      <div className="grid gap-2">
        <p className="text-sm font-semibold">Past practice runs</p>
        {rehearsals.length ? rehearsals.slice(0, 5).map(item => (
          <button key={item.id} type="button" onClick={() => setRehearsal(item)}
            className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-3 text-left text-sm hover:bg-surface-muted">
            <span className="min-w-0 truncate"><strong>{item.title}</strong> · {item.scenario.name} · {item.steps.length} step(s)</span>
            <Badge tone={item.status === "completed" ? "success" : "warning"}>{item.status}</Badge>
          </button>
        )) : <EmptyState title="No practice runs yet" description="Run a simulation and start a practice run to see it here." />}
      </div>

      <div className="grid gap-3 rounded-xl border border-border p-4">
        <p className="text-sm font-semibold">Compare with a real drill</p>
        <p className="text-sm text-muted">Runs the same scenario again and compares the predicted steps with what a Step G drill actually did. The drill report is only read — never changed.</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Continuity tenant id"><Input value={bcTenantId} onChange={event => setBcTenantId(event.target.value)} disabled={busy} placeholder="Business-continuity tenant" /></Field>
          <Field label="Drill run id"><Input value={runId} onChange={event => setRunId(event.target.value)} disabled={busy} placeholder="Drill run" /></Field>
        </div>
        <div><Button variant="secondary" onClick={() => void compareDrill()} disabled={busy || !seeds.length}>Compare with drill</Button></div>
        {comparison && <div className="grid gap-2">
          <Alert tone={comparison.missing.length ? "warning" : "success"} title={comparisonHeadline(comparison)}>
            {comparison.findings.length ? comparison.findings.join(" ") : "No further notes."}
          </Alert>
          {comparison.missing.length > 0 && <p className="text-sm">Predicted but not drilled: {comparison.missing.map(step => step.entity_name || step.key).join(", ")}</p>}
          {comparison.extra.length > 0 && <p className="text-sm">Drilled but not predicted: {comparison.extra.map(step => step.title || step.key).join(", ")}</p>}
          {comparison.owner_mismatches.length > 0 && <p className="text-sm">Different owner than predicted: {comparison.owner_mismatches.map(item => item.simulated.entity_name || item.simulated.key).join(", ")}</p>}
        </div>}
      </div>
    </CardBody>
  </Card>;
}
