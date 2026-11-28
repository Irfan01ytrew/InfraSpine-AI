import { test } from "node:test";
import assert from "node:assert/strict";
import {
  filterAgents,
  formatDurationMs,
  formatMoneyUsd,
  sortAgents,
  statusLabel,
  statusTone,
  summarizeOrg,
} from "../src/workforce/workforceView.mjs";

const agents = [
  { agent_kind: "studio", agent_id: "a1", display_name: "Research Bot", effective_status: "active" },
  { agent_kind: "governance", agent_id: "a2", display_name: "Policy Checker", effective_status: "paused" },
  { agent_kind: "profile", agent_id: "a3", display_name: "Billing Helper", effective_status: "retired" },
];

test("statusTone maps lifecycle states", () => {
  assert.equal(statusTone("active"), "success");
  assert.equal(statusTone("paused"), "warning");
  assert.equal(statusTone("retired"), "neutral");
  assert.equal(statusTone("dead"), "danger");
  assert.equal(statusTone("weird"), "neutral");
});

test("statusLabel humanises states", () => {
  assert.equal(statusLabel("active"), "Active");
  assert.equal(statusLabel("paused"), "Paused");
  assert.equal(statusLabel(""), "Unknown");
});

test("filterAgents matches name, kind and id, and filters by status", () => {
  assert.equal(filterAgents(agents, "research", "").length, 1);
  assert.equal(filterAgents(agents, "GOVERNANCE", "").length, 1);
  assert.equal(filterAgents(agents, "a3", "").length, 1);
  assert.equal(filterAgents(agents, "", "paused").length, 1);
  assert.equal(filterAgents(agents, "bot", "paused").length, 0);
  assert.equal(filterAgents(agents, "", "").length, 3);
});

test("sortAgents sorts by name or by status then name", () => {
  assert.deepEqual(sortAgents(agents, "name").map((a) => a.display_name),
    ["Billing Helper", "Policy Checker", "Research Bot"]);
  assert.deepEqual(sortAgents(agents, "status").map((a) => a.display_name),
    ["Research Bot", "Policy Checker", "Billing Helper"]);
});

test("formatMoneyUsd formats decimals and renders null as an em dash", () => {
  assert.equal(formatMoneyUsd(10), "$10.00");
  assert.equal(formatMoneyUsd("2.5"), "$2.50");
  assert.equal(formatMoneyUsd(null), "—");
  assert.equal(formatMoneyUsd(undefined), "—");
});

test("formatDurationMs renders milliseconds and seconds", () => {
  assert.equal(formatDurationMs(500), "500 ms");
  assert.equal(formatDurationMs(1500), "1.5 s");
  assert.equal(formatDurationMs(null), "—");
});

test("summarizeOrg counts paused and blocked agents", () => {
  const summary = summarizeOrg({
    agents: [
      { effective_status: "active", can_start: true },
      { effective_status: "paused", can_start: false },
      { effective_status: "active", can_start: false },
    ],
    total_agents: 3, total_runs: 9, total_cost_usd: 4.5, success_rate: 0.8,
  });
  assert.equal(summary.totalAgents, 3);
  assert.equal(summary.totalRuns, 9);
  assert.equal(summary.totalCostUsd, 4.5);
  assert.equal(summary.successRate, 0.8);
  assert.equal(summary.pausedAgents, 1);
  assert.equal(summary.cannotStartAgents, 2);
});
