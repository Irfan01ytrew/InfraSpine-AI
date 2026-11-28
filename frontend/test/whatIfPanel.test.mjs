import assert from "node:assert/strict";
import test from "node:test";

import {
  comparisonHeadline,
  nextPendingStep,
  rehearsalProgress,
  seedOptions,
  summarizeSimulation,
} from "../src/whatif/whatIfPanel.mjs";

test("seed options list visible entities sorted by name and skip relationships", () => {
  const graph = {
    entities: [
      { id: "e-2", name: "Payroll", kind: "process", record_type: "entity" },
      { id: "e-1", name: "ERP", kind: "system", record_type: "entity" },
      { id: "r-1", name: "depends", kind: "depends_on", record_type: "relationship" },
    ],
  };
  assert.deepEqual(seedOptions(graph), [
    { id: "e-1", name: "ERP", kind: "system" },
    { id: "e-2", name: "Payroll", kind: "process" },
  ]);
  assert.deepEqual(seedOptions(null), []);
  assert.deepEqual(seedOptions({}), []);
});

test("simulation summary counts waves and formats money at risk", () => {
  const summary = summarizeSimulation({
    affected: [{}, {}, {}],
    failed: [{}],
    degraded: [{}],
    recovery_plan: { waves: [[{ action: "restore_from_backup" }], [{ action: "manual_workaround" }]], blocked_by_dependency_cycle: [] },
    money_at_risk: { totals: { PKR: 1500 }, batch_count: 1 },
  });
  assert.equal(summary.affectedCount, 3);
  assert.equal(summary.failedCount, 1);
  assert.equal(summary.degradedCount, 1);
  assert.equal(summary.waveCount, 2);
  assert.equal(summary.stepCount, 2);
  assert.equal(summary.moneyText, "PKR 1,500 at risk across 1 batch(es)");
});

test("simulation summary says no money at risk when totals are empty", () => {
  const summary = summarizeSimulation({
    affected: [], failed: [], degraded: [],
    recovery_plan: { waves: [], blocked_by_dependency_cycle: [{ id: "x" }] },
    money_at_risk: { totals: {}, batch_count: 0 },
  });
  assert.equal(summary.moneyText, "No money at risk");
  assert.equal(summary.blockedCount, 1);
});

test("rehearsal progress counts statuses and reports when all steps are done", () => {
  const rehearsal = {
    steps: [
      { status: "done" },
      { status: "skipped" },
      { status: "pending" },
    ],
  };
  const progress = rehearsalProgress(rehearsal);
  assert.deepEqual(
    { total: progress.total, done: progress.done, skipped: progress.skipped, pending: progress.pending, allDone: progress.allDone },
    { total: 3, done: 1, skipped: 1, pending: 1, allDone: false },
  );
  assert.equal(nextPendingStep(rehearsal).status, "pending");
  const finished = rehearsalProgress({ steps: [{ status: "done" }, { status: "skipped" }] });
  assert.equal(finished.allDone, true);
  assert.equal(nextPendingStep({ steps: [{ status: "done" }] }), null);
});

test("drill comparison headline covers full and partial coverage", () => {
  assert.equal(
    comparisonHeadline({ summary: { simulated_count: 3, missing_count: 0, coverage: 1, drill_verified_rate: 1 }, missing: [] }),
    "The drill covered all 3 predicted step(s); 100% of drill steps were verified.",
  );
  assert.equal(
    comparisonHeadline({ summary: { simulated_count: 3, missing_count: 1, coverage: 0.66, drill_verified_rate: 0.5 }, missing: [{ key: "erp" }] }),
    "The drill covered 66% of predicted steps (1 predicted step(s) missing); 50% of drill steps were verified.",
  );
});
