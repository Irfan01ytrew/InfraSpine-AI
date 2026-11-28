import assert from "node:assert/strict";
import test from "node:test";

import {
  agentStatus,
  blockerTitle,
  canControlAgent,
  canControlRun,
  controlLabel,
  formatTokens,
  pendingKey,
  runRollup,
  sortAgents,
} from "../src/control-room/controlRoomView.mjs";

test("agentStatus ranks attention first", () => {
  assert.equal(agentStatus({ enabled: false, paused: false, latest_run: null }), "disabled");
  assert.equal(agentStatus({ enabled: true, paused: true, latest_run: { status: "running" } }), "paused");
  assert.equal(agentStatus({ enabled: true, paused: false, latest_run: { status: "failed" } }), "failed");
  assert.equal(agentStatus({ enabled: true, paused: false, latest_run: { status: "running" } }), "running");
  assert.equal(agentStatus({ enabled: true, paused: false, latest_run: { status: "queued" } }), "queued");
  assert.equal(agentStatus({ enabled: true, paused: false, latest_run: { status: "completed" } }), "ready");
  assert.equal(agentStatus({ enabled: true, paused: false, latest_run: null }), "idle");
  assert.equal(agentStatus(null), "idle");
  assert.equal(agentStatus({ enabled: true, paused: false, latest_run: { status: "cancelled" } }), "ready");
});

test("sortAgents puts paused and failed agents first, then by name", () => {
  const agents = [
    { name: "zeta", enabled: true, paused: false, latest_run: { status: "completed" } },
    { name: "alpha", enabled: true, paused: false, latest_run: null },
    { name: "mid", enabled: true, paused: true, latest_run: null },
    { name: "beta", enabled: true, paused: false, latest_run: { status: "failed" } },
    { name: "off", enabled: false, paused: false, latest_run: null },
  ];
  assert.deepEqual(sortAgents(agents).map((a) => a.name), ["mid", "beta", "off", "zeta", "alpha"]);
  assert.deepEqual(sortAgents(null), []);
  // The input order is not mutated.
  assert.equal(agents[0].name, "zeta");
});

test("runRollup totals every status and groups active runs", () => {
  assert.deepEqual(
    runRollup({ queued: 2, running: 1, paused: 3, failed: 1, completed: 5, cancelled: 1, compensating: 1 }),
    { total: 14, failed: 1, paused: 3, active: 4, completed: 5 },
  );
  assert.deepEqual(runRollup({}), { total: 0, failed: 0, paused: 0, active: 0, completed: 0 });
  assert.deepEqual(runRollup(null), { total: 0, failed: 0, paused: 0, active: 0, completed: 0 });
  assert.deepEqual(runRollup({ weird: 2 }), { total: 2, failed: 0, paused: 0, active: 0, completed: 0 });
});

test("blockerTitle names the three backend blocker kinds", () => {
  assert.equal(blockerTitle({ kind: "run_paused" }), "Paused run");
  assert.equal(blockerTitle({ kind: "run_awaiting_approval" }), "Waiting for approval");
  assert.equal(blockerTitle({ kind: "run_failed" }), "Failed run");
  assert.equal(blockerTitle({ kind: "something_new" }), "Needs attention");
  assert.equal(blockerTitle(null), "Needs attention");
});

test("canControlRun mirrors the backend command states", () => {
  assert.equal(canControlRun({ status: "queued" }, "pause"), true);
  assert.equal(canControlRun({ status: "running" }, "pause"), true);
  assert.equal(canControlRun({ status: "paused" }, "pause"), false);
  assert.equal(canControlRun({ status: "failed" }, "pause"), false);
  assert.equal(canControlRun({ status: "paused" }, "resume"), true);
  // The backend also resumes failed runs (resume from the last verified step).
  assert.equal(canControlRun({ status: "failed" }, "resume"), true);
  assert.equal(canControlRun({ status: "completed" }, "resume"), false);
  assert.equal(canControlRun(null, "pause"), false);
  assert.equal(canControlRun({ status: "running" }, "cancel"), false);
});

test("canControlAgent gates pause/resume on the paused flag", () => {
  const live = { enabled: true, paused: false };
  const paused = { enabled: true, paused: true };
  const disabled = { enabled: false, paused: false };
  assert.equal(canControlAgent(live, "pause"), true);
  assert.equal(canControlAgent(paused, "pause"), false);
  assert.equal(canControlAgent(disabled, "pause"), false);
  assert.equal(canControlAgent(live, "resume"), false);
  assert.equal(canControlAgent(paused, "resume"), true);
  assert.equal(canControlAgent(live, "assign"), true);
  assert.equal(canControlAgent(null, "pause"), false);
  assert.equal(canControlAgent(live, "stop"), false);
});

test("controlLabel and pendingKey are stable", () => {
  assert.equal(controlLabel("pause", "run"), "Pause run");
  assert.equal(controlLabel("resume", "agent"), "Resume agent");
  assert.equal(controlLabel("assign", "agent"), "Reassign agent");
  assert.equal(pendingKey("run", "abc"), "run:abc");
  assert.equal(pendingKey("agent", "abc"), "agent:abc");
});

test("formatTokens groups thousands and never throws", () => {
  assert.equal(formatTokens(1234), "1,234");
  assert.equal(formatTokens(0), "0");
  assert.equal(formatTokens(null), "0");
  assert.equal(formatTokens("nope"), "0");
});
