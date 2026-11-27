import { useState, type FormEvent, type ReactNode } from 'react';
import type { Evidence, Resource } from './types';

export const localDate = (offset = 0) => { const date = new Date(Date.now() + offset); return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
export const dateText = (value?: string) => value ? new Date(value).toLocaleString() : 'Not recorded';
export const label = (value: string) => value.replaceAll('_', ' ');
export const names = (nodes: Resource[], ids: string[]) => ids.map(id => nodes.find(n => n.id === id)?.name ?? 'Restricted resource').join(', ');
export function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={'bc-badge ' + tone}>{children}</span>; }
export function Field({ label: text, children, wide = false }: { label: string; children: ReactNode; wide?: boolean }) { return <label className={wide ? 'bc-field bc-wide' : 'bc-field'}><span>{text}</span>{children}</label>; }
export function Empty({ children }: { children: ReactNode }) { return <div className="bc-empty">{children}</div>; }
export function ResourceOptions({ nodes }: { nodes: Resource[] }) { return <>{nodes.map(n => <option key={n.id} value={n.id}>{n.name} · {label(n.kind)}</option>)}</>; }
export function EvidenceFields({ reference, note, setReference, setNote }: { reference: string; note: string; setReference: (v: string) => void; setNote: (v: string) => void }) {
  return <><Field label="Evidence reference"><input required minLength={3} maxLength={400} value={reference} onChange={e => setReference(e.target.value)} placeholder="Approved SOP, review or incident record ID" /></Field><Field label="Evidence note"><input required minLength={3} maxLength={1000} value={note} onChange={e => setNote(e.target.value)} placeholder="What does this reference establish?" /></Field></>;
}
export const evidence = (reference: string, note: string): Evidence[] => [{ reference, note, observed_at: new Date().toISOString() }];
export function ActionForm({ title, submitLabel, onSubmit, children, className = '' }: { title: string; submitLabel: string; onSubmit: () => Promise<void>; children: ReactNode; className?: string }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [saved, setSaved] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); setBusy(true); setError(''); setSaved(false); try { await onSubmit(); setSaved(true); } catch (err) { setError(String((err as Error).message)); } finally { setBusy(false); } }
  return <form className={'bc-card bc-form ' + className} onSubmit={e => void submit(e)}><h3>{title}</h3><fieldset disabled={busy}><div className="bc-fields">{children}</div><div className="bc-form-actions"><button type="submit" className="bc-btn primary">{busy ? 'Saving…' : submitLabel}</button>{saved && <span role="status">Saved and refreshed</span>}</div></fieldset>{error && <div role="alert" className="bc-error">{error}</div>}</form>;
}
