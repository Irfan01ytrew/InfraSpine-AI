import { useCallback, useEffect, useState } from "react";
import { Copy, KeyRound, Pause, Play, Plus, RefreshCw, Send, Trash2, Webhook as WebhookIcon } from "lucide-react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import { useLiveRefresh } from "../lib/useLiveRefresh";
import type { Webhook, WebhookDelivery } from "../types";
import { useFeedback } from "./Feedback";
import { Alert, Badge, Button, Card, CardBody, CardHeader, EmptyState, Field, Input, PageHeader, Skeleton } from "./ui";

const EVENT_LABELS: Record<string, string> = {
  "request.submitted": "Request submitted",
  "request.status_changed": "Request status changed",
  "request.approved": "Request approved",
  "request.rejected": "Request rejected",
  "request.cancelled": "Request cancelled",
  "approval.step_pending": "Approval step waiting",
};
const STATUS_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  sent: "success", retry: "warning", pending: "neutral", sending: "neutral", failed: "danger", cancelled: "neutral",
};

function Deliveries({ hook, onChanged }: { hook: Webhook; onChanged: () => void }) {
  const { toast } = useFeedback();
  const [rows, setRows] = useState<WebhookDelivery[] | null>(null);
  const load = useCallback(async () => {
    try { setRows((await api.webhookDeliveries(hook.id)).deliveries); } catch { setRows([]); }
  }, [hook.id]);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load, 15_000);
  if (rows === null) return <Skeleton className="h-16" />;
  if (rows.length === 0) return <p className="text-xs text-muted">No deliveries yet. Send a test to check the receiver.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-left text-xs" aria-label={`Deliveries for ${hook.name}`}>
        <thead className="text-muted"><tr><th className="py-1 pr-3 font-medium">Event</th><th className="pr-3 font-medium">Status</th><th className="pr-3 font-medium">HTTP</th><th className="pr-3 font-medium">Tries</th><th className="pr-3 font-medium">When</th><th className="font-medium" /></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className="border-t border-border align-top">
              <td className="py-1.5 pr-3">{EVENT_LABELS[row.event] ?? row.event}</td>
              <td className="pr-3"><Badge tone={STATUS_TONE[row.status] ?? "neutral"}>{row.status}</Badge>{row.last_error && <span className="block max-w-[220px] text-muted [overflow-wrap:anywhere]">{row.last_error}</span>}</td>
              <td className="pr-3 tabular-nums">{row.response_status ?? "—"}{row.duration_ms != null && <span className="text-muted"> · {row.duration_ms} ms</span>}</td>
              <td className="pr-3 tabular-nums">{row.attempts}</td>
              <td className="pr-3">{formatDate(row.sent_at ?? row.created_at)}{row.status === "retry" && row.next_attempt_at && <span className="block text-muted">next {formatDate(row.next_attempt_at)}</span>}</td>
              <td>{(row.status === "sent" || row.status === "failed" || row.status === "cancelled") && (
                <Button size="sm" variant="ghost" onClick={() => void api.redeliverWebhook(row.id).then(() => { toast("success", "Queued again"); void load(); onChanged(); }).catch((err) => toast("error", "Could not redeliver", describeError(err)))}>Redeliver</Button>
              )}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Administrators send request and approval events to other systems (ERP, chat, ticketing) as signed HTTPS calls. */
export function WebhooksPage() {
  const { toast } = useFeedback();
  const [hooks, setHooks] = useState<Webhook[] | null>(null);
  const [events, setEvents] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState({ name: "", url: "", events: ["request.submitted"] as string[] });
  const [secret, setSecret] = useState<{ name: string; value: string } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await api.webhooks();
      setHooks(result.webhooks); setEvents(result.events); setError(null);
    } catch (err) { setError(describeError(err)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(load, 30_000);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api.createWebhook({ name: draft.name.trim(), url: draft.url.trim(), events: draft.events });
      setHooks(result.webhooks);
      setSecret({ name: draft.name.trim(), value: result.secret });
      setDraft({ name: "", url: "", events: ["request.submitted"] });
      toast("success", "Webhook added", "Copy the signing secret now; it is not shown again.");
    } catch (err) { toast("error", "Could not add the webhook", describeError(err)); }
    finally { setBusy(false); }
  }

  async function act(label: string, action: () => Promise<{ webhooks: Webhook[]; secret?: string }>, hook?: Webhook) {
    try {
      const result = await action();
      setHooks(result.webhooks);
      if (result.secret && hook) setSecret({ name: hook.name, value: result.secret });
      toast("success", label);
    } catch (err) { toast("error", "Could not update the webhook", describeError(err)); }
  }

  const toggle = (name: string) => setDraft({ ...draft, events: draft.events.includes(name) ? draft.events.filter((e) => e !== name) : [...draft.events, name] });

  return (
    <>
      <PageHeader title="Webhooks" description="Send request and approval events to your other systems as signed HTTPS calls. HR requests are never sent." actions={<Button onClick={() => void load()}><RefreshCw /> Refresh</Button>} />
      {error && <Alert tone="danger" className="mb-5">{error}</Alert>}
      {secret && (
        <Alert tone="success" className="mb-5">
          <div className="grid gap-2">
            <strong>Signing secret for “{secret.name}” — copy it now, it is shown only once.</strong>
            <code className="block rounded bg-surface-2 px-2 py-1 text-xs [overflow-wrap:anywhere]">{secret.value}</code>
            <span className="text-xs">Verify each call: HMAC-SHA256 of <code>&lt;t&gt;.&lt;raw body&gt;</code> with this secret must equal <code>v1</code> in the <code>X-InfraSpine-Signature: t=…,v1=…</code> header; reject old timestamps.</span>
            <span className="flex gap-2">
              <Button size="sm" onClick={() => void navigator.clipboard.writeText(secret.value).then(() => toast("success", "Secret copied")).catch(() => toast("error", "Could not copy", "Select and copy it manually."))}><Copy /> Copy</Button>
              <Button size="sm" variant="ghost" onClick={() => setSecret(null)}>I have stored it</Button>
            </span>
          </div>
        </Alert>
      )}
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
        <Card className="overflow-hidden">
          <CardHeader icon={<Plus />} title="New webhook" description="Public HTTPS addresses only; internal addresses need the deployment's approval." />
          <CardBody>
            <form className="grid gap-3" onSubmit={(event) => void create(event)}>
              <Field label="Name"><Input maxLength={120} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="e.g. ERP purchase sync" /></Field>
              <Field label="Receiver URL"><Input maxLength={2000} value={draft.url} onChange={(e) => setDraft({ ...draft, url: e.target.value })} placeholder="https://erp.example.com/hooks/infraspine" /></Field>
              <fieldset className="grid gap-1.5">
                <legend className="mb-1 text-xs font-medium text-foreground-2">Events</legend>
                {(events.length ? events : Object.keys(EVENT_LABELS)).map((name) => (
                  <label key={name} className="inline-flex items-center gap-2 text-[13px] text-foreground-2">
                    <input type="checkbox" className="size-4 accent-[var(--brand)]" checked={draft.events.includes(name)} onChange={() => toggle(name)} />
                    {EVENT_LABELS[name] ?? name}
                  </label>
                ))}
              </fieldset>
              <Button type="submit" variant="primary" loading={busy} disabled={busy || draft.name.trim().length < 2 || !draft.url.trim() || draft.events.length === 0}>{!busy && <Plus />} Add webhook</Button>
            </form>
          </CardBody>
        </Card>

        <div className="grid min-w-0 gap-4">
          {hooks === null ? <Skeleton className="h-32" /> : hooks.length === 0 ? (
            <Card><EmptyState icon={<WebhookIcon />} title="No webhooks yet" description="Add one to send events to another system." /></Card>
          ) : hooks.map((hook) => (
            <Card key={hook.id} className="overflow-hidden">
              <CardBody className="grid gap-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-[14px] font-semibold text-foreground">{hook.name}</p>
                    <p className="text-xs text-muted [overflow-wrap:anywhere]">{hook.url}</p>
                  </div>
                  <span className="flex items-center gap-1.5">
                    {hook.is_active ? <Badge tone="success" dot>Active</Badge> : <Badge tone="warning" dot>Paused</Badge>}
                    {hook.last_delivery && <Badge tone={STATUS_TONE[hook.last_delivery.status] ?? "neutral"}>Last: {hook.last_delivery.status}</Badge>}
                  </span>
                </div>
                {hook.disabled_reason && <Alert tone="warning">{hook.disabled_reason} Fix the receiver, then resume.</Alert>}
                <div className="flex flex-wrap gap-1.5">{hook.events.map((e) => <Badge key={e}>{EVENT_LABELS[e] ?? e}</Badge>)}</div>
                <div className="flex flex-wrap gap-1 border-t border-border pt-2">
                  <Button size="sm" variant="ghost" onClick={() => void act("Test queued", () => api.testWebhook(hook.id))}><Send /> Send test</Button>
                  {hook.is_active
                    ? <Button size="sm" variant="ghost" onClick={() => void act("Webhook paused", () => api.updateWebhook(hook.id, { is_active: false }))}><Pause /> Pause</Button>
                    : <Button size="sm" variant="ghost" onClick={() => void act("Webhook resumed", () => api.updateWebhook(hook.id, { is_active: true }))}><Play /> Resume</Button>}
                  <Button size="sm" variant="ghost" onClick={() => void act("New secret created", () => api.rotateWebhookSecret(hook.id), hook)}><KeyRound /> New secret</Button>
                  <Button size="sm" variant="ghost" aria-expanded={open === hook.id} onClick={() => setOpen(open === hook.id ? null : hook.id)}>{open === hook.id ? "Hide deliveries" : "Deliveries"}</Button>
                  <span className="flex-1" />
                  <Button size="sm" variant="danger-ghost" onClick={() => void act("Webhook deleted", () => api.deleteWebhook(hook.id))}><Trash2 /> Delete</Button>
                </div>
                {open === hook.id && <Deliveries hook={hook} onChanged={() => void load()} />}
              </CardBody>
            </Card>
          ))}
        </div>
      </div>
    </>
  );
}
