import test from 'node:test';
import assert from 'node:assert/strict';
import { templateDraftState, templateSummary } from '../src/workflows/templatePicker.ts';

const template = () => ({
  key: 'invoice-approval',
  name: 'Invoice approve karwao',
  description: 'Approve an invoice with a recorded decision.',
  category: 'finance',
  spec: {
    nodes: [
      { id: 'start', type: 'start', label: 'Start', x: 0, y: 0, config: {} },
      { id: 'verify', type: 'task', label: 'Verify invoice', x: 200, y: 0, config: {} },
      { id: 'approve', type: 'approval', label: 'Approve payment', x: 400, y: 0, config: {} },
      { id: 'end', type: 'end', label: 'End', x: 600, y: 0, config: {} },
    ],
    edges: [
      { source: 'start', target: 'verify', branch: 'always' },
      { source: 'verify', target: 'approve', branch: 'always' },
      { source: 'approve', target: 'end', branch: 'always' },
    ],
    input_form: [
      { name: 'invoice_number', label: 'Invoice number', type: 'text', required: true },
      { name: 'amount', label: 'Amount', type: 'number', required: true },
      { name: 'note', label: 'Note', type: 'text', required: false },
    ],
  },
});

test('templateSummary lists category, steps and required inputs only', () => {
  const summary = templateSummary(template());
  assert.equal(summary.key, 'invoice-approval');
  assert.equal(summary.category, 'finance');
  assert.equal(summary.stepCount, 4);
  assert.deepEqual(summary.stepLabels, ['Start', 'Verify invoice', 'Approve payment', 'End']);
  assert.deepEqual(summary.requiredInputs, ['Invoice number', 'Amount']);
});

test('templateSummary defaults a missing category to operations', () => {
  const bare = template();
  delete bare.category;
  assert.equal(templateSummary(bare).category, 'operations');
});

test('templateSummary tolerates a template with no nodes or inputs', () => {
  const empty = template();
  empty.spec = { nodes: [], edges: [] };
  const summary = templateSummary(empty);
  assert.equal(summary.stepCount, 0);
  assert.deepEqual(summary.stepLabels, []);
  assert.deepEqual(summary.requiredInputs, []);
});

test('templateDraftState opens the created draft in the canvas, inert', () => {
  const result = {
    workflow: { id: 'wf-1', scope_id: 's-1', owner_id: 'u-1', name: 'Invoice approve karwao', description: '', latest_version: 1, active_version: null, enabled: false, updated_at: '2026-11-28T00:00:00Z' },
    version: { id: 'v-1', number: 1, status: 'draft', author_id: 'u-1', digest: 'abc', spec: template().spec, created_at: '2026-11-28T00:00:00Z' },
  };
  const state = templateDraftState(result);
  assert.equal(state.spec.nodes.length, 4);
  assert.deepEqual(state.errors, []);
  assert.equal(state.requiresReview, true);
  assert.equal(state.workflowId, 'wf-1');
  assert.equal(state.baseVersion, 1);
});
