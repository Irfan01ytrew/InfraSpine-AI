import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api";
import { Alert, Badge, Button, Card, CardBody, CardHeader, PageHeader, Skeleton } from "../components/ui";
import {
  filingOutcomeLabel,
  incidentKindLabel,
  isAccessError,
  severityTone,
  sortEscalations,
  statusTone,
} from "./selfHealing.mjs";
import { sh, type ContinuityOption, type FilingResult, type SelfHealingEscalation, type SelfHealingIncident, type SelfHealingOverview, type SelfHealingTestRun } from "./api";

function formatTime(iso: string) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

export function SelfHealingPage() {
  const [overview, setOverview] = useState<SelfHealingOverview | null>(null);
  const [incidents, setIncidents] = useState<SelfHealingIncident[]>([]);
  const [escalations, setEscalations] = useState<SelfHealingEscalation[]>([]);
  const [testRuns, setTestRuns] = useState<SelfHealingTestRun[]>([]);
  const [options, setOptions] = useState<ContinuityOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [accessDenied, setAccessDenied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filingTenant, setFilingTenant] = useState<Record<string, string>>({});
  const [filingBusy, setFilingBusy] = useState<Record<string, boolean>>({});
  const [filingResult, setFilingResult] = useState<Record<string, FilingResult | string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setAccessDenied(false);
    try {
      const [ov, inc, esc, runs, opts] = await Promise.all([
        sh.overview(),
        sh.incidents(),
        sh.escalations(),
        sh.testRuns(),
        sh.continuityOptions(),
      ]);
      setOverview(ov);
      setIncidents(inc.items);
      setEscalations(esc.items);
      setTestRuns(runs.items);
      setOptions(opts.items);
    } catch (err) {
      if (err instanceof ApiError && isAccessError(err.status)) {
        setAccessDenied(true);
      } else {
        setError(err instanceof Error ? err.message : "Could not load self-healing data.");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function acknowledge(key: string) {
    try {
      const row = await sh.acknowledge(key);
      setEscalations((items) => items.map((item) => (item.incident_key === key ? row : item)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not acknowledge the escalation.");
    }
  }

  async function fileEvent(incident: SelfHealingIncident) {
    const tenantId = filingTenant[incident.key];
    if (!tenantId) return;
    setFilingBusy((busy) => ({ ...busy, [incident.key]: true }));
    try {
      const result = await sh.fileEvent(tenantId, incident.key);
      setFilingResult((results) => ({ ...results, [incident.key]: result }));
    } catch (err) {
      setFilingResult((results) => ({
        ...results,
        [incident.key]: err instanceof Error ? err.message : "Filing failed.",
      }));
    } finally {
      setFilingBusy((busy) => ({ ...busy, [incident.key]: false }));
    }
  }

  if (loading) {
    return (
      <div className="grid gap-3">
        <Skeleton className="h-8 w-60" />
        <Skeleton className="h-4 w-96" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (accessDenied) {
    return (
      <div>
        <PageHeader
          eyebrow="Operations · Self-healing"
          title="Self-healing"
          description="Automatic fault detection, verified remedies, and escalation to the owner."
        />
        <Alert tone="info" title="Self-healing is not available to this account">
          This page needs the self_healing.read grant on the organisation. Ask an administrator to grant it.
        </Alert>
      </div>
    );
  }

  const latestRun = testRuns[0];
  const probeEntries = Object.entries(overview?.latest_probes ?? {});

  return (
    <div className="grid gap-4">
      <PageHeader
        eyebrow="Operations · Self-healing"
        title="Self-healing"
        description="Automatic fault detection, verified remedies, and escalation to the owner. Remedies only run when verified; everything else escalates."
        actions={<Button variant="secondary" onClick={() => void load()}>Refresh</Button>}
      />
      {error && <Alert tone="danger" title="Something went wrong">{error}</Alert>}

      <div className="grid gap-4 sm:grid-cols-3">
        <Card><CardBody>
          <p className="text-sm text-muted">Open incidents</p>
          <p className="mt-1 text-[22px] font-semibold">{overview?.open_incident_count ?? 0}</p>
          <p className="text-xs text-muted">
            Worst open severity: {overview?.incidents.worst ?? "none"}
          </p>
        </CardBody></Card>
        <Card><CardBody>
          <p className="text-sm text-muted">Unacknowledged escalations</p>
          <p className="mt-1 text-[22px] font-semibold">{overview?.unacknowledged_escalations ?? 0}</p>
          <p className="text-xs text-muted">Escalations wait in the admin UI until a human acknowledges them.</p>
        </CardBody></Card>
        <Card><CardBody>
          <p className="text-sm text-muted">Latest self-test run</p>
          <p className="mt-1 text-[22px] font-semibold">{latestRun ? `${latestRun.suite} · ${latestRun.status}` : "none yet"}</p>
          <p className="text-xs text-muted">
            {latestRun ? `${latestRun.passed} passed, ${latestRun.failed} failed · ${formatTime(latestRun.started_at)}` : "The auto-test loop has not run yet."}
          </p>
        </CardBody></Card>
      </div>

      <Card>
        <CardHeader title="Latest probes" description="The newest health snapshot per component." />
        <CardBody className="grid gap-2">
          {probeEntries.length ? probeEntries.map(([component, probe]) => (
            <div key={component} className="flex items-center justify-between gap-3 rounded-lg border border-border p-3">
              <span className="min-w-0 truncate text-sm font-semibold">{component}</span>
              <span className="flex items-center gap-2">
                <span className="text-xs text-muted">{probe.age_seconds.toFixed(0)}s old</span>
                <Badge tone={probe.status === "ok" ? "success" : probe.status === "degraded" ? "warning" : "danger"}>{probe.status}</Badge>
              </span>
            </div>
          )) : <p className="text-sm text-muted">No probes recorded yet.</p>}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Incidents" description="One row per fault. Filing a recovery event is an explicit human act — nothing here files by itself." />
        <CardBody className="grid gap-2">
          {incidents.length ? incidents.map((incident) => {
            const result = filingResult[incident.key];
            return (
              <div key={incident.key} className="rounded-lg border border-border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold">{incidentKindLabel(incident.kind)} · {incident.summary}</p>
                    <p className="text-xs text-muted">
                      {incident.component} · {incident.attempts} remed{incident.attempts === 1 ? "y" : "ies"} tried · {formatTime(incident.updated_at)}
                    </p>
                  </div>
                  <span className="flex items-center gap-2">
                    <Badge tone={severityTone(incident.severity)}>{incident.severity}</Badge>
                    <Badge tone={statusTone(incident.status)}>{incident.status}</Badge>
                  </span>
                </div>
                {options.length > 0 && incident.status !== "remedied" && (
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <select
                      className="rounded-lg border border-border bg-surface px-2 py-1 text-sm"
                      value={filingTenant[incident.key] ?? ""}
                      onChange={(event) => setFilingTenant((tenants) => ({ ...tenants, [incident.key]: event.target.value }))}
                      aria-label={`Recovery workspace for ${incident.key}`}
                    >
                      <option value="">File as a recovery event in…</option>
                      {options.map((option) => (
                        <option key={option.tenant_id} value={option.tenant_id}>{option.tenant_name}</option>
                      ))}
                    </select>
                    <Button
                      variant="secondary"
                      disabled={!filingTenant[incident.key] || filingBusy[incident.key]}
                      onClick={() => void fileEvent(incident)}
                    >
                      {filingBusy[incident.key] ? "Filing…" : "File recovery event"}
                    </Button>
                    {typeof result === "string" ? (
                      <span className="text-sm text-danger-text">{result}</span>
                    ) : result ? (
                      <span className="text-sm text-muted">{filingOutcomeLabel(result.outcome)}{result.duplicate ? " (duplicate)" : ""}</span>
                    ) : null}
                  </div>
                )}
              </div>
            );
          }) : <p className="text-sm text-muted">No incidents recorded.</p>}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Escalations" description="Faults the owner was told about. Acknowledging records that a human has seen it." />
        <CardBody className="grid gap-2">
          {escalations.length ? sortEscalations(escalations).map((escalation) => (
            <div key={escalation.incident_key} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold">{incidentKindLabel(escalation.kind)} · {escalation.summary}</p>
                <p className="text-xs text-muted">Delivered {formatTime(escalation.delivered_at)} · {escalation.channel}</p>
              </div>
              <span className="flex items-center gap-2">
                <Badge tone={severityTone(escalation.severity)}>{escalation.severity}</Badge>
                {escalation.acknowledged_at ? (
                  <Badge tone="success">Acknowledged</Badge>
                ) : (
                  <Button variant="secondary" onClick={() => void acknowledge(escalation.incident_key)}>Acknowledge</Button>
                )}
              </span>
            </div>
          )) : <p className="text-sm text-muted">No escalations.</p>}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Self-test runs" description="The auto-test loop runs the suite on a throwaway database and escalates failures. A failing test never triggers a remedy." />
        <CardBody className="grid gap-2">
          {testRuns.length ? testRuns.map((run) => (
            <div key={run.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold">{run.suite} suite · {run.status}</p>
                <p className="text-xs text-muted">
                  {run.passed} passed, {run.failed} failed, {run.errors} errors · {run.duration_seconds.toFixed(1)}s · {formatTime(run.started_at)}
                </p>
                {run.failure_names.length > 0 && (
                  <p className="truncate text-xs text-muted">{run.failure_names.slice(0, 3).join(", ")}</p>
                )}
              </div>
              <Badge tone={run.status === "passed" ? "success" : run.status === "failed" ? "danger" : "warning"}>{run.status}</Badge>
            </div>
          )) : <p className="text-sm text-muted">No self-test runs yet.</p>}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Continuity options" description="Workspaces with an approved or in-review plan. Activation stays a human decision in Business continuity — nothing here activates a plan." />
        <CardBody className="grid gap-2">
          {options.length ? options.map((option) => (
            <div key={option.tenant_id} className="rounded-lg border border-border p-3">
              <p className="text-sm font-semibold">{option.tenant_name} <span className="font-normal text-muted">· {option.role}</span></p>
              <p className="text-xs text-muted">{option.plans.map((plan) => `${plan.title} (v${plan.version}, ${plan.status})`).join(" · ")}</p>
            </div>
          )) : <p className="text-sm text-muted">No continuity workspace with an activatable plan.</p>}
        </CardBody>
      </Card>
    </div>
  );
}
