import { useId, useMemo, useRef, useState } from "react";
import { Maximize, Minus, Plus } from "lucide-react";
import type { Graph, GraphRecord } from "./types";

const palette: Record<string, string> = { organization: "#7c83e4", business_unit: "#7c83e4", department: "#34a68c", employee: "#4994ca", system: "#c48a39", process: "#9c76c4", project: "#dc798a" };
const columns = ["organization", "business_unit", "department", "employee", "position", "role", "responsibility", "system", "asset", "process", "workflow", "project", "supplier", "customer"];

export function GraphMap({ graph, selected, onSelect, onExpand, highlighted = [], hierarchy = false }: {
  graph: Graph; selected?: string; onSelect: (r: GraphRecord) => void; onExpand: (id: string) => void; highlighted?: string[]; hierarchy?: boolean;
}) {
  const marker = useId().replace(/:/g, "");
  const svg = useRef<SVGSVGElement>(null);
  const drag = useRef<{x: number; y: number; ox: number; oy: number} | null>(null);
  const [view, setView] = useState({x: 20, y: 36, scale: 1});
  const layout = useMemo(() => {
    const groups = new Map<string, GraphRecord[]>();
    for (const n of graph.entities) { const key = hierarchy ? n.department_id ?? n.kind : n.kind; groups.set(key, [...groups.get(key) ?? [], n]); }
    const keys = [...groups.keys()].sort((a, b) => columns.indexOf(a) - columns.indexOf(b) || a.localeCompare(b));
    const positions = new Map<string, {x: number; y: number}>();
    keys.forEach((key, col) => groups.get(key)!.sort((a,b) => a.name.localeCompare(b.name)).forEach((n, row) => positions.set(n.id, {x: col * 245, y: row * 92})));
    return {positions, keys, width: Math.max(760, keys.length * 245), height: Math.max(420, ...[...groups.values()].map(g => g.length * 92 + 70))};
  }, [graph.entities, hierarchy]);
  function fit() {
    const box = svg.current?.getBoundingClientRect();
    if (box) setView({x: 20, y: 36, scale: Math.min(1.2, (box.width - 40) / layout.width, (box.height - 60) / layout.height)});
  }
  return <div className="cg-map" data-testid="company-map">
    <div className="cg-map-controls"><button aria-label="Zoom in" onClick={() => setView(v => ({...v, scale: Math.min(2.5, v.scale * 1.2)}))}><Plus size={16}/></button><button aria-label="Zoom out" onClick={() => setView(v => ({...v, scale: Math.max(.12, v.scale / 1.2)}))}><Minus size={16}/></button><button aria-label="Fit map" onClick={fit}><Maximize size={16}/></button><span>{Math.round(view.scale * 100)}%</span></div>
    <svg ref={svg} role="group" aria-label="Interactive company relationships" onWheel={e => { setView(v => ({...v, scale: Math.min(2.5, Math.max(.12, v.scale * (e.deltaY > 0 ? .92 : 1.08)))})); }}
      onPointerDown={e => { if (e.target === svg.current || (e.target as Element).tagName === "rect" && (e.target as Element).getAttribute("data-bg")) { drag.current = {x: e.clientX, y: e.clientY, ox: view.x, oy: view.y}; e.currentTarget.setPointerCapture(e.pointerId); } }}
      onPointerMove={e => { if (drag.current) setView(v => ({...v, x: drag.current!.ox + e.clientX - drag.current!.x, y: drag.current!.oy + e.clientY - drag.current!.y})); }}
      onPointerUp={() => { drag.current = null; }} onPointerCancel={() => {drag.current = null;}}>
      <defs><pattern id={`${marker}-grid`} width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="var(--border-strong)"/></pattern><marker id={marker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="var(--muted)"/></marker></defs>
      <rect data-bg="true" width="100%" height="100%" fill={`url(#${marker}-grid)`}/>
      <g transform={`translate(${view.x} ${view.y}) scale(${view.scale})`}>
        {layout.keys.map((key, i) => <text key={key} x={i * 245 + 8} y={-14} className="cg-map-column">{(graph.entities.find(n => n.id === key)?.name ?? key).replaceAll("_", " ")}</text>)}
        {graph.relationships.filter(e => !hierarchy || ["part_of", "reports_to", "holds_position", "has_role"].includes(e.kind)).map(edge => {
          const a = layout.positions.get(edge.source_id!), b = layout.positions.get(edge.target_id!);
          if (!a || !b) return null;
          const related = selected && [edge.source_id, edge.target_id].includes(selected);
          const x1 = a.x + 200, x2 = b.x, y1 = a.y + 30, y2 = b.y + 30;
          return <g key={edge.id} onClick={() => onSelect(edge)} className="cg-edge" role="button" tabIndex={0} aria-label={`${edge.kind.replaceAll("_", " ")}: ${graph.entities.find(n=>n.id===edge.source_id)?.name} to ${graph.entities.find(n=>n.id===edge.target_id)?.name}`} onKeyDown={e => {if(e.key === "Enter") onSelect(edge);}}>
            <path d={`M${x1},${y1} C${x1 + 35},${y1} ${x2 - 35},${y2} ${x2},${y2}`} fill="none" stroke={related ? "var(--primary)" : "var(--border-strong)"} strokeWidth={related ? 2.8 : 1.5} markerEnd={`url(#${marker})`} />
            {related && <text x={(x1+x2)/2} y={(y1+y2)/2-5}>{edge.kind.replaceAll("_", " ")}</text>}
          </g>;
        })}
        {graph.entities.map(node => {
          const p = layout.positions.get(node.id)!;
          return <g key={node.id} transform={`translate(${p.x},${p.y})`} className={`cg-node ${selected === node.id ? "selected" : ""} ${highlighted.includes(node.id) ? "affected" : ""}`} role="button" tabIndex={0} aria-label={`Inspect ${node.name}`} onClick={() => onSelect(node)} onDoubleClick={() => onExpand(node.id)} onKeyDown={e => {if(e.key === "Enter") onSelect(node);}}>
            <title>{node.name} · {node.kind} · {node.visibility}</title><rect width="200" height="62" rx="9"/><rect width="4" height="38" x="0" y="12" rx="2" style={{fill: palette[node.kind] ?? "#8e94a1", stroke: "none"}}/>
            <text x="14" y="25" className="cg-node-name">{node.name.length > 24 ? node.name.slice(0, 23) + "…" : node.name}</text><text x="14" y="45" className="cg-node-kind">{node.kind.replaceAll("_", " ")} · v{node.version}</text>
          </g>;
        })}
      </g>
    </svg>
    <div className="cg-map-help">Drag to pan · Scroll to zoom · Double-click to expand</div>
  </div>;
}
