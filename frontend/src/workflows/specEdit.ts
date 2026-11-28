import type { FlowNode, Kind, Spec } from './types';

/** Node kinds the builder palette offers. Moved here from FlowDiagram so the
 *  palette, the canvas, and the unit tests share one definition. */
export const nodeKinds: { kind: Kind; label: string; symbol: string }[] = [
  { kind: 'start', label: 'Start', symbol: '▶' }, { kind: 'task', label: 'Data task', symbol: '▤' },
  { kind: 'human', label: 'Human task', symbol: '◎' }, { kind: 'approval', label: 'Approval', symbol: '✓' },
  { kind: 'decision', label: 'Decision', symbol: '◇' }, { kind: 'parallel', label: 'Parallel / join', symbol: '⋔' },
  { kind: 'timer', label: 'Timer', symbol: '◷' }, { kind: 'event', label: 'Wait for event', symbol: 'ϟ' },
  { kind: 'agent', label: 'AI specialist', symbol: '✧' }, { kind: 'integration', label: 'Integration', symbol: '⇄' },
  { kind: 'subworkflow', label: 'Subworkflow', symbol: '▣' }, { kind: 'loop', label: 'Bounded loop', symbol: '↻' },
  { kind: 'end', label: 'End', symbol: '■' },
];

export function newNode(kind: Kind, x = 100, y = 100): FlowNode {
  return {
    id: kind + '_' + crypto.randomUUID().slice(0, 8), type: kind, label: nodeKinds.find(n => n.kind === kind)!.label, x, y,
    risk: kind === 'agent' || kind === 'integration' ? 'medium' : 'low', deadline_seconds: 86400, timeout_seconds: 60, max_attempts: 3,
    config: { values: {}, form: [], ...(kind === 'decision' ? { condition: { path: 'input.amount', operator: 'gte', value: 0 } } : {}) },
  };
}

export function arrange(spec: Spec): Spec {
  const ranks = new Map<string, number>(); const todo = [...spec.nodes];
  for (let pass = 0; pass < spec.nodes.length; pass++) for (const n of todo) {
    const parents = spec.edges.filter(e => e.target === n.id).map(e => e.source);
    if (parents.every(p => ranks.has(p))) ranks.set(n.id, parents.length ? Math.max(...parents.map(p => ranks.get(p)!)) + 1 : 0);
  }
  const counts = new Map<number, number>();
  return { ...spec, nodes: spec.nodes.map(n => { const r = ranks.get(n.id) ?? 0; const i = counts.get(r) ?? 0; counts.set(r, i + 1); return { ...n, x: 60 + r * 270, y: 70 + i * 150 }; }) };
}

/** Remove a node and every edge touching it. Returns the spec unchanged when
 *  the id is not in the graph, so a stray delete click is a no-op. */
export function deleteNode(spec: Spec, id: string): Spec {
  if (!spec.nodes.some(n => n.id === id)) return spec;
  return {
    ...spec,
    nodes: spec.nodes.filter(n => n.id !== id),
    edges: spec.edges.filter(e => e.source !== id && e.target !== id),
  };
}

/** Add an edge, unless it is a self-loop, names an unknown node, or already
 *  exists. Decision sources branch on 'true'; every other source is 'always'. */
export function connectNodes(spec: Spec, source: string, target: string): Spec {
  if (source === target) return spec;
  const sourceNode = spec.nodes.find(n => n.id === source);
  if (!sourceNode || !spec.nodes.some(n => n.id === target)) return spec;
  if (spec.edges.some(e => e.source === source && e.target === target)) return spec;
  return {
    ...spec,
    edges: [...spec.edges, { source, target, branch: sourceNode.type === 'decision' ? 'true' : 'always' }],
  };
}
