import test from 'node:test';
import assert from 'node:assert/strict';

import { summarizeDrillComparison } from '../src/continuity/drillSummary.mjs';

test('clean drill comparison summarises as a match', () => {
  const summary = summarizeDrillComparison({ matches: true, differences: [], affected: 5, recovered: 5, simulated: 5 });
  assert.equal(summary.matches, true);
  assert.equal(summary.total, 0);
  assert.equal(summary.headline, 'Drill matches the simulation forecast.');
  assert.equal(summary.recovered, 5);
});

test('differences are counted by code with a readable headline', () => {
  const summary = summarizeDrillComparison({
    matches: false,
    affected: 5,
    recovered: 1,
    simulated: 5,
    differences: [
      { code: 'affected_not_recovered', resource_id: 'a', detail: 'x' },
      { code: 'affected_not_recovered', resource_id: 'b', detail: 'x' },
      { code: 'rto_missed_in_drill', resource_id: 'c', detail: 'x' },
      { code: 'drill_met_rto_simulation_missed', resource_id: 'c', detail: 'x' },
    ],
  });
  assert.equal(summary.matches, false);
  assert.equal(summary.total, 4);
  assert.deepEqual(summary.byCode, {
    affected_not_recovered: 2,
    rto_missed_in_drill: 1,
    drill_met_rto_simulation_missed: 1,
  });
  assert.equal(summary.headline, '4 differences from the forecast · 1 RTO miss · 2 resources not recovered');
});

test('missing comparison renders as no match without crashing', () => {
  const summary = summarizeDrillComparison(null);
  assert.equal(summary.matches, false);
  assert.equal(summary.total, 0);
  assert.equal(summary.headline, '0 differences from the forecast');
});
