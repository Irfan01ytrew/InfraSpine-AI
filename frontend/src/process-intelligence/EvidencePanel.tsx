import { X } from "lucide-react";
import type { Model } from "./types";
import { date } from "./api";

export function EvidencePanel({ model, ids, onClose }: { model: Model; ids: string[]; onClose: () => void }) {
  const rows = [...new Set(ids)].map(id => model.data.evidence[id]).filter(Boolean);
  return <section className="pi-card pi-evidence" aria-label="Evidence inspector">
    <div className="pi-card-head"><div><h2>Source evidence</h2><p>{rows.length} persisted events · current source permissions checked by the API</p></div><button aria-label="Close evidence" onClick={onClose}><X size={18} /></button></div>
    <div className="pi-evidence-list">{rows.map(e => <article key={e.id}>
      <div className="pi-row"><strong>{e.activity}</strong><span className="pi-badge">Observed</span></div>
      <p>{e.case_id} · {date(e.occurred_at)}</p><p>{e.actor || "Owner unrecorded"} · {e.department || "Department unrecorded"}</p>
      <dl><dt>Original record</dt><dd>{e.evidence_ref}</dd><dt>External event</dt><dd>{e.external_id}</dd><dt>Source</dt><dd>{model.data.sources.find(s => s.id === e.source_id)?.name ?? e.source_id}</dd></dl>
    </article>)}</div>
  </section>;
}
