import { useState } from "react";
import { CheckCircle2, Clock3, Download, ShieldCheck } from "lucide-react";
import { Badge, Button, Card, CardBody, CardHeader } from "../components/ui";
import { recovery, root, type Config, type Detail, type Target } from "./api";

export const field = "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary";
export const date = (value: string | null) => value ? new Date(value).toLocaleString() : "Not observed";
export const label = (value: string) => value.replaceAll("_", " ");
export function Status({ value }: { value: string }) {
  return <Badge tone={value === "resolved" || value === "succeeded" ? "success" : ["critical", "failed", "dead_letter", "escalated"].includes(value) ? "danger" : value === "awaiting_approval" || value === "high" ? "warning" : "neutral"}>{label(value)}</Badge>;
}

export function IncidentDetail({ tenant, detail, config, target, busy, act }: {
  tenant: string; detail: Detail; config: Config; target?: Target; busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const i = detail.incident;
  const [playbook, setPlaybook] = useState("");
  const [reason, setReason] = useState("");
  const [disposition, setDisposition] = useState("false_positive");
  const [note, setNote] = useState("");
  const [kind, setKind] = useState("evidence");
  const canWrite = config.role !== "observer";
  const owner = config.role === "owner";
  const terminal = ["resolved", "cancelled"].includes(i.status);
  const movable = !terminal && !["recovering", "verifying"].includes(i.status);
  const canDiagnose = canWrite && !terminal && !["recovering", "verifying", "awaiting_approval", "recovery_proposed"].includes(i.status);
  const eligible = config.playbooks.filter(p => p.enabled && p.adapter === target?.adapter);
  const transition = (state: string) => act(() => recovery.post(tenant, `incidents/${i.id}/transition`, { version: i.version, state, reason, ...(state === "resolved" || state === "cancelled" ? { disposition } : {}) }));
  return <div className="space-y-4" data-testid="incident-detail">
    <Card><CardHeader title={i.title} description={`${target?.name ?? i.target_id} · ${label(i.source)} / ${label(i.category)}`} actions={<Status value={i.severity} />} />
      <CardBody className="space-y-4">
        <div className="flex flex-wrap gap-2"><Status value={i.status} /><Badge>{i.occurrences} occurrences</Badge><Badge>Cycle {i.generation}</Badge><Badge>{i.recovery_count}/10 recovery budget</Badge></div>
        <dl className="grid grid-cols-2 gap-3 text-xs"><div><dt className="text-muted">Detected</dt><dd>{date(i.opened_at)}</dd></div><div><dt className="text-muted">SLA deadline</dt><dd>{date(i.sla_due)}</dd></div><div><dt className="text-muted">Last failure</dt><dd>{date(i.last_seen)}</dd></div><div><dt className="text-muted">Verified restoration</dt><dd>{date(i.verified_at)}</dd></div></dl>
        <div className="flex flex-wrap gap-2"><Button disabled={!canDiagnose || busy} onClick={() => void act(() => recovery.post(tenant, `incidents/${i.id}/diagnose`))}>Investigate</Button><a className="inline-flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm" href={`${root(tenant)}/incidents/${i.id}/report`} download><Download size={14} />Incident report</a></div>
        {Object.keys(i.diagnosis).length > 0 && <section className="rounded-lg bg-surface-2 p-3 text-sm"><h4 className="mb-2 font-semibold">Diagnosis · hypothesis, unconfirmed</h4><p>{String(i.diagnosis.hypothesis ?? "Investigation recorded")}</p><p className="mt-1 text-xs text-muted">Confidence {Math.round(Number(i.diagnosis.confidence ?? 0) * 100)}% · grounded in recorded observations</p><details className="mt-3"><summary className="cursor-pointer">Observations, dependency impact and known fixes</summary><pre className="mt-2 whitespace-pre-wrap break-words text-xs">{JSON.stringify(i.diagnosis, null, 2)}</pre></details></section>}
        {canWrite && ["investigating", "failed", "escalated"].includes(i.status) && <form className="space-y-2 border-t border-border pt-3" onSubmit={e => { e.preventDefault(); void act(() => recovery.post(tenant, `incidents/${i.id}/recoveries`, { version: i.version, playbook_id: playbook, request_key: crypto.randomUUID() })); }}>
          <label className="text-sm font-medium" htmlFor="recovery-playbook">Recovery playbook</label><select id="recovery-playbook" required className={field} value={playbook} onChange={e => setPlaybook(e.target.value)}><option value="">Select an available playbook</option>{eligible.map(p => <option key={p.id} value={p.id}>{p.name} · {p.action} · {p.preapproved ? "preapproved" : "human approval"}</option>)}</select>
          {!eligible.length && <p className="text-xs text-muted">No enabled playbook supports this connected adapter.</p>}<Button type="submit" disabled={busy || !playbook}>Propose recovery</Button>
        </form>}
        {canWrite && movable && <div className="space-y-2 border-t border-border pt-3"><label htmlFor="incident-reason" className="text-sm font-medium">Decision rationale</label><textarea id="incident-reason" className={field} value={reason} maxLength={2000} onChange={e => setReason(e.target.value)} placeholder="Record the reason for this decision" />
          <div className="flex flex-wrap gap-2">{["detected", "reopened"].includes(i.status) && <Button disabled={busy || reason.trim().length < 3} onClick={() => void transition("triaged")}>Acknowledge</Button>}{i.status !== "escalated" && <Button disabled={busy || reason.trim().length < 3} onClick={() => void transition("escalated")}>Escalate</Button>}</div>
          {owner && <details><summary className="cursor-pointer text-xs text-muted">Authorized disposition</summary><p className="my-2 text-xs text-muted">Automatic restoration requires successful health checks. Record a disposition to close a false positive, duplicate or accepted risk.</p><select aria-label="Disposition" className={field} value={disposition} onChange={e => setDisposition(e.target.value)}><option value="false_positive">False positive</option><option value="duplicate">Duplicate incident</option><option value="accepted_risk">Accepted risk</option></select><div className="mt-2 flex gap-2"><Button disabled={busy || reason.trim().length < 3 || ["awaiting_approval", "recovery_proposed"].includes(i.status)} onClick={() => void transition("resolved")}>Resolve with disposition</Button><Button disabled={busy || reason.trim().length < 3} onClick={() => void transition("cancelled")}>Cancel incident</Button></div></details>}
        </div>}
      </CardBody></Card>
    <Card><CardHeader title="Recovery execution history" icon={<Clock3 />} /><CardBody className="space-y-3">{detail.commands.length === 0 ? <p className="text-sm text-muted">No recovery has been proposed.</p> : detail.commands.map(c => <div key={c.id} className="rounded-lg border border-border p-3 text-sm"><div className="flex flex-wrap justify-between gap-2"><strong>{config.playbooks.find(p => p.id === c.playbook_id)?.name ?? "Recovery"}</strong><Status value={c.status} /></div><p className="mt-2 text-xs text-muted">{date(c.created_at)} · {c.attempts} attempts · {c.successes} healthy samples</p>{c.last_error && <p className="mt-1 text-xs text-danger-text">{label(c.last_error)}</p>}
      {c.status === "awaiting_approval" && <div className="mt-3 space-y-2"><p className="text-xs">Approval expires {date(c.approval_expires)}. Action fingerprint <code className="break-all">{c.action_digest}</code></p>{owner && c.requester_id !== config.user_id ? <><label className="text-xs" htmlFor={`approval-${c.id}`}>Approval rationale</label><textarea id={`approval-${c.id}`} className={field} value={reason} onChange={e => setReason(e.target.value)} /><div className="flex gap-2">{[true, false].map(approve => <Button key={String(approve)} disabled={busy || reason.trim().length < 3} onClick={() => void act(() => recovery.post(tenant, `commands/${c.id}/decision`, { approve, reason, digest: c.action_digest }))}>{approve ? "Approve exact action" : "Reject recovery"}</Button>)}</div></> : <p className="text-xs text-muted">A different recovery workspace owner must review this action.</p>}</div>}</div>)}</CardBody></Card>
    <Card><CardHeader title="Incident timeline" icon={<ShieldCheck />} description={detail.audit_valid ? "Encrypted evidence chain verified" : "Evidence verification unavailable"} /><CardBody><ol className="space-y-4">{detail.timeline.map(event => <li className="border-l-2 border-primary/30 pl-4" key={event.sequence}><div className="flex flex-wrap items-center justify-between gap-2 text-sm"><strong>{label(event.kind)}</strong><time className="text-xs text-muted">{date(event.at)}</time></div><details className="mt-1 text-xs"><summary className="cursor-pointer text-muted">Evidence #{event.sequence}</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-2 p-2">{JSON.stringify(event.data, null, 2)}</pre></details></li>)}</ol></CardBody></Card>
    {detail.related.length > 0 && <Card><CardHeader title="Correlated incidents" /><CardBody>{detail.related.map(r => <p className="mb-2 text-sm" key={r.id}>{r.title} · {label(r.status)}</p>)}</CardBody></Card>}
    {canWrite && <Card><CardHeader title="Evidence and postmortem" icon={<CheckCircle2 />} /><CardBody><form className="space-y-3" onSubmit={e => { e.preventDefault(); void act(async () => { await recovery.post(tenant, `incidents/${i.id}/notes`, { kind, note }); setNote(""); }); }}><select aria-label="Record type" className={field} value={kind} onChange={e => setKind(e.target.value)}><option value="evidence">Evidence note</option><option value="postmortem">Postmortem</option><option value="preventive_action">Preventive action</option></select><textarea aria-label="Incident note" required minLength={3} maxLength={4000} className={field} value={note} onChange={e => setNote(e.target.value)} placeholder="Record observations, evidence references, lessons and accountable next actions" /><Button type="submit" disabled={busy || note.trim().length < 3}>Save record</Button></form></CardBody></Card>}
  </div>;
}
