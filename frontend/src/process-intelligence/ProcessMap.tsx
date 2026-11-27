import { useId, useMemo, useRef, useState } from "react";
import { Maximize2, Minus, Plus } from "lucide-react";
import type { Case, Model } from "./types";
import { hours } from "./api";

export function ProcessMap({ model, cases, onEvidence }: { model: Model; cases: Case[]; onEvidence: (ids: string[]) => void }) {
  const marker = useId().replace(/:/g, "");
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
  const visible = useMemo(() => new Set(cases.flatMap(c => c.event_ids)), [cases]);
  const nodes = model.data.nodes.filter(n => n.evidence_ids.some(id => visible.has(id)));
  const edges = model.data.edges.map(e => ({ ...e, pairs: e.evidence_pairs.filter(pair => pair.every(id => visible.has(id))) })).filter(e => e.pairs.length);
  const rank = new Map<string, number>();
  for (const c of cases) c.sequence.forEach((activity, i) => { if (!rank.has(activity)) rank.set(activity, i); });
  const buckets = new Map<number, number>();
  const positions = new Map(nodes.map(n => {
    const col = rank.get(n.activity) ?? 0; const row = buckets.get(col) ?? 0; buckets.set(col, row + 1);
    return [n.activity, { x: 40 + col * 250, y: 95 + row * 145 }];
  }));
  const width = Math.max(900, (Math.max(0, ...rank.values()) + 1) * 250 + 70);
  const height = Math.max(390, Math.max(0, ...buckets.values()) * 145 + 140);
  const approved = new Set(model.data.baseline?.sequences.flatMap(seq => seq.slice(1).map((to, i) => `${seq[i]}\0${to}`)) ?? []);
  return <section className="pi-card">
    <div className="pi-card-head"><div><h2>Discovered process</h2><p>Observed transitions · click an activity to inspect evidence</p></div><div className="pi-actions">
      <button aria-label="Zoom out" onClick={() => setZoom(z => Math.max(.4, z - .15))}><Minus size={15} /></button>
      <span aria-live="polite">{Math.round(zoom * 100)}%</span>
      <button aria-label="Zoom in" onClick={() => setZoom(z => Math.min(2.5, z + .15))}><Plus size={15} /></button>
      <button aria-label="Reset map" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }}><Maximize2 size={15} /></button>
    </div></div>
    {!nodes.length ? <p className="pi-empty">No events match this scope.</p> : <div className="pi-map" onWheel={e => { if (e.ctrlKey) { e.preventDefault(); setZoom(z => Math.min(2.5, Math.max(.4, z - e.deltaY * .002))); } }}>
      <svg aria-label="Interactive process discovery map" role="img" viewBox={`0 0 ${width} ${height}`} onPointerDown={e => { if ((e.target as Element).closest('[role="button"]')) return; drag.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y }; e.currentTarget.setPointerCapture(e.pointerId); }} onPointerMove={e => { if (drag.current) { const scale = width / e.currentTarget.getBoundingClientRect().width; setPan({ x: drag.current.px + (e.clientX - drag.current.x) * scale, y: drag.current.py + (e.clientY - drag.current.y) * scale }); } }} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
        <defs><marker id={marker} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" /></marker></defs>
        <g transform={`translate(${pan.x} ${pan.y}) scale(${zoom})`}>
          {edges.map(e => { const a = positions.get(e.from)!; const b = positions.get(e.to)!; const back = b.x <= a.x; const x1 = a.x + 190; const y1 = a.y + 35; const x2 = b.x; const y2 = b.y + 35; const d = back ? `M ${x1} ${y1} C ${x1 + 70} ${y1 - 125}, ${x2 - 70} ${y2 - 125}, ${x2} ${y2}` : `M ${x1} ${y1} C ${x1 + 40} ${y1}, ${x2 - 40} ${y2}, ${x2} ${y2}`;
            return <g key={`${e.from}-${e.to}`} className={approved.size && !approved.has(`${e.from}\0${e.to}`) ? "pi-edge pi-edge-extra" : "pi-edge"}><path d={d} fill="none" stroke="currentColor" strokeWidth={Math.min(6, 1 + Math.sqrt(e.pairs.length))} markerEnd={`url(#${marker})`} /><text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - (back ? 85 : 12)} textAnchor="middle">{e.pairs.length}×</text><title>{`${e.from} → ${e.to}: ${e.pairs.length} observations; full-model median interval ${hours(e.median_interval_hours)}`}</title></g>;
          })}
          {nodes.map(n => { const p = positions.get(n.activity)!; const ids = n.evidence_ids.filter(id => visible.has(id)); return <g key={n.activity} transform={`translate(${p.x} ${p.y})`} role="button" tabIndex={0} aria-label={`Inspect ${n.activity}`} onClick={() => onEvidence(ids)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onEvidence(ids); } }} className="pi-node">
            <rect width="190" height="76" rx="12" /><text x="14" y="28">{n.activity.length > 23 ? n.activity.slice(0, 21) + "…" : n.activity}</text><text x="14" y="53" className="pi-node-detail">{ids.length} events · {n.actors.length} owners</text><title>{n.activity}</title>
          </g>; })}
        </g>
      </svg>
    </div>}
    <div className="pi-map-key"><span className="pi-dot" />Observed transition{model.data.baseline && <><span className="pi-dot pi-dot-warn" />Outside reviewed baseline</>}<span>Drag to pan · +/− to zoom · tab to inspect</span></div>
  </section>;
}
