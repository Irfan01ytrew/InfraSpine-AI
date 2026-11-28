import { useCallback, useEffect, useState } from "react";
import { Activity, AlertTriangle, Bell, Gauge, RefreshCw, Wallet } from "lucide-react";
import { describeError } from "../api";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, PageHeader, SelectInput, Skeleton, Stat } from "../components/ui";
import { spendSeries } from "../finance/financeView.mjs";
import { controlRoomApi, type AlertConfig, type AlertDelivery, type ControlRoomCharts, type Scope } from "./api";
import { barPercent, relativeAge, statusBars } from "./chartData.mjs";
import { buildConfigPayload, recipientError } from "./controlRoomConfig.mjs";

const POLL_MS = 30_000;
const PALETTE = ["bg-primary", "bg-success", "bg-warning", "bg-danger", "bg-foreground-2"];
const RUN_TONE: Record<string, "neutral" | "primary" | "success" | "warning" | "danger"> = {
  completed: "success",
  failed: "danger",
  running: "primary",
  queued: "warning",
  paused: "warning",
  cancelled: "neutral",
  compensating: "warning",
  compensated: "neutral",
};

function timeOf(iso: string) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}

export function ControlRoomPage() {
  const [scopes, setScopes] = useState<Scope[] | null>(null);
  const [group, setGroup] = useState("");
  const [data, setData] = useState<ControlRoomCharts | null>(null);
  const [currency, setCurrency] = useState("");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [config, setConfig] = useState<AlertConfig | null>(null);
  const [deliveries, setDeliveries] = useState<AlertDelivery[] | null>(null);
  const [configError, setConfigError] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState("");
  const [formEnabled, setFormEnabled] = useState(true);
  const [quietStart, setQuietStart] = useState("22:00");
  const [quietEnd, setQuietEnd] = useState("07:00");
  const [recipients, setRecipients] = useState<{ channel: "whatsapp" | "email"; address: string }[]>([]);
  const [newChannel, setNewChannel] = useState<"whatsapp" | "email">("whatsapp");
  const [newAddress, setNewAddress] = useState("");
  const [newRecipientError, setNewRecipientError] = useState("");
  const [ruleRows, setRuleRows] = useState<AlertConfig["effective_rules"]>([]);

  useEffect(() => {
    controlRoomApi.scopes().then((result) => {
      setScopes(result.scopes);
      setGroup((current) => current || result.scopes[0]?.id || "");
    }).catch((err) => setError(describeError(err)));
  }, []);

  const load = useCallback(() => {
    if (!group) return;
    controlRoomApi.charts(group)
      .then((result) => {
        setData(result);
        setCurrency((cur) => cur || result.spend.currencies[0] || "");
        setError("");
      })
      .catch((err) => setError(describeError(err)));
  }, [group]);

  useEffect(() => {
    setData(null);
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, revision]);

  const scope = scopes?.find((item) => item.id === group);

  const loadAlerts = useCallback(() => {
    if (!group) return;
    controlRoomApi.deliveries(group)
      .then((result) => setDeliveries(result.deliveries))
      .catch(() => setDeliveries([]));
    if (!scope?.can_manage) {
      setConfig(null);
      return;
    }
    controlRoomApi.config(group)
      .then((result) => {
        setConfig(result);
        setFormEnabled(result.enabled);
        setQuietStart(result.quiet_start);
        setQuietEnd(result.quiet_end);
        setRecipients(result.recipients);
        setRuleRows(result.effective_rules);
        setConfigError("");
      })
      .catch((err) => setConfigError(describeError(err)));
  }, [group, scope?.can_manage]);

  useEffect(() => {
    loadAlerts();
  }, [loadAlerts, revision]);

  const updateRule = (key: string, patch: Partial<AlertConfig["effective_rules"][number]>) =>
    setRuleRows((rows) => rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));

  const addRecipient = () => {
    const problem = recipientError(newChannel, newAddress);
    if (problem) {
      setNewRecipientError(problem);
      return;
    }
    const address = newAddress.trim();
    if (recipients.some((item) => item.channel === newChannel && item.address === address)) {
      setNewRecipientError("That recipient is already listed.");
      return;
    }
    setRecipients((current) => [...current, { channel: newChannel, address }]);
    setNewAddress("");
    setNewRecipientError("");
  };

  const saveConfig = () => {
    if (!group) return;
    setSaving(true);
    setConfigError("");
    setSavedAt("");
    controlRoomApi
      .updateConfig(group, buildConfigPayload({
        enabled: formEnabled,
        quietStart,
        quietEnd,
        recipients,
        rules: ruleRows,
      }))
      .then((result) => {
        setConfig(result);
        setRuleRows(result.effective_rules);
        setSavedAt(new Date().toLocaleTimeString());
      })
      .catch((err) => setConfigError(describeError(err)))
      .finally(() => setSaving(false));
  };

  const chart = spendSeries(data?.spend, currency);
  const status = statusBars(data?.invoices_by_status.by_status ?? {});
  const statusMax = Math.max(0, ...status.map((row) => row.count));

  return <div className="grid gap-5 pb-8">
    <PageHeader eyebrow="CONTROL ROOM · LIVE" title="Control room"
      description="Live charts and health for one department, refreshed every 30 seconds. Alerts fire the moment a metric crosses a threshold — this page only watches, it never acts."
      actions={<Button variant="secondary" onClick={() => setRevision((v) => v + 1)}><RefreshCw /> Refresh</Button>} />
    {error && <Alert tone="danger" title="Control room data could not be loaded">{error}</Alert>}
    {scopes === null && !error && <Skeleton className="h-40 w-full" />}
    {scopes && scopes.length === 0 && <Card><EmptyState icon={<Gauge />} title="No workflow scope yet"
      description="Join a department to see its live control room." /></Card>}
    {scopes && scopes.length > 0 && <>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Department" className="min-w-64">
          <SelectInput value={group} onChange={(event) => setGroup(event.target.value)} aria-label="Department">
            {scopes.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </SelectInput>
        </Field>
        <div className="ml-auto pb-1 text-xs text-muted">
          {scope?.can_manage ? "You manage this department · " : ""}
          {data ? `Updated ${timeOf(data.generated_at)} · refreshes every 30s` : "Loading…"}
        </div>
      </div>

      {data && data.active_alerts.length > 0 && <div className="grid gap-2" aria-label="Active alerts">
        {data.active_alerts.map((alert) => <Alert key={alert.rule_key} tone={alert.severity === "critical" ? "danger" : "warning"}
          title={`${alert.severity === "critical" ? "Critical" : "Warning"}: ${alert.title}`}>
          {alert.detail} <span className="text-muted">· evaluated {timeOf(alert.evaluated_at)}</span>
        </Alert>)}
      </div>}

      {data && <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Workflow engine" value={data.health.worker_enabled ? "On" : "Off"}
          hint={data.health.worker_enabled ? "Worker is enabled" : "Worker is off — runs will queue"}
          icon={<Activity className="size-4" />} />
        <Stat label="Last run activity" value={relativeAge(data.health.last_run_activity_age_seconds)}
          hint={data.runs.total ? `${data.runs.total} runs in this department` : "No runs yet"} />
        <Stat label="Ingestion workers" value={String(data.health.ingestion_workers_online)}
          hint={data.health.ingestion_workers_online ? "online" : "none online"} />
        <Stat label="LLM quota (max dept)" value={data.health.llm_quota_max_percent === null ? "—" : `${data.health.llm_quota_max_percent}%`}
          hint="Highest monthly token use across departments" />
      </div>}

      {data && <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader title="Spend by month" description="Verified, approved and paid invoices per month (organisation time zone)."
            actions={data.spend.currencies.length > 1 ? <SelectInput value={currency} onChange={(e) => setCurrency(e.target.value)} aria-label="Currency">
              {data.spend.currencies.map((c) => <option key={c}>{c}</option>)}
            </SelectInput> : undefined} />
          <CardBody>
            {!chart.series.length || !data.spend.rows.length ? <EmptyState icon={<Wallet />} title="No verified spend yet" />
              : <>
                <div className="flex h-56 items-end gap-2" role="img" aria-label="Monthly spend by department">
                  {chart.series.map((point) => <div key={point.month} className="flex h-full flex-1 flex-col justify-end" title={`${point.month}: ${currency} ${point.total.toLocaleString()}`}>
                    <div className="flex flex-col-reverse overflow-hidden rounded-t" style={{ height: `${chart.max ? (point.total / chart.max) * 100 : 0}%` }}>
                      {point.parts.map((part, index) => <div key={part.department} className={PALETTE[index % PALETTE.length]} style={{ height: `${point.total ? (part.amount / point.total) * 100 : 0}%` }} />)}
                    </div>
                    <div className="mt-1 text-center text-xs text-muted">{point.month.slice(2)}</div>
                  </div>)}
                </div>
                <div className="mt-3 flex flex-wrap gap-3 text-xs">{chart.departments.map((department, index) =>
                  <span key={department} className="flex items-center gap-1"><span className={`size-2.5 rounded-sm ${PALETTE[index % PALETTE.length]}`} />{department}</span>)}</div>
              </>}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Invoices by status" description="All invoices you may read in your departments." />
          <CardBody>
            {data.invoices_by_status.total === 0 ? <EmptyState icon={<Wallet />} title="No invoices visible"
              description="Invoice charts need a finance role for a department. Workflow data on this page is unaffected." />
              : <div className="grid gap-2.5">
                {status.map((row) => <div key={row.status} className="grid grid-cols-[110px_1fr_48px] items-center gap-2 text-sm">
                  <span className="truncate capitalize text-muted">{row.status.replace(/_/g, " ")}</span>
                  <div className="h-3 overflow-hidden rounded bg-surface-2">
                    <div className={`h-full rounded ${row.status === "exception" ? "bg-danger" : "bg-primary"}`} style={{ width: `${barPercent(row.count, statusMax)}%` }} />
                  </div>
                  <span className="text-right font-medium tabular-nums">{row.count}</span>
                </div>)}
                <p className="text-xs text-muted">{data.invoices_by_status.total} invoices in total</p>
              </div>}
          </CardBody>
        </Card>
      </div>}

      {data && <Card>
        <CardHeader title="Runs in this department" description="Live counts from the unified workflow engine. Recent failures are listed first — the full run history lives in the workflow engine." />
        <CardBody>
          {data.runs.total === 0 ? <EmptyState icon={<Activity />} title="No runs yet" description="Workflow runs for this department will appear here." />
            : <>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(data.runs.by_status).map(([status, count]) =>
                  <Badge key={status} tone={RUN_TONE[status] ?? "neutral"}>{status.replace(/_/g, " ")} · {count}</Badge>)}
                <Badge tone={data.runs.failed_last_hour ? "danger" : "neutral"}>failed in the last hour · {data.runs.failed_last_hour}</Badge>
              </div>
              {data.runs.recent_failed.length > 0 && <div className="mt-4 grid gap-2">
                {data.runs.recent_failed.map((run) => <div key={run.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm">
                  <AlertTriangle className="size-4 shrink-0 text-danger" />
                  <span className="font-medium">{run.workflow_name}</span>
                  <span className="text-muted">{run.failure_code ?? "failed"}</span>
                  {run.blocked_reason && <span className="text-muted">· {run.blocked_reason}</span>}
                  <span className="ml-auto text-xs text-muted">{run.finished_at ? timeOf(run.finished_at) : timeOf(run.created_at)}</span>
                </div>)}
              </div>}
            </>}
        </CardBody>
      </Card>}

      {data && <Card>
        <CardHeader title="Alert deliveries" icon={<Bell className="size-4" />}
          description="What the alert worker actually sent, newest first. WhatsApp goes first, then email; when no recipient is configured the alert stays in-app." />
        <CardBody>
          {deliveries === null ? <Skeleton className="h-16 w-full" />
            : deliveries.length === 0 ? <EmptyState icon={<Bell />} title="No alerts sent yet"
              description="Deliveries appear here the moment an alert fires. The live preview above shows which alerts would fire right now." />
            : <div className="grid gap-2">
              {deliveries.map((delivery) => <div key={delivery.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm">
                <AlertTriangle className={`size-4 shrink-0 ${delivery.severity === "critical" ? "text-danger" : "text-warning"}`} />
                <span className="font-medium">{delivery.title}</span>
                <Badge tone={delivery.status === "sent" ? "success" : delivery.status === "failed" ? "danger" : "neutral"}>{delivery.status}</Badge>
                <span className="text-muted">{delivery.channel}{delivery.recipient ? ` · ${delivery.recipient}` : ""}</span>
                {delivery.error && <span className="text-danger">· {delivery.error}</span>}
                <span className="ml-auto text-xs text-muted">{timeOf(delivery.evaluated_at)}</span>
              </div>)}
            </div>}
        </CardBody>
      </Card>}

      {scope?.can_manage && <Card>
        <CardHeader title="Alert settings" description="Who gets alerted, when the night is quiet, and which rules fire. Only department managers see this — changes are audited." />
        <CardBody>
          {config === null && !configError && <Skeleton className="h-40 w-full" />}
          {configError && <Alert tone="danger" title="Alert settings could not be loaded">{configError}</Alert>}
          {config && <div className="grid gap-5">
            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={formEnabled} onChange={(event) => setFormEnabled(event.target.checked)} />
                Alerts enabled for this department
              </label>
              <label className="field"><span className="field-label">Quiet hours start</span>
                <input type="time" value={quietStart} onChange={(event) => setQuietStart(event.target.value)} /></label>
              <label className="field"><span className="field-label">Quiet hours end</span>
                <input type="time" value={quietEnd} onChange={(event) => setQuietEnd(event.target.value)} /></label>
              <span className="text-xs text-muted">22:00–07:00 PKT by default; only critical alerts that break quiet hours are sent at night.</span>
            </div>

            <div className="grid gap-2">
              <h3 className="text-sm font-medium">Recipients</h3>
              {recipients.length === 0 && <p className="text-sm text-muted">No recipients yet — alerts stay in-app until one is added.</p>}
              {recipients.map((recipient, index) => <div key={`${recipient.channel}:${recipient.address}`} className="flex items-center gap-2 text-sm">
                <Badge tone={recipient.channel === "whatsapp" ? "primary" : "neutral"}>{recipient.channel}</Badge>
                <span>{recipient.address}</span>
                <Button variant="ghost" onClick={() => setRecipients((current) => current.filter((_, i) => i !== index))}>Remove</Button>
              </div>)}
              <div className="flex flex-wrap items-end gap-2">
                <Field label="Channel" className="min-w-36">
                  <SelectInput value={newChannel} onChange={(event) => setNewChannel(event.target.value as "whatsapp" | "email")} aria-label="Channel">
                    <option value="whatsapp">WhatsApp</option>
                    <option value="email">Email</option>
                  </SelectInput>
                </Field>
                <Field label="Number or email" error={newRecipientError} className="min-w-64">
                  <input type="text" value={newAddress} placeholder={newChannel === "whatsapp" ? "+923001234567" : "owner@example.com"}
                    onChange={(event) => setNewAddress(event.target.value)} />
                </Field>
                <Button variant="secondary" onClick={addRecipient}>Add recipient</Button>
              </div>
            </div>

            <div className="grid gap-2">
              <h3 className="text-sm font-medium">Rules</h3>
              <p className="text-xs text-muted">Threshold = when the rule fires. Cooldown = how long before the same alert can fire again. Only changes from the default are saved.</p>
              {ruleRows.map((row) => <div key={row.key} className="grid gap-2 rounded-lg border border-border p-3 sm:grid-cols-[1fr_auto] sm:items-center">
                <div>
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-medium">{row.title}</span>
                    <Badge tone={row.severity === "critical" ? "danger" : "warning"}>{row.severity}</Badge>
                    {row.breaks_quiet_hours && <Badge tone="primary">breaks quiet hours</Badge>}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                    <span>default threshold {row.default_threshold} · cooldown {Math.round(row.default_cooldown_seconds / 60)} min</span>
                    {(["whatsapp", "email", "in_app"] as const).map((channel) => <label key={channel} className="flex items-center gap-1">
                      <input type="checkbox" checked={row.channels.includes(channel)}
                        onChange={(event) => updateRule(row.key, { channels: event.target.checked
                          ? [...row.channels, channel]
                          : row.channels.filter((item) => item !== channel) })} />
                      {channel}
                    </label>)}
                  </div>
                </div>
                <div className="flex flex-wrap items-end gap-2">
                  <label className="field"><span className="field-label">Threshold</span>
                    <input type="number" min={0} step="any" value={row.threshold}
                      onChange={(event) => updateRule(row.key, { threshold: Number(event.target.value) })} /></label>
                  <label className="field"><span className="field-label">Cooldown (min)</span>
                    <input type="number" min={1} value={Math.round(row.cooldown_seconds / 60)}
                      onChange={(event) => updateRule(row.key, { cooldown_seconds: Math.max(1, Number(event.target.value)) * 60 })} /></label>
                  <label className="flex items-center gap-2 pb-2 text-sm">
                    <input type="checkbox" checked={row.enabled}
                      onChange={(event) => updateRule(row.key, { enabled: event.target.checked })} />
                    Enabled
                  </label>
                </div>
              </div>)}
            </div>

            {configError && <Alert tone="danger" title="Alert settings could not be saved">{configError}</Alert>}
            {savedAt && <Alert tone="success" title={`Alert settings saved at ${savedAt}`} />}
            <div>
              <Button onClick={saveConfig} disabled={saving}>{saving ? "Saving…" : "Save alert settings"}</Button>
            </div>
          </div>}
        </CardBody>
      </Card>}

      <p className="text-xs text-muted">
        The alert worker checks this department every 5 minutes and sends each fired alert once — WhatsApp first,
        then email, in-app when no recipient is configured. Non-critical alerts wait out quiet hours (22:00–07:00 PKT).
      </p>
    </>}
  </div>;
}
