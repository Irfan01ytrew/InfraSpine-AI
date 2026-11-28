import { useEffect, useState } from "react";
import { Link2, MessageCircle, Plus, Unlink } from "lucide-react";
import { api, describeError } from "../api";
import type { AgentStudioAgent } from "../types";
import { Alert, Badge, Button, Card, CardBody, CardHeader } from "./ui";

type Status = Awaited<ReturnType<typeof api.agentStudioWhatsAppQr>>;
const receiptLabels = {
  server_acknowledged: "WhatsApp server acknowledged the reply",
  device_delivered: "WhatsApp reported delivery to a linked device",
  read: "WhatsApp reported the reply as read",
};

export function AgentStudioWhatsAppQrPanel({ agents, onCreateAgent, onConfigureAgent }: {
  agents: AgentStudioAgent[]; onCreateAgent: () => void; onConfigureAgent: (agent: AgentStudioAgent) => void;
}) {
  const [state, setState] = useState<Status | null>(null);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const enabled = agents.filter(agent => agent.enabled);
  const agentId = selected || state?.connection?.agent_id || enabled[0]?.id || "";
  const agent = agents.find(item => item.id === agentId);
  const connection = state?.connection;

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await api.agentStudioWhatsAppQr();
        if (!stopped) { setState(next); setReconnecting(false); }
      } catch { if (!stopped) setReconnecting(true); }
      finally { if (!stopped) timer = setTimeout(() => void poll(), 3000); }
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, []);

  async function connect() {
    setBusy(true); setError(null);
    try {
      await api.connectAgentStudioWhatsAppQr(agentId);
      setState(await api.agentStudioWhatsAppQr());
    } catch (cause) { setError(describeError(cause)); }
    finally { setBusy(false); }
  }
  async function disconnect() {
    setBusy(true); setError(null);
    try {
      await api.disconnectAgentStudioWhatsAppQr();
      setState(await api.agentStudioWhatsAppQr());
    } catch (cause) { setError(describeError(cause)); }
    finally { setBusy(false); }
  }
  async function cancelCommand(id: string) {
    setBusy(true); setError(null);
    try {
      await api.cancelAgentStudioWhatsAppQrCommand(id);
      setState(await api.agentStudioWhatsAppQr());
    } catch (cause) { setError(describeError(cause)); }
    finally { setBusy(false); }
  }

  return <Card>
    <CardHeader title="Connect with a QR code" description="Baileys · Personal WhatsApp · No Meta app or webhook token required" icon={<MessageCircle />} actions={<Badge tone={connection?.status === "connected" ? "success" : "neutral"} dot>{connection?.status === "connected" ? "Connected" : connection?.status?.replaceAll("_", " ") || "Not connected"}</Badge>} />
    <CardBody className="grid gap-5">
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-xl border border-border p-4"><strong className="text-primary">1 · Choose your agent</strong><p className="mt-2 text-sm text-muted">Select an enabled agent below, then click Show QR.</p></div>
        <div className="rounded-xl border border-border p-4"><strong className="text-primary">2 · Scan from your phone</strong><p className="mt-2 text-sm text-muted">WhatsApp → Settings / ⋮ → Linked devices → Link a device.</p></div>
        <div className="rounded-xl border border-border p-4"><strong className="text-primary">3 · Message yourself</strong><p className="mt-2 text-sm text-muted">Open your own WhatsApp chat and send <code>Irfanix, laptop check karo</code>. Follow-ups work for five minutes after your last command.</p></div>
      </div>
      {error && <Alert tone="danger" title="Connection needs attention">{error}</Alert>}
      {reconnecting && <p role="status" className="text-sm text-muted">Reconnecting to InfraSpine…</p>}
      {state && !state.available && <Alert tone="warning" title="Bridge dependency missing">Install Node.js 20+ and run npm ci in integrations/whatsapp-baileys on the server.</Alert>}
      {!enabled.length ? <Button onClick={onCreateAgent}><Plus /> Create a WhatsApp agent</Button> : <label className="grid gap-2 text-sm font-medium">Agent for my WhatsApp commands
        <select value={agentId} disabled={busy || Boolean(connection?.enabled)} onChange={event => setSelected(event.target.value)} className="rounded-lg border border-border bg-surface p-3">
          {enabled.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>}
      {agent && !agent.tool_permissions.includes("my_work") && <Alert tone="warning" title="Enable pending-work access">For your assigned work, enable “My assigned open work” in this agent's permissions. <Button variant="secondary" size="sm" onClick={() => onConfigureAgent(agent)}>Configure agent</Button></Alert>}
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy || !state?.available || !agent?.enabled || Boolean(connection?.enabled)} onClick={() => void connect()}><Link2 /> {busy ? "Please wait…" : "Show QR / Reconnect"}</Button>
        {connection && <Button variant="secondary" disabled={busy || !connection.enabled} onClick={() => void disconnect()}><Unlink /> Disconnect</Button>}
      </div>
      {connection?.qr && <div className="flex flex-col items-center gap-3 rounded-xl border border-border bg-surface-2 p-5">
        <img src={connection.qr} width={280} height={280} alt="Scan this private WhatsApp linking QR with your phone" className="rounded-lg bg-white" />
        <p className="text-sm text-muted">QR refreshes automatically. Keep it private.</p>
      </div>}
      {connection?.enabled && !connection.qr && connection.status !== "connected" && <p role="status" className="text-sm text-muted">Connecting to WhatsApp and preparing your QR…</p>}
      {connection?.status === "connected" && <Alert tone="success" title="WhatsApp linked">Apni “Message yourself” chat mein <strong>Irfanix, laptop check karo</strong> bhejein. Approvals dashboard mein milengi.</Alert>}
      {connection?.last_error && <Alert tone="warning" title="Connection status">{connection.last_error}</Alert>}
      {connection?.latest_command && <div className="rounded-xl border border-border p-4 text-sm">
        <strong>Latest command</strong>
        <time className="ml-2 text-muted" dateTime={connection.latest_command.received_at}>{new Date(connection.latest_command.received_at).toLocaleString()}</time>
        <p className="mt-1">{connection.latest_command.receipt
          ? receiptLabels[connection.latest_command.receipt]
          : connection.latest_command.status === "completed"
            ? "Reply submitted to WhatsApp. Waiting for a delivery receipt."
            : `Command ${connection.latest_command.status.replaceAll("_", " ")}`}</p>
        {connection.latest_command.status === "queued" &&
          <Button variant="secondary" size="sm" className="mt-2" disabled={busy} onClick={() => void cancelCommand(connection.latest_command!.id)}>
            Cancel queued command
          </Button>}
      </div>}
      <p className="text-sm leading-relaxed text-muted">Baileys is a free, unofficial open-source WhatsApp client. AI provider charges may still apply; Ollama can run locally. Only your own self-chat commands are processed; other contacts and groups cannot control your agents. Session keys are encrypted. To revoke access from your phone: WhatsApp → Linked devices → Log out.</p>
    </CardBody>
  </Card>;
}
