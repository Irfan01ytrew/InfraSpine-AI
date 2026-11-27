import { useState, type FormEvent } from "react";
import { graphApi } from "./api";
import { describeError } from "../api";
import type { GraphRecord, RecordInput } from "./types";

export function RecordEditor({ tenant, current, nodes, types, onSaved, onCancel }: { tenant: string; current?: GraphRecord; nodes: GraphRecord[]; types: {entity_types: string[]; relationship_types: string[]}; onSaved: () => void; onCancel: () => void }) {
  const [data, setData] = useState<RecordInput>(() => current ? Object.fromEntries(Object.entries(current).filter(([k]) => !["id", "version", "validation_status", "updated_at"].includes(k))) as unknown as RecordInput : {
    record_type: "entity", name: "", external_key: crypto.randomUUID(), kind: "system", source_id: null, target_id: null, department_id: null,
    visibility: "tenant", readers: [], attributes: {}, evidence: [], confidence: 1, inferred: false, retired: false,
    valid_from: new Date().toISOString(), valid_to: null, observed_at: new Date().toISOString(),
  });
  const [source, setSource] = useState(current?.evidence[0]?.source ?? "");
  const [reference, setReference] = useState(current?.evidence[0]?.reference ?? "");
  const [quote, setQuote] = useState(current?.evidence[0]?.quote ?? "");
  const [metadata, setMetadata] = useState(JSON.stringify(current?.attributes ?? {}, null, 2));
  const [readers, setReaders] = useState(current?.readers.join(", ") ?? "");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  function update<K extends keyof RecordInput>(key: K, value: RecordInput[K]) { setData(d => ({...d, [key]: value})); }
  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError("");
    try { const observed_at = new Date().toISOString();
      await graphApi.propose(tenant, {...data, attributes: JSON.parse(metadata), readers: readers.split(",").map(s=>s.trim()).filter(Boolean), observed_at,
        evidence: [{source, reference, quote, observed_at}, ...(current?.evidence.slice(1) ?? [])]}, current?.id, current?.version);
      onSaved();
    } catch(e) { setError(describeError(e)); } finally {setBusy(false);}
  }
  return <form className="cg-panel cg-editor" onSubmit={submit} aria-label="Graph record editor">
    <div className="cg-row"><h2>{current ? "Propose an update" : "Add to the company graph"}</h2><button type="button" onClick={onCancel}>Cancel</button></div>
    <p className="cg-muted">Changes become visible after an authorized reviewer approves the evidence.</p>
    {error && <div className="cg-error" role="alert">{error}</div>}
    <div className="cg-form-grid">
      <label>Record type<select value={data.record_type} disabled={!!current} onChange={e=>{update("record_type", e.target.value as RecordInput["record_type"]); update("kind", e.target.value === "entity" ? "system" : "depends_on");}}><option value="entity">Entity</option><option value="relationship">Relationship</option></select></label>
      <label>Type<input required list="cg-type-options" pattern="[a-z][a-z0-9_]{0,63}" disabled={!!current} value={data.kind} onChange={e=>update("kind",e.target.value)}/><datalist id="cg-type-options">{(data.record_type === "entity" ? types.entity_types : types.relationship_types).map(t=><option key={t} value={t}/>)}</datalist></label>
      <label>Name<input required maxLength={240} value={data.name} onChange={e=>update("name", e.target.value)}/></label>
      <label>External key<input required maxLength={220} disabled={!!current} value={data.external_key} onChange={e=>update("external_key", e.target.value)}/></label>
      {data.record_type === "relationship" && <>{(["source_id", "target_id"] as const).map((key,i)=><label key={key}>{i ? "Target entity" : "Source entity"}<select required disabled={!!current} value={data[key] ?? ""} onChange={e=>update(key, e.target.value || null)}><option value="">Choose entity</option>{nodes.map(n=><option key={n.id} value={n.id}>{n.name}</option>)}</select></label>)}<p className="cg-muted cg-span">depends_on: dependent → provider. owns / operates / supplies: provider → resource. backup_for: employee → resource.</p></>}
      <label>Department<select value={data.department_id ?? ""} onChange={e=>update("department_id",e.target.value || null)}><option value="">No department</option>{nodes.filter(n=>n.kind === "department").map(n=><option key={n.id} value={n.id}>{n.name}</option>)}</select></label>
      <label>Visibility<select value={data.visibility} onChange={e=>update("visibility",e.target.value as RecordInput["visibility"])}><option value="tenant">Company members</option><option value="department">Department members</option><option value="restricted">Named readers and owners</option></select></label>
      {data.visibility === "restricted" && <label className="cg-span">Reader user IDs, separated by commas<input value={readers} onChange={e=>setReaders(e.target.value)} placeholder="Owners can inspect all company graph records"/></label>}
      <label>Confidence<input type="number" min="0" max="1" step="0.01" value={data.confidence} onChange={e=>update("confidence",Number(e.target.value))}/></label>
      <label>Valid until (optional)<input type="datetime-local" value={data.valid_to ? data.valid_to.slice(0,16) : ""} onChange={e=>update("valid_to",e.target.value ? new Date(e.target.value).toISOString() : null)}/></label>
      <label>Evidence source<input required maxLength={160} value={source} onChange={e=>setSource(e.target.value)}/></label>
      <label>Source reference<input required maxLength={500} value={reference} onChange={e=>setReference(e.target.value)} placeholder="Document revision, source record or ticket"/></label>
      <label className="cg-span">Supporting evidence<textarea required maxLength={2000} rows={3} value={quote} onChange={e=>setQuote(e.target.value)}/></label>
      <label className="cg-span">Metadata (JSON object)<textarea rows={3} value={metadata} onChange={e=>setMetadata(e.target.value)}/></label>
      <label className="cg-check"><input type="checkbox" checked={data.inferred} onChange={e=>update("inferred",e.target.checked)}/> Inferred claim, subject to validation</label>
      {current && <label className="cg-check"><input type="checkbox" checked={data.retired} onChange={e=>update("retired",e.target.checked)}/> Retire this record</label>}
    </div>
    <button className="cg-primary" type="submit" disabled={busy}>{busy ? "Saving proposal…" : "Submit for approval"}</button>
  </form>;
}
