import { useEffect, useMemo, useState } from "react";
import { Copy, ExternalLink, Link2, MessageCircle, Plus, RefreshCw, ShieldCheck, Unlink } from "lucide-react";
import { api, describeError } from "../api";
import { formatDate } from "../format";
import type { AgentStudioAgent, AgentStudioWhatsAppConnection } from "../types";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Field, Input, Textarea } from "./ui";
import { AgentStudioWhatsAppQrPanel } from "./AgentStudioWhatsAppQrPanel";

const EMPTY = {
  agent_id: "",
  phone_number_id: "",
  business_account_id: "",
  allowed_senders: "",
  api_version: "v26.0",
  access_token: "",
  app_secret: "",
  verify_token: "",
};

function createVerifyToken(): string {
  const bytes = window.crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function AgentStudioWhatsAppPanel({ agents, onCreateAgent, onConfigureAgent }: {
  agents: AgentStudioAgent[];
  onCreateAgent: () => void;
  onConfigureAgent: (agent: AgentStudioAgent) => void;
}) {
  const [connection, setConnection] = useState<AgentStudioWhatsAppConnection | null>(null);
  const [mode, setMode] = useState<"qr" | "cloud">("qr");
  const [draft, setDraft] = useState({ ...EMPTY, verify_token: createVerifyToken() });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const activeAgents = useMemo(() => agents.filter((agent) => agent.enabled), [agents]);
  const selectedAgentId = draft.agent_id || activeAgents[0]?.id || "";
  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId) ?? null;
  const selectedAgentEnabled = Boolean(selectedAgent?.enabled);

  useEffect(() => {
    let cancelled = false;
    void api.agentStudioWhatsApp().then(({ connection: current }) => {
      if (cancelled) return;
      setConnection(current);
      if (current) {
        setDraft({
          ...EMPTY,
          agent_id: current.agent_id,
          phone_number_id: current.phone_number_id,
          business_account_id: current.business_account_id,
          allowed_senders: current.allowed_senders.join("\n"),
          api_version: current.api_version,
        });
      }
    }).catch((cause) => { if (!cancelled) setError(describeError(cause)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  function update<K extends keyof typeof EMPTY>(key: K, value: (typeof EMPTY)[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  async function save() {
    setBusy(true); setError(null); setNotice(null);
    try {
      const allowed = [...new Set(draft.allowed_senders.split(/[\n,;]+/).map((phone) => phone.replace(/\D/g, "")).filter(Boolean))];
      const result = await api.saveAgentStudioWhatsApp({
        agent_id: selectedAgentId,
        phone_number_id: draft.phone_number_id.trim(),
        business_account_id: draft.business_account_id.trim(),
        allowed_senders: allowed,
        api_version: draft.api_version.trim() || "v26.0",
        ...(draft.access_token.trim() ? { access_token: draft.access_token.trim() } : {}),
        ...(draft.app_secret.trim() ? { app_secret: draft.app_secret.trim() } : {}),
        ...(draft.verify_token.trim() ? { verify_token: draft.verify_token.trim() } : {}),
      });
      setConnection(result.connection);
      setDraft((current) => ({ ...current, access_token: "", app_secret: "" }));
      setNotice("WhatsApp connection saved. Add the callback URL and verify token in Meta, then verify the connection here.");
    } catch (cause) { setError(describeError(cause)); }
    finally { setBusy(false); }
  }

  async function verify() {
    setBusy(true); setError(null); setNotice(null);
    try {
      const result = await api.verifyAgentStudioWhatsApp();
      const refreshed = await api.agentStudioWhatsApp();
      setConnection(refreshed.connection);
      setNotice(`Meta verified ${result.display_phone_number || "your number"}${result.verified_name ? ` (${result.verified_name})` : ""} and subscribed the business number to messages.`);
    } catch (cause) { setError(describeError(cause)); }
    finally { setBusy(false); }
  }

  async function disconnect() {
    setBusy(true); setError(null); setNotice(null);
    try {
      await api.disconnectAgentStudioWhatsApp();
      setConnection(null); setDraft({ ...EMPTY, verify_token: createVerifyToken() });
      setNotice("WhatsApp connection removed.");
    } catch (cause) { setError(describeError(cause)); }
    finally { setBusy(false); }
  }

  async function copy(value: string) {
    try { await navigator.clipboard.writeText(value); setNotice("Copied to clipboard."); }
    catch { setError("Clipboard access is unavailable in this browser. Select and copy the value manually."); }
  }

  const webhookUrl = connection?.webhook_url ?? "";
  const webhookIsPublicHttps = webhookUrl.startsWith("https://") && !/localhost|127\.0\.0\.1/i.test(webhookUrl);

  return <div className="grid gap-4">
    <div className="flex gap-2">
      <Button variant={mode === "qr" ? "primary" : "secondary"} onClick={() => setMode("qr")}>QR se connect · Baileys</Button>
      <Button variant={mode === "cloud" ? "primary" : "secondary"} onClick={() => setMode("cloud")}>Meta Cloud API</Button>
    </div>
    {mode === "qr" ? <AgentStudioWhatsAppQrPanel agents={agents} onCreateAgent={onCreateAgent} onConfigureAgent={onConfigureAgent} /> : <>
    {error && <Alert tone="danger" title="WhatsApp setup needs attention">{error}</Alert>}
    {notice && <Alert tone="success" title={notice} />}
    <Card>
      <CardHeader title="How the WhatsApp connection works" description="This uses the official Meta WhatsApp Business Cloud API. Your regular personal WhatsApp account cannot be connected directly." icon={<MessageCircle />} />
      <CardBody className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-lg border border-border bg-surface-2/40 p-3"><p className="text-xs font-semibold text-primary">1 · Prepare Meta</p><p className="mt-1 text-xs leading-relaxed text-muted">Create a Meta Business app, add WhatsApp, and use a business phone number with its Phone Number ID and WABA ID.</p></div>
        <div className="rounded-lg border border-border bg-surface-2/40 p-3"><p className="text-xs font-semibold text-primary">2 · Save securely here</p><p className="mt-1 text-xs leading-relaxed text-muted">Choose an enabled agent, add the Meta IDs, permanent token, App Secret and approved sender numbers. Secrets are encrypted before storage.</p></div>
        <div className="rounded-lg border border-border bg-surface-2/40 p-3"><p className="text-xs font-semibold text-primary">3 · Register the webhook</p><p className="mt-1 text-xs leading-relaxed text-muted">After saving, copy the callback URL and verify token into Meta Webhooks, subscribe to <strong>messages</strong>, then verify the connection here.</p></div>
        <p className="text-xs leading-relaxed text-muted sm:col-span-3">Roman Urdu: WhatsApp connect karne ke liye Meta Business app aur business number zaroori hai. Personal WhatsApp seedha connect nahi hota. Meta token aur App Secret sirf is dashboard mein dain—chat mein share na karein.</p>
        <a className="inline-flex items-center gap-1 text-xs text-primary underline sm:col-span-3" href="https://developers.facebook.com/apps/" target="_blank" rel="noreferrer">Open Meta for Developers <ExternalLink className="size-3" /></a>
      </CardBody>
    </Card>
    <Card>
      <CardHeader title="Connect an agent to WhatsApp" description="Messages from approved phone numbers become private Agent Studio tasks. Credentials are encrypted at rest." icon={<MessageCircle />} actions={<Badge tone={connection?.last_verified_at ? "success" : connection ? "warning" : "neutral"} dot>{connection?.last_verified_at ? "Connected" : connection ? "Setup needed" : "Not connected"}</Badge>} />
      <CardBody className="grid gap-4">
        {loading ? <p className="text-sm text-muted">Loading WhatsApp settings…</p> : <>
          {!activeAgents.length && <Alert tone="warning" title="Create an enabled agent first">WhatsApp routes each approved sender's message to an agent. Start with the support-agent template, then return here to connect your Meta account. <Button size="sm" variant="secondary" className="mt-2" onClick={onCreateAgent}><Plus /> Create WhatsApp agent</Button></Alert>}
          {selectedAgent?.enabled && !selectedAgent.tool_permissions.includes("my_work") && <Alert tone="warning" title="Pending-work questions need one permission">To answer “what work is pending for me?”, this agent needs the <strong>My assigned open work</strong> tool. It only reads tasks assigned to your own account. <Button size="sm" variant="secondary" className="mt-2" onClick={() => onConfigureAgent(selectedAgent)}><ShieldCheck /> Enable task access</Button></Alert>}
          <div className="grid gap-3 md:grid-cols-2">
            <Field label="Agent that handles WhatsApp tasks">
              <select className="h-10 w-full rounded-md border border-border bg-surface px-3 text-sm" value={selectedAgentId} onChange={(event) => update("agent_id", event.target.value)} disabled={!activeAgents.length}>
                {selectedAgent && !selectedAgent.enabled && <option value={selectedAgent.id} disabled>{selectedAgent.name} · paused</option>}
                {activeAgents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
              </select>
            </Field>
            <Field label="Meta Graph API version" hint="Current default: v26.0. Change only if your Meta app requires another active version.">
              <Input value={draft.api_version} onChange={(event) => update("api_version", event.target.value)} placeholder="v26.0" />
            </Field>
            <Field label="WhatsApp Phone Number ID" hint="Meta App Dashboard → WhatsApp → API Setup.">
              <Input value={draft.phone_number_id} onChange={(event) => update("phone_number_id", event.target.value)} inputMode="numeric" required />
            </Field>
            <Field label="WhatsApp Business Account ID" hint="The WABA ID linked to this phone number.">
              <Input value={draft.business_account_id} onChange={(event) => update("business_account_id", event.target.value)} inputMode="numeric" required />
            </Field>
            <Field label="Approved sender numbers" hint="Include country code, digits only. Only these people can run your agent. One per line.">
              <Textarea value={draft.allowed_senders} onChange={(event) => update("allowed_senders", event.target.value)} placeholder={"923001234567\n923331234567"} rows={3} required />
            </Field>
            <div className="grid gap-3">
              <Field label="Permanent access token" hint={connection?.access_token_configured ? "Saved encrypted. Leave blank to keep the current token; enter a value to rotate it." : "Use a Meta system-user token with WhatsApp Business Messaging and Management permissions."}>
                <Input type="password" autoComplete="new-password" value={draft.access_token} onChange={(event) => update("access_token", event.target.value)} required={!connection} />
              </Field>
              <Field label="Meta App Secret" hint={connection?.app_secret_configured ? "Saved encrypted. Leave blank to keep the current secret." : "Meta App Dashboard → App settings → Basic."}>
                <Input type="password" autoComplete="new-password" value={draft.app_secret} onChange={(event) => update("app_secret", event.target.value)} required={!connection} />
              </Field>
            </div>
          </div>
          <Field label="Webhook Verify Token" hint={connection ? "The saved value is hidden for security. Regenerate and save this connection first if you need a new one for Meta." : "Generated securely for you. Save the connection, then copy it below into Meta's webhook settings. You do not need to obtain this token from Meta."}>
            <div className="flex gap-2"><Input type="text" autoComplete="off" value={draft.verify_token} onChange={(event) => update("verify_token", event.target.value)} required={!connection} /><Button type="button" variant="secondary" onClick={() => update("verify_token", createVerifyToken())}>Regenerate</Button></div>
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" disabled={busy || !selectedAgentId || !selectedAgentEnabled} onClick={() => void save()}>{busy ? "Saving…" : <><Link2 /> Save connection</>}</Button>
            {connection && <Button variant="secondary" disabled={busy} onClick={() => void verify()}>{busy ? "Checking…" : <><RefreshCw /> Verify & subscribe</>}</Button>}
            {connection && <Button variant="ghost" disabled={busy} onClick={() => void disconnect()}><Unlink /> Disconnect</Button>}
          </div>
        </>}
      </CardBody>
    </Card>

    {connection && <Card>
      <CardHeader title="Meta webhook setup" description="Paste these values into your Meta Developer App to receive inbound WhatsApp messages." icon={<ShieldCheck />} />
      <CardBody className="grid gap-4">
        {!webhookIsPublicHttps && <Alert tone="warning" title="Meta needs a public HTTPS address">The current callback URL is local or not HTTPS, so Meta cannot reach it. Set WHATSAPP_WEBHOOK_BASE_URL to your public HTTPS app address (or a secure tunnel) and reload this page.</Alert>}
        <Field label="Callback URL"><div className="flex gap-2"><Input readOnly value={webhookUrl} /><Button variant="secondary" onClick={() => void copy(webhookUrl)} aria-label="Copy callback URL"><Copy /></Button></div></Field>
        <Field label="Verify token" hint="Paste this same value in Meta. If you regenerated it, save the connection again before verifying the webhook."><div className="flex gap-2"><Input type="text" value={draft.verify_token} onChange={(event) => update("verify_token", event.target.value)} placeholder="Regenerate and save if the original value is unavailable" /><Button variant="secondary" onClick={() => void copy(draft.verify_token)} disabled={!draft.verify_token} aria-label="Copy verify token"><Copy /></Button></div></Field>
        <ol className="grid gap-2 pl-5 text-sm text-foreground-2 list-decimal">
          <li>In <a className="text-primary underline" href="https://developers.facebook.com/apps/" target="_blank" rel="noreferrer">Meta for Developers</a>, create or open a Business app and add the WhatsApp product.</li>
          <li>Open Webhooks, choose <strong>WhatsApp Business Account</strong>, paste the callback URL and the same verify token, then verify and save.</li>
          <li>Subscribe the <strong>messages</strong> webhook field. Add a permanent system-user access token, App Secret, IDs and allowed sender numbers above.</li>
          <li>Save the connection here, then click <strong>Verify & subscribe</strong>. Send a text message to your WhatsApp Business number to start an agent task.</li>
        </ol>
        <p className="text-xs text-muted">Current agent: {connection.agent_name}. Allowed senders: {connection.allowed_senders.join(", ")}. {connection.last_verified_at ? `Last checked ${formatDate(connection.last_verified_at)}.` : "Not verified yet."}</p>
        <a className="inline-flex items-center gap-1 text-xs text-primary underline" href="https://developers.facebook.com/documentation/business-messaging/whatsapp/overview" target="_blank" rel="noreferrer">WhatsApp Cloud API guide <ExternalLink className="size-3" /></a>
      </CardBody>
    </Card>}
    </>}
  </div>;
}
