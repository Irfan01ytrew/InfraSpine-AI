import test from 'node:test';
import assert from 'node:assert/strict';
import { arrange, connectNodes, deleteNode, newNode, nodeKinds } from '../src/workflows/specEdit.ts';

const spec = () => ({
  nodes: [
    { id: 'start', type: 'start', label: 'Start', x: 0, y: 0, config: { values: {}, form: [] } },
    { id: 'task', type: 'task', label: 'Do it', x: 300, y: 0, config: { values: {}, form: [] } },
    { id: 'end', type: 'end', label: 'End', x: 600, y: 0, config: { values: {}, form: [] } },
  ],
  edges: [
    { source: 'start', target: 'task', branch: 'always' },
    { source: 'task', target: 'end', branch: 'always' },
  ],
});

test('newNode creates a palette node with safe defaults', () => {
  const node = newNode('approval', 120, 80);
  assert.equal(node.type, 'approval');
  assert.equal(node.label, 'Approval');
  assert.equal(node.x, 120);
  assert.equal(node.y, 80);
  assert.equal(node.risk, 'low');
  assert.match(node.id, /^approval_[0-9a-f-]{8}$/);
  assert.notEqual(newNode('task').id, newNode('task').id);
});

test('connectNodes adds an edge, decision sources branch on true', () => {
  const connected = connectNodes(spec(), 'start', 'end');
  assert.equal(connected.edges.length, 3);
  assert.equal(connected.edges.at(-1).branch, 'always');
  const decision = spec();
  decision.nodes.push({ id: 'gate', type: 'decision', label: 'Gate', x: 900, y: 0, config: { values: {}, form: [], condition: { path: 'input.amount', operator: 'gte', value: 0 } } });
  assert.equal(connectNodes(decision, 'gate', 'end').edges.at(-1).branch, 'true');
});

test('connectNodes refuses self-loops, duplicates and unknown nodes', () => {
  const base = spec();
  assert.equal(connectNodes(base, 'start', 'start').edges.length, 2);
  assert.equal(connectNodes(base, 'start', 'task').edges.length, 2);
  assert.equal(connectNodes(base, 'start', 'ghost').edges.length, 2);
  assert.equal(connectNodes(base, 'ghost', 'end').edges.length, 2);
});

test('deleteNode removes the node and every edge touching it', () => {
  const pruned = deleteNode(spec(), 'task');
  assert.deepEqual(pruned.nodes.map(n => n.id), ['start', 'end']);
  assert.deepEqual(pruned.edges, []);
});

test('deleteNode is a no-op for an unknown id', () => {
  const base = spec();
  assert.equal(deleteNode(base, 'ghost'), base);
});

test('arrange lays nodes out in rank order', () => {
  const arranged = arrange(spec());
  const byId = Object.fromEntries(arranged.nodes.map(n => [n.id, n]));
  assert.ok(byId.start.x < byId.task.x && byId.task.x < byId.end.x);
});

test('nodeKinds covers every node type the canvas supports', () => {
  const kinds = new Set(nodeKinds.map(n => n.kind));
  for (const kind of ['start', 'end', 'task', 'human', 'approval', 'decision', 'parallel', 'timer', 'event', 'agent', 'integration', 'subworkflow', 'loop']) {
    assert.ok(kinds.has(kind), `missing ${kind}`);
  }
});
