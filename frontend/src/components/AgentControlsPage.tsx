import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { AgentControl, AgentControlEvent, AgentControlSpend } from "../types";
import { attentionCount, formatUsd, needsAttention, sortControls, spendPercent, spendStateLabel, statusLabel } from "../agent-controls/agentControlsView.mjs";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Dialog, EmptyState, Field, Input, PageHeader, Skeleton, Textarea } from "./ui";
import { useLiveRefresh } from "../lib/useLiveRefresh";
import { Ban, RefreshCw, ShieldCheck, Skull } from "lucide-react";

/**
 * Step AK (slice 4): admin page for per-agent controls.
 *
 * Lists every control record (status, spend used/limit with warn/block state),
 * shows one agent's spend report (recent ledger rows) and audit trail, and
 * offers the controls themselves: pause / resume / disable, edit the spend
 * limit, and the terminal kill-switch. Every change needs a reason — the
 * backend requires it and writes it into the audit event. A killed agent
 * cannot be revived from this page: the backend refuses it, and only an
 * administrator creating a new control record brings an agent back.
 *
 * Reads need `agents.read` (or `agents.manage`); changes need `agents.manage`
 * on the organisation scope. The backend enforces both independently — this
 * page only renders what the API returns.
 */

function StatusBadge({ status }: { status: AgentControl["status"] }) {
  const tone = status === "active" ? "success" : status === "killed" ? "danger" : "warning";
  return <Badge tone={tone}>{statusLabel(status)}</Badge>;
}

function SpendBar({ control }: { control: AgentControl }) {
  const percent = spendPercent(control.spend_used_usd, control.spend_limit_usd);
  if (percent === null) return <span className="text-xs text-muted">No limit set</span>;
  const tone = control.spend_state === "at_limit" || control.spend_state === "over_limit"
    ? "bg-danger" : control.spend_state === "near_limit" ? "bg-warning" : "bg-success";
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 min-w-24 flex-1 overflow-hidden rounded-full bg-border" role="progressbar" aria-valuenow={Math.round(percent)} aria-valuemin={0} aria-valuemax={100} aria-label="Spend used">
        <div className={`h-full ${tone}`} style={{ width: `${percent}%` }} />
      </div>
      <span className="text-xs text-muted">{Math.round(percent)}%</span>
    </div>
  );
}

export function AgentControlsPage() {
  const [controls, setControls] = useState<AgentControl[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [spend, setSpend] = useState<AgentControlSpend | null>(null);
  const [events, setEvents] = useState<AgentControlEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [killOpen, setKillOpen] = useState(false);
  const [killReason, setKillReason] = useState("");
  const [limitOpen, setLimitOpen] = useState(false);
  const [limitValue, setLimitValue] = useState("");
  const [limitReason, setLimitReason] = useState("");
  const [statusReason, setStatusReason] = useState("");

  const selected = useMemo(
    () => controls.find((control) => control.agent_id === selectedId) ?? null,
    [controls, selectedId],
  );
  const attention = attentionCount(controls);
  const sorted = useMemo(() => sortControls(controls), [controls]);

  async function refresh() {
    try {
      const data = await api.agentControls();
      setControls(data.controls);
      setError(null);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setLoading(false);
    }
  }

  async function refreshDetail(agentId: string) {
    setDetailLoading(true);
    try {
      const [spendData, eventData] = await Promise.all([api.agentControlSpend(agentId), api.agentControlEvents(agentId)]);
      setSpend(spendData);
      setEvents(eventData.events);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setDetailLoading(false);
    }
  }

  useEffect(() => { void refresh(); }, []);
  useLiveRefresh(refresh);

  useEffect(() => {
    if (!selectedId) { setSpend(null); setEvents([]); return; }
    void refreshDetail(selectedId);
  }, [selectedId]);

  async function changeStatus(next: "active" | "paused" | "disabled") {
    if (!selected || !statusReason.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await api.patchAgentControl(selected.agent_id, { reason: statusReason.trim(), status: next });
      setControls((current) => current.map((control) => control.agent_id === updated.agent_id ? updated : control));
      setNotice(`Agent ${next === "active" ? "resumed" : next}. The change is in the audit trail.`);
      setStatusReason("");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function saveLimit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !limitReason.trim()) return;
    const value = limitValue.trim();
    if (value !== "" && !/^\d+(\.\d{1,4})?$/.test(value)) {
      setError("Spend limit must be a non-negative amount with at most 4 decimal places, or empty to remove the limit.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const updated = await api.patchAgentControl(selected.agent_id, {
        reason: limitReason.trim(),
        spend_limit_usd: value === "" ? null : value,
      });
      setControls((current) => current.map((control) => control.agent_id === updated.agent_id ? updated : control));
      setNotice(value === "" ? "Spend limit removed." : `Spend limit set to ${formatUsd(updated.spend_limit_usd)}.`);
      setLimitOpen(false);
      setLimitReason("");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  async function kill(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !killReason.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await api.killAgentControl(selected.agent_id, killReason.trim());
      setControls((current) => current.map((control) => control.agent_id === updated.agent_id ? updated : control));
      setNotice(`Agent killed. ${updated.cancelled_work ? "Queued work was cancelled." : ""} This cannot be undone from here.`);
      setKillOpen(false);
      setKillReason("");
      void refreshDetail(selected.agent_id);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Agent controls"
        description="Per-agent data access, permissions, spend limits and the kill-switch. An agent can never widen its own access; only administrators with agents.manage change these records."
        actions={<Button onClick={() => void refresh()}><RefreshCw /> Refresh</Button>}
      />
      {error && <Alert tone="danger" title="Couldn't load agent controls">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      {attention > 0 && (
        <Alert tone="warning" title={`${attention} agent${attention === 1 ? "" : "s"} need${attention === 1 ? "s" : ""} attention`}>
          Killed, paused, disabled, or at/over the spend limit. Open the agent below to act.
        </Alert>
      )}
      {loading ? (
        <div className="grid gap-3"><Skeleton className="h-8 w-60" /><Skeleton className="h-64 w-full" /></div>
      ) : controls.length === 0 ? (
        <EmptyState
          icon={<ShieldCheck className="size-6" />}
          title="No agent control records yet"
          description="Create a control record for an agent from the API, or wait for one to be registered. Until a record exists, an agent keeps its previous behaviour."
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
          <Card>
            <CardHeader title="Controlled agents" description={`${controls.length} record${controls.length === 1 ? "" : "s"} · attention first`} />
            <CardBody>
              <div className="grid gap-2">
                {sorted.map((control) => (
                  <button
                    key={control.agent_id}
                    type="button"
                    onClick={() => { setSelectedId(control.agent_id); setNotice(null); }}
                    className={`rounded-lg border p-3 text-left ${selectedId === control.agent_id ? "border-primary bg-primary-soft" : "border-border hover:bg-surface-2"}`}
                    aria-pressed={selectedId === control.agent_id}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-xs font-semibold">{control.agent_id.slice(0, 8)}…</span>
                      <StatusBadge status={control.status} />
                    </div>
                    <p className="mt-1 text-xs text-muted">{control.agent_kind} · v{control.version}</p>
                    <div className="mt-2">
                      <p className="text-xs">
                        {formatUsd(control.spend_used_usd)} used{control.spend_limit_usd ? ` of ${formatUsd(control.spend_limit_usd)}` : ""} · {spendStateLabel(control.spend_state)}
                      </p>
                      <div className="mt-1"><SpendBar control={control} /></div>
                    </div>
                    {needsAttention(control) && <p className="mt-2 text-xs font-semibold text-warning-text">Needs attention</p>}
                  </button>
                ))}
              </div>
            </CardBody>
          </Card>

          {selected ? (
            <div className="grid content-start gap-4">
              <Card>
                <CardHeader
                  title={`Agent ${selected.agent_id.slice(0, 8)}…`}
                  description={`${selected.agent_kind} · status ${statusLabel(selected.status)} · record v${selected.version}`}
                />
                <CardBody>
                  {selected.status === "killed" && (
                    <Alert tone="danger" title="Killed — terminal" className="mb-3">
                      Killed {selected.killed_at ? formatDate(selected.killed_at) : ""}{selected.kill_reason ? ` — ${selected.kill_reason}` : ""}. This agent stays dead until an administrator creates a new control record for it.
                    </Alert>
                  )}
                  <dl className="grid gap-2 text-[13px]">
                    <div className="flex justify-between gap-2"><dt className="text-muted">Spend</dt><dd>{formatUsd(selected.spend_used_usd)} used · {formatUsd(selected.spend_limit_usd)} limit · {spendStateLabel(selected.spend_state)}</dd></div>
                    <div className="flex justify-between gap-2"><dt className="text-muted">Data scopes</dt><dd>{selected.data_scopes.length ? selected.data_scopes.join(", ") : "None — reads nothing"}</dd></div>
                    <div className="flex justify-between gap-2"><dt className="text-muted">Permissions</dt><dd>{selected.permissions.length ? selected.permissions.join(", ") : "None"}</dd></div>
                    <div className="flex justify-between gap-2"><dt className="text-muted">Allowed actions</dt><dd>{selected.allowed_actions.length ? selected.allowed_actions.join(", ") : "None — requests nothing"}</dd></div>
                    <div className="flex justify-between gap-2"><dt className="text-muted">Max steps</dt><dd>{selected.max_steps} · delegates to {selected.delegate_to.length} agent{selected.delegate_to.length === 1 ? "" : "s"} (depth {selected.max_delegation_depth})</dd></div>
                  </dl>
                  {selected.status !== "killed" && (
                    <div className="mt-4 grid gap-2">
                      <Field label="Reason for the status change (required, written to the audit trail)">
                        <Input value={statusReason} onChange={(event) => setStatusReason(event.target.value)} placeholder="e.g. Pausing while the finance review runs" />
                      </Field>
                      <div className="flex flex-wrap gap-2">
                        {selected.status !== "paused" && <Button disabled={busy || !statusReason.trim()} onClick={() => void changeStatus("paused")}><Ban /> Pause</Button>}
                        {selected.status !== "disabled" && <Button disabled={busy || !statusReason.trim()} onClick={() => void changeStatus("disabled")}><Ban /> Disable</Button>}
                        {selected.status !== "active" && <Button variant="primary" disabled={busy || !statusReason.trim()} onClick={() => void changeStatus("active")}>Resume</Button>}
                        <Button variant="soft" disabled={busy} onClick={() => { setLimitValue(selected.spend_limit_usd ?? ""); setLimitOpen(true); }}>Edit spend limit</Button>
                        <Button variant="danger" disabled={busy} onClick={() => setKillOpen(true)}><Skull /> Kill agent</Button>
                      </div>
                    </div>
                  )}
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Spend report" description="Recent costed calls from the append-only spend ledger. Money is Decimal — never a float." />
                <CardBody>
                  {detailLoading && !spend ? <Skeleton className="h-24 w-full" /> : !spend || spend.recent.length === 0 ? (
                    <p className="text-xs text-muted">No costed calls recorded for this agent yet.</p>
                  ) : (
                    <table className="w-full text-left text-[13px]">
                      <thead><tr className="text-muted"><th className="py-1 pr-2 font-medium">Provider / model</th><th className="py-1 pr-2 font-medium">Tokens</th><th className="py-1 pr-2 font-medium">Cost</th><th className="py-1 font-medium">Recorded</th></tr></thead>
                      <tbody>
                        {spend.recent.map((entry) => (
                          <tr key={entry.id} className="border-t border-border">
                            <td className="py-1 pr-2">{entry.provider || "—"} / {entry.model || "—"}</td>
                            <td className="py-1 pr-2">{entry.tokens}</td>
                            <td className="py-1 pr-2">{formatUsd(entry.cost_usd)}</td>
                            <td className="py-1">{entry.recorded_at ? formatDate(entry.recorded_at) : "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Audit trail" description="Every control change and every denied or approval-gated decision that matters." />
                <CardBody>
                  {detailLoading && events.length === 0 ? <Skeleton className="h-24 w-full" /> : events.length === 0 ? (
                    <p className="text-xs text-muted">No audit events for this agent yet.</p>
                  ) : (
                    <ul className="grid gap-2">
                      {events.map((event) => (
                        <li key={event.id} className="rounded-md border border-border px-3 py-2 text-[13px]">
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-semibold">{event.event}</span>
                            <span className="text-xs text-muted">{event.created_at ? formatDate(event.created_at) : ""}</span>
                          </div>
                          {event.reason && <p className="mt-0.5 text-muted">{event.reason}</p>}
                        </li>
                      ))}
                    </ul>
                  )}
                </CardBody>
              </Card>
            </div>
          ) : (
            <Card><CardBody><p className="text-[13px] text-muted">Select an agent on the left to see its spend report, audit trail and controls.</p></CardBody></Card>
          )}
        </div>
      )}

      <Dialog
        open={killOpen}
        onOpenChange={(open) => { if (!open) { setKillOpen(false); setKillReason(""); } }}
        title="Kill this agent?"
        description="Terminal. The agent is refused everywhere at once — login, dispatch and governance proposals — and its queued work is cancelled. It can only come back when an administrator creates a new control record."
        footer={<>
          <Button onClick={() => { setKillOpen(false); setKillReason(""); }}>Cancel</Button>
          <Button variant="danger" loading={busy} disabled={busy || !killReason.trim()} onClick={() => (document.getElementById("agent-controls-kill-form") as HTMLFormElement | null)?.requestSubmit()}><Skull /> Kill agent</Button>
        </>}
      >
        <form id="agent-controls-kill-form" className="grid gap-4" onSubmit={(event) => void kill(event)}>
          <Field label="Reason (required, written to the audit trail)">
            <Textarea required value={killReason} onChange={(event) => setKillReason(event.target.value)} placeholder="e.g. Agent was spending past its limit on finance data" />
          </Field>
        </form>
      </Dialog>

      <Dialog
        open={limitOpen}
        onOpenChange={(open) => { if (!open) { setLimitOpen(false); setLimitReason(""); } }}
        title="Edit spend limit"
        description="Leave empty to remove the limit (the agent then has no spend cap — use only for owner-created agents)."
        footer={<>
          <Button onClick={() => { setLimitOpen(false); setLimitReason(""); }}>Cancel</Button>
          <Button variant="primary" loading={busy} disabled={busy || !limitReason.trim()} onClick={() => (document.getElementById("agent-controls-limit-form") as HTMLFormElement | null)?.requestSubmit()}>Save limit</Button>
        </>}
      >
        <form id="agent-controls-limit-form" className="grid gap-4" onSubmit={(event) => void saveLimit(event)}>
          <Field label="Spend limit (USD)">
            <Input value={limitValue} onChange={(event) => setLimitValue(event.target.value)} placeholder="e.g. 10.0000" inputMode="decimal" />
          </Field>
          <Field label="Reason (required, written to the audit trail)">
            <Textarea required value={limitReason} onChange={(event) => setLimitReason(event.target.value)} placeholder="e.g. Raising the monthly cap for the research agent" />
          </Field>
        </form>
      </Dialog>
    </>
  );
}
