import test from "node:test";
import assert from "node:assert/strict";
import {
  attentionCount,
  formatUsd,
  needsAttention,
  sortControls,
  spendPercent,
  spendStateLabel,
  statusLabel,
} from "../src/agent-controls/agentControlsView.mjs";

test("spendPercent returns null without a limit and clamps to 0–100", () => {
  assert.equal(spendPercent("5.0000", null), null);
  assert.equal(spendPercent("5.0000", "0.0000"), null);
  assert.equal(spendPercent("5.0000", "10.0000"), 50);
  assert.equal(spendPercent("15.0000", "10.0000"), 100);
  assert.equal(spendPercent("-1.0000", "10.0000"), 0);
});

test("spendStateLabel and statusLabel cover every backend value", () => {
  assert.equal(spendStateLabel("ok"), "Within budget");
  assert.equal(spendStateLabel("near_limit"), "Near limit");
  assert.equal(spendStateLabel("at_limit"), "At limit");
  assert.equal(spendStateLabel("over_limit"), "Over limit");
  assert.equal(statusLabel("active"), "Active");
  assert.equal(statusLabel("paused"), "Paused");
  assert.equal(statusLabel("disabled"), "Disabled");
  assert.equal(statusLabel("killed"), "Killed");
});

test("formatUsd shows the 4dp string and No limit for null", () => {
  assert.equal(formatUsd("12.3456"), "USD 12.3456");
  assert.equal(formatUsd(null), "No limit");
  assert.equal(formatUsd(undefined), "No limit");
});

test("needsAttention flags killed/disabled/paused and at/over limit, not near limit", () => {
  const base = { status: "active", spend_state: "ok", spend_used_usd: "1.0000", spend_limit_usd: "10.0000" };
  assert.equal(needsAttention(base), false);
  assert.equal(needsAttention({ ...base, spend_state: "near_limit" }), false);
  assert.equal(needsAttention({ ...base, spend_state: "at_limit" }), true);
  assert.equal(needsAttention({ ...base, spend_state: "over_limit" }), true);
  assert.equal(needsAttention({ ...base, status: "paused" }), true);
  assert.equal(needsAttention({ ...base, status: "disabled" }), true);
  assert.equal(needsAttention({ ...base, status: "killed" }), true);
  assert.equal(needsAttention(null), false);
});

test("attentionCount and sortControls put attention items first, then highest spend", () => {
  const ok = { status: "active", spend_state: "ok", spend_used_usd: "1.0000", spend_limit_usd: "10.0000" };
  const near = { status: "active", spend_state: "near_limit", spend_used_usd: "9.0000", spend_limit_usd: "10.0000" };
  const killed = { status: "killed", spend_state: "ok", spend_used_usd: "0.0000", spend_limit_usd: null };
  assert.equal(attentionCount([ok, near, killed]), 1);
  assert.deepEqual(sortControls([ok, near, killed]), [killed, near, ok]);
});
