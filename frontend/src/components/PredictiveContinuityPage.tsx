import { useCallback, useEffect, useMemo, useState } from "react";
import { describeError } from "../api";
import { graphApi } from "../company-graph/api";
import type { Tenant } from "../company-graph/types";
import { formatDate, formatDay } from "../format";
import { Spinner } from "../icons";
import { useLiveRefresh } from "../lib/useLiveRefresh";
import { forecastApi } from "../predictive-continuity/api";
import type {
  ForecastProposal,
  ForecastRecordView,
  ForecastReconciliation,
  MoneyAtRisk,
  ProposalStatus,
} from "../predictive-continuity/types";
import {
  HORIZONS,
  DEFAULT_HORIZON,
  allowedProposalActions,
  dueProcesses,
  filterRecordsByHorizon,
  findingLabel,
  formatMoneyTotals,
  latestPerRoot,
  proposalActionForFinding,
  proposalStatusLabel,
  reviewQueue,
  severityTone,
  shortHash,
  summarizeRecord,
  trustBadge,
} from "../predictive-continuity/predictiveContinuityView.mjs";
import { useFeedback } from "./Feedback";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, PageHeader, SelectInput, Skeleton, Stat } from "./ui";

type Tab = "forecasts" | "proposals" | "review";

type Confirm =
  | { kind: "decide"; proposal: ForecastProposal; decision: "approve" | "reject" }
  | { kind: "execute"; proposal: ForecastProposal }
  | { kind: "cancel"; proposal: ForecastProposal };

function proposalTone(status: ProposalStatus): "neutral" | "primary" | "success" | "warning" | "danger" {
  switch (status) {
    case "pending_approval":
      return "warning";
    case "approved":
      return "primary";
    case "dispatched":
      return "primary";
    case "verified":
      return "success";
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

/** Money the server redacted renders as hidden, never reconstructed. */
function moneyText(money: MoneyAtRisk | null, redacted: boolean): string {
  const text = formatMoneyTotals(money);
  if (text !== null) return text;
  return redacted ? "Money hidden — owner only" : "No money at risk";
}

function rootLabel(record: ForecastRecordView): string {
  const root = record.forecast.unavailable[0];
  return root ? `${root.name} (${record.root_kind})` : `${record.root_id} (${record.root_kind})`;
}

export function PredictiveContinuityPage() {
  const { toast } = useFeedback();
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [tab, setTab] = useState<Tab>("forecasts");
  const [horizon, setHorizon] = useState(DEFAULT_HORIZON);
  const [includeSuperseded, setIncludeSuperseded] = useState(false);
  const [records, setRecords] = useState<ForecastRecordView[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [proposals, setProposals] = useState<ForecastProposal[]>([]);
  const [proposalCounts, setProposalCounts] = useState<Record<string, number>>({});
  const [reconciliations, setReconciliations] = useState<ForecastReconciliation[]>([]);
  const [reconciliationCounts, setReconciliationCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [decideNote, setDecideNote] = useState("");

  useEffect(() => {
    let live = true;
    graphApi.tenants()
      .then(({ items }) => {
        if (!live) return;
        setTenants(items);
        setTenantId((current) => current || items[0]?.id || "");
      })
      .catch((reason) => { if (live) setError(describeError(reason)); });
    return () => { live = false; };
  }, []);

  const refresh = useCallback(async (silent = false) => {
    if (!tenantId) return;
    if (!silent) setLoading(true);
    try {
      const [forecastResponse, proposalResponse, proposalCountResponse, reconciliationResponse, reconciliationCountResponse] = await Promise.all([
        forecastApi.forecasts(tenantId, includeSuperseded),
        forecastApi.proposals(tenantId),
        forecastApi.proposalCounts(tenantId),
        forecastApi.reconciliations(tenantId),
        forecastApi.reconciliationCounts(tenantId),
      ]);
      setRecords(forecastResponse.forecasts);
      setProposals(proposalResponse.proposals);
      setProposalCounts(proposalCountResponse.counts);
      setReconciliations(reconciliationResponse.reconciliations);
      setReconciliationCounts(reconciliationCountResponse.counts);
      setError("");
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setLoading(false);
    }
  }, [tenantId, includeSuperseded]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { setSelectedId(null); }, [tenantId, horizon]);
  useLiveRefresh(() => refresh(true));

  const visibleRecords = useMemo(
    () => filterRecordsByHorizon(records, horizon, includeSuperseded),
    [records, horizon, includeSuperseded],
  );
  const standing = useMemo(
    () => latestPerRoot(filterRecordsByHorizon(records, horizon)),
    [records, horizon],
  );
  const selected = useMemo(
    () => records.find((record) => record.id === selectedId) ?? null,
    [records, selectedId],
  );
  const queue = useMemo(() => reviewQueue(reconciliations), [reconciliations]);
  const dueTotal = useMemo(
    () => visibleRecords.reduce((sum, record) => sum + (record.forecast.counts.processes_due_within_horizon ?? 0), 0),
    [visibleRecords],
  );

  async function runAction(key: string, work: () => Promise<string>) {
    if (busy) return;
    setBusy(key);
    setError("");
    try {
      toast("success", await work());
      await refresh(true);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy("");
    }
  }

  async function proposeFromRecord(record: ForecastRecordView, code: string, resourceId: string) {
    const actionKind = proposalActionForFinding("forecast", code);
    if (!actionKind || !tenantId) return;
    await runAction(`propose:${record.id}:${resourceId}`, async () => {
      const result = await forecastApi.createProposal(tenantId, {
        forecast_record_id: record.id,
        finding_code: code,
        resource_id: resourceId,
        action_kind: actionKind,
      });
      setTab("proposals");
      return result.created ? "Proposal created — waiting for approval" : "Proposal already exists for this finding";
    });
  }

  async function confirmAction() {
    if (!confirm || !tenantId) return;
    const proposal = confirm.proposal;
    if (confirm.kind === "decide") {
      const decision = confirm.decision;
      const note = decideNote;
      await runAction(`decide:${proposal.id}`, async () => {
        await forecastApi.decideProposal(tenantId, proposal.id, decision, proposal.action_hash, note);
        return decision === "approve" ? "Proposal approved — it can now be dispatched" : "Proposal rejected";
      });
    } else if (confirm.kind === "execute") {
      await runAction(`execute:${proposal.id}`, async () => {
        await forecastApi.executeProposal(tenantId, proposal.id);
        return "Proposal dispatched — exactly once, verified by read-back";
      });
    } else {
      await runAction(`cancel:${proposal.id}`, async () => {
        await forecastApi.cancelProposal(tenantId, proposal.id);
        return "Proposal cancelled";
      });
    }
    setConfirm(null);
    setDecideNote("");
  }

  async function acknowledge(item: ForecastReconciliation) {
    if (!tenantId) return;
    await runAction(`ack:${item.id}`, async () => {
      await forecastApi.acknowledgeReconciliation(tenantId, item.id);
      return "Report marked as seen";
    });
  }

  const supplierReview = selected
    && selected.root_kind === "supplier"
    && dueProcesses(selected.forecast).length > 0;

  return (
    <div className="grid gap-4">
      <PageHeader
        title="Predictive continuity"
        description="Forecasts that say in advance which processes break when someone, a system or a supplier is unavailable — advisory only. A forecast becomes an action only through an approval."
        actions={tenantId ? <Button variant="secondary" onClick={() => void refresh()} disabled={loading}>{loading ? <Spinner /> : "Refresh"}</Button> : undefined}
      />
      {error && <Alert tone="danger" title="Could not load forecasts">{error}</Alert>}
      {!loading && !tenants.length && !error && (
        <EmptyState title="No graph workspace is available" description="This account has no company graph tenant. Create or grant a tenant in Company intelligence setup before forecast data can appear." />
      )}
      {tenants.length > 1 && (
        <Card><CardBody className="flex flex-wrap items-center gap-3">
          <label className="text-sm font-medium" htmlFor="forecast-tenant">Tenant</label>
          <span className="min-w-56 flex-1">
            <SelectInput id="forecast-tenant" value={tenantId} onChange={(event) => setTenantId(event.target.value)}>
              {tenants.map((tenant) => <option key={tenant.id} value={tenant.id}>{tenant.name}</option>)}
            </SelectInput>
          </span>
        </CardBody></Card>
      )}
      {tenantId && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Forecast records" value={records.filter((record) => !record.superseded).length} hint={`${records.length} total`} />
            <Stat label={`Processes due in ${horizon} days`} value={dueTotal} hint="Across the records below" />
            <Stat label="Proposals waiting" value={proposalCounts.actionable ?? 0} hint={`${proposalCounts.pending_approval ?? 0} pending approval · ${proposalCounts.approved ?? 0} approved`} />
            <Stat label="Review queue" value={reconciliationCounts.unacknowledged ?? 0} hint={`${reconciliationCounts.unacknowledged_critical ?? 0} critical unacknowledged`} />
          </div>
          <div className="flex flex-wrap gap-2" role="tablist" aria-label="Predictive continuity sections">
            <Button variant={tab === "forecasts" ? "primary" : "secondary"} onClick={() => setTab("forecasts")}>Forecasts ({visibleRecords.length})</Button>
            <Button variant={tab === "proposals" ? "primary" : "secondary"} onClick={() => setTab("proposals")}>Proposals ({proposals.length})</Button>
            <Button variant={tab === "review" ? "primary" : "secondary"} onClick={() => setTab("review")}>Review queue ({queue.length})</Button>
          </div>

          {tab === "forecasts" && (
            <div className="grid gap-4">
              <Card>
                <CardHeader title="Horizon" description="The horizon changes the answer: deadlines and SLAs inside the window decide what is due." />
                <CardBody className="flex flex-wrap items-center gap-2">
                  {HORIZONS.map((days) => (
                    <Button key={days} variant={horizon === days ? "primary" : "secondary"} onClick={() => setHorizon(days)}>
                      {days} day{days === 1 ? "" : "s"}
                    </Button>
                  ))}
                  <label className="ml-auto flex items-center gap-2 text-sm text-muted">
                    <input type="checkbox" checked={includeSuperseded} onChange={(event) => setIncludeSuperseded(event.target.checked)} />
                    Show superseded records
                  </label>
                </CardBody>
              </Card>
              {loading && !records.length ? (
                <Skeleton className="h-64 w-full" />
              ) : !standing.length ? (
                <EmptyState title="No forecasts yet" description="No forecasts recorded for this tenant and horizon yet. Forecasts appear here after a standing scan runs." />
              ) : (
                <Card>
                  <CardHeader title="Standing forecast" description="The newest current forecast per unavailable root — what the twin predicts right now." />
                  <CardBody className="grid gap-3">
                    {standing.map((record) => {
                      const badge = trustBadge(record.trust);
                      return (
                        <button key={record.id} type="button" onClick={() => setSelectedId(record.id)} className="rounded-xl border border-border bg-surface p-4 text-left hover:border-primary">
                          <span className="flex flex-wrap items-center gap-2">
                            <span className="text-sm font-semibold">{rootLabel(record)}</span>
                            <Badge tone={badge.tone}>{badge.label}</Badge>
                            {record.superseded && <Badge tone="neutral">Superseded</Badge>}
                            <span className="ml-auto text-xs text-muted">{record.as_of ? formatDate(record.as_of) : ""}</span>
                          </span>
                          <span className="mt-1 block text-sm text-muted">{summarizeRecord(record)}</span>
                        </button>
                      );
                    })}
                  </CardBody>
                </Card>
              )}
              {selected && (
                <Card>
                  <CardHeader
                    title={`Forecast: ${rootLabel(selected)}`}
                    description={`${summarizeRecord(selected)} · as of ${selected.as_of ? formatDate(selected.as_of) : "unknown"} · fingerprint ${selected.fingerprint.slice(0, 12)}…`}
                    actions={<Button variant="secondary" onClick={() => setSelectedId(null)}>Close</Button>}
                  />
                  <CardBody className="grid gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                      {(() => { const badge = trustBadge(selected.trust); return <Badge tone={badge.tone}>{badge.label}</Badge>; })()}
                      {selected.superseded && <Badge tone="neutral">Superseded · drift {selected.drift_score ?? "unknown"}</Badge>}
                      <span className="text-sm text-muted">Money at risk: {moneyText(selected.forecast.money_at_risk, false)}</span>
                      <span className="text-sm text-muted">Due in horizon: {moneyText(selected.forecast.money_at_risk_due, false)}</span>
                    </div>
                    {supplierReview && (
                      <Alert tone="warning" title="Supplier single point of failure">
                        This supplier is unavailable and {dueProcesses(selected.forecast).length} process(es) are due within the horizon.
                        <span className="mt-2 block">
                          <Button
                            variant="secondary"
                            disabled={busy !== ""}
                            onClick={() => void proposeFromRecord(selected, "supplier_spof", selected.root_id)}
                          >
                            Propose supplier review
                          </Button>
                        </span>
                      </Alert>
                    )}
                    {!selected.forecast.processes.length ? (
                      <EmptyState title="No processes affected" description="This forecast names no affected processes." />
                    ) : (
                      <ol className="grid gap-3">
                        {selected.forecast.processes.map((process) => {
                          const due = process.due_status === "due_within_horizon";
                          const money = moneyText(process.money_at_risk, false);
                          return (
                            <li key={process.id} className="rounded-xl border border-border bg-surface p-4">
                              <div className="flex flex-wrap items-center gap-2">
                                <span className="text-sm font-semibold">{process.n}. {process.name}</span>
                                {due
                                  ? <Badge tone="warning">Due in horizon</Badge>
                                  : <Badge tone="neutral">{process.due_status === "at_risk_later" ? "Due later" : "No deadline"}</Badge>}
                                {process.reason && <Badge tone="danger">Uncovered</Badge>}
                                <span className="ml-auto text-xs text-muted">{process.due_at ? `due ${formatDay(process.due_at)}` : ""}</span>
                              </div>
                              <p className="mt-1 text-sm text-muted">
                                Owners: {process.owner_names.length ? process.owner_names.join(", ") : "—"}
                                {" · "}Dependencies: {process.dependencies.length ? process.dependencies.map((dep) => dep.name).join(", ") : "—"}
                                {" · "}Money at risk: {money}
                              </p>
                              <p className="mt-1 text-sm text-muted">
                                {process.reason
                                  ? `Uncovered: ${process.reason.replaceAll("_", " ")}.`
                                  : process.covered_by?.length
                                    ? `Covered by ${process.covered_by.map((person) => person.name).join(", ")}.`
                                    : "Covered."}
                                {" "}Live work: {process.live_work_items.length
                                  ? process.live_work_items.map((item) => `${item.name} (${item.kind}${item.status ? `, ${item.status}` : ""})`).join("; ")
                                  : "none linked."}
                              </p>
                              {process.reason && (
                                <div className="mt-2">
                                  <Button
                                    variant="secondary"
                                    disabled={busy !== ""}
                                    onClick={() => void proposeFromRecord(selected, "uncovered_process", process.id)}
                                  >
                                    Propose backup coverage
                                  </Button>
                                </div>
                              )}
                            </li>
                          );
                        })}
                      </ol>
                    )}
                  </CardBody>
                </Card>
              )}
              {!selected && visibleRecords.length > 0 && (
                <Card>
                  <CardHeader title="All records" description="Newest first. Pick a standing forecast above to see its numbered process list." />
                  <CardBody className="grid gap-2">
                    {visibleRecords.map((record) => {
                      const badge = trustBadge(record.trust);
                      return (
                        <button key={record.id} type="button" onClick={() => setSelectedId(record.id)} className="flex flex-wrap items-center gap-2 rounded-lg border border-border px-3 py-2 text-left text-sm hover:border-primary">
                          <span className="font-medium">{rootLabel(record)}</span>
                          <Badge tone={badge.tone}>{badge.label}</Badge>
                          {record.superseded && <Badge tone="neutral">Superseded</Badge>}
                          <span className="text-muted">{summarizeRecord(record)}</span>
                          <span className="ml-auto text-xs text-muted">{record.as_of ? formatDate(record.as_of) : ""}</span>
                        </button>
                      );
                    })}
                  </CardBody>
                </Card>
              )}
            </div>
          )}

          {tab === "proposals" && (
            <div className="grid gap-3">
              <Alert tone="info" title="Proposals never act on their own">
                A proposal is a suggestion until a human approves it. Approving binds the decision to the exact action shown; dispatching runs it exactly once through execute_once, verified by read-back.
              </Alert>
              {!proposals.length ? (
                <EmptyState title="No proposals yet" description="Proposals appear here when a forecast finding is proposed from a forecast record." />
              ) : proposals.map((proposal) => {
                const actions = allowedProposalActions(proposal.status);
                return (
                  <Card key={proposal.id}>
                    <CardHeader
                      title={`${findingLabel(proposal.finding.code)} — ${proposal.finding.detail}`}
                      description={`${proposal.action_type} · approver: ${proposal.approver_class ?? proposal.required_permission} · proposed ${formatDate(proposal.created_at)}`}
                      actions={<Badge tone={proposalTone(proposal.status)}>{proposalStatusLabel(proposal.status)}</Badge>}
                    />
                    <CardBody className="grid gap-2">
                      <p className="text-sm text-muted">
                        Severity: <Badge tone={severityTone(proposal.finding.severity)}>{proposal.finding.severity}</Badge>
                        {" · "}Money at risk: {moneyText(proposal.finding.money, proposal.money_redacted)}
                        {" · "}Action hash: <code>{shortHash(proposal.action_hash)}</code>
                      </p>
                      <div className="flex flex-wrap gap-2">
                        {actions.canDecide && (
                          <>
                            <Button variant="primary" disabled={busy !== ""} onClick={() => { setConfirm({ kind: "decide", proposal, decision: "approve" }); setDecideNote(""); }}>Approve</Button>
                            <Button variant="secondary" disabled={busy !== ""} onClick={() => { setConfirm({ kind: "decide", proposal, decision: "reject" }); setDecideNote(""); }}>Reject</Button>
                          </>
                        )}
                        {actions.canExecute && (
                          <Button variant="primary" disabled={busy !== ""} onClick={() => setConfirm({ kind: "execute", proposal })}>Dispatch</Button>
                        )}
                        {actions.canCancel && (
                          <Button variant="secondary" disabled={busy !== ""} onClick={() => setConfirm({ kind: "cancel", proposal })}>Cancel</Button>
                        )}
                        {!actions.canDecide && !actions.canExecute && !actions.canCancel && (
                          <span className="text-sm text-muted">This proposal is {proposalStatusLabel(proposal.status).toLowerCase()} — no further action is available.</span>
                        )}
                      </div>
                    </CardBody>
                  </Card>
                );
              })}
              {confirm && (
                <Card>
                  <CardHeader
                    title={
                      confirm.kind === "decide"
                        ? confirm.decision === "approve" ? "Approve this proposal?" : "Reject this proposal?"
                        : confirm.kind === "execute" ? "Dispatch this approved proposal?" : "Cancel this proposal?"
                    }
                    description={
                      confirm.kind === "decide"
                        ? `The decision applies only to the exact action ${shortHash(confirm.proposal.action_hash)} — nothing else.`
                        : confirm.kind === "execute"
                          ? "Dispatch runs the action exactly once through execute_once, verified by read-back. A re-dispatch replays the stored result."
                          : "Cancelling also cancels the pending approval. A cancelled proposal cannot be re-proposed."
                    }
                  />
                  <CardBody className="grid gap-3">
                    {confirm.kind === "decide" && (
                      <label className="grid gap-1 text-sm font-medium">
                        Note (optional)
                        <input className="rounded-lg border border-border bg-surface px-3 py-2 text-sm" value={decideNote} onChange={(event) => setDecideNote(event.target.value)} maxLength={500} placeholder="Why this decision" />
                      </label>
                    )}
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="primary"
                        disabled={busy !== ""}
                        onClick={() => void confirmAction()}
                      >
                        {confirm.kind === "decide" ? (confirm.decision === "approve" ? "Confirm approval" : "Confirm rejection") : confirm.kind === "execute" ? "Confirm dispatch" : "Confirm cancel"}
                      </Button>
                      <Button variant="secondary" disabled={busy !== ""} onClick={() => { setConfirm(null); setDecideNote(""); }}>Back</Button>
                    </div>
                  </CardBody>
                </Card>
              )}
            </div>
          )}

          {tab === "review" && (
            <div className="grid gap-3">
              <Alert tone="info" title="Drill reconciliation review queue">
                Each report compares a stored forecast with a real continuity drill. Acknowledging marks it as seen — the report, the forecast and the drill record are never rewritten.
              </Alert>
              {!queue.length && !reconciliations.length ? (
                <EmptyState title="No reconciliations yet" description="Reconciliation reports appear here after a forecast is compared with a drill run." />
              ) : (
                <>
                  {!queue.length && (
                    <EmptyState title="Queue clear" description="Every reconciliation report has been acknowledged." />
                  )}
                  {queue.map((item) => (
                    <Card key={item.id}>
                      <CardHeader
                        title={item.matches ? "Drill matched the forecast" : `Drill differed — ${item.findings.length} finding(s)`}
                        description={`${item.drill_kind ? `${item.drill_kind} · ` : ""}drill ${item.drill_run_id.slice(0, 8)}… · ${formatDate(item.created_at)}${item.adjusted_drift_score !== null ? ` · forecast drift now ${item.adjusted_drift_score}` : ""}`}
                        actions={<Badge tone={severityTone(item.worst_severity)}>{item.worst_severity}</Badge>}
                      />
                      <CardBody className="grid gap-2">
                        {item.findings.map((finding, index) => (
                          <p key={`${finding.fingerprint}-${index}`} className="text-sm text-muted">
                            <Badge tone={severityTone(finding.severity)}>{finding.severity}</Badge>{" "}
                            <strong className="text-foreground">{findingLabel(finding.code)}</strong> — {finding.detail}
                            {" · "}Money: {moneyText(finding.money, item.money_redacted)}
                          </p>
                        ))}
                        <div>
                          <Button variant="secondary" disabled={busy !== ""} onClick={() => void acknowledge(item)}>Mark as seen</Button>
                        </div>
                      </CardBody>
                    </Card>
                  ))}
                  {reconciliations.some((item) => item.acknowledged) && (
                    <Card>
                      <CardHeader title="Acknowledged" description="Reports already reviewed." />
                      <CardBody className="grid gap-2">
                        {reconciliations.filter((item) => item.acknowledged).map((item) => (
                          <p key={item.id} className="text-sm text-muted">
                            {item.matches ? "Matched" : "Differed"} · {item.drill_kind ?? "drill"} · {item.acknowledged_at ? formatDate(item.acknowledged_at) : ""}
                          </p>
                        ))}
                      </CardBody>
                    </Card>
                  )}
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
