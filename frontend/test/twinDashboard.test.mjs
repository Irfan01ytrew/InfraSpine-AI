import assert from "node:assert/strict";
import test from "node:test";

import {
  findingTone,
  gaugeSegments,
  healthBand,
  inboxRows,
  maskMoney,
  proposalStatusTone,
  queueSummary,
  shortFingerprint,
  timelineRows,
  trustBand,
} from "../src/digital-twin/twinDashboard.mjs";

test("health and trust bands match the backend thresholds", () => {
  assert.equal(healthBand(100), "healthy");
  assert.equal(healthBand(75), "healthy");
  assert.equal(healthBand(74), "degraded");
  assert.equal(healthBand(40), "degraded");
  assert.equal(healthBand(39), "critical");
  assert.equal(healthBand(null), "unknown");
  assert.equal(trustBand(75), "trustworthy");
  assert.equal(trustBand(40), "review");
  assert.equal(trustBand(39), "stale");
  assert.equal(trustBand(null), "unknown");
});

test("gauge segments fill the bands in order", () => {
  const segments = gaugeSegments(82);
  assert.deepEqual(segments.map((s) => s.filled), [40, 35, 7]);
  assert.deepEqual(gaugeSegments(0).map((s) => s.filled), [0, 0, 0]);
  assert.deepEqual(gaugeSegments(40).map((s) => s.filled), [40, 0, 0]);
  assert.deepEqual(gaugeSegments(150).map((s) => s.filled), [40, 35, 25]);
});

test("timeline rows are newest first with a first-version label", () => {
  const versions = [
    { seq: 1, fingerprint: "aaa", as_of: "2026-11-27T00:00:00Z", drift_score: null, trust: null, stale_sources: [], is_first_version: true },
    { seq: 2, fingerprint: "bbb", as_of: "2026-11-28T00:00:00Z", drift_score: 11, trust: "trustworthy", stale_sources: ["finance"], is_first_version: false },
  ];
  const rows = timelineRows(versions);
  assert.equal(rows[0].fingerprint, "bbb");
  assert.equal(rows[0].label, "Drift 11");
  assert.deepEqual(rows[0].staleSources, ["finance"]);
  assert.equal(rows[1].label, "First version — no drift recorded yet");
  assert.deepEqual(timelineRows(null), []);
});

test("inbox rows put pending approval first, then approved", () => {
  const proposals = [
    { id: "p-3", status: "executed", action_type: "twin.request_backup", approver_class: "coverage", required_permission: "company_graph.role.approver", finding: { code: "affected_not_recovered", severity: "critical", detail: "d" }, money_redacted: false, action_hash: "h3", created_at: "2026-11-28T02:00:00Z" },
    { id: "p-1", status: "approved", action_type: "twin.request_money_review", approver_class: "owner", required_permission: "company_graph.role.owner", finding: { code: "money_mismatch", severity: "warning", detail: "d" }, money_redacted: true, action_hash: "h1", created_at: "2026-11-28T01:00:00Z" },
    { id: "p-2", status: "pending_approval", action_type: "twin.request_drill_review", approver_class: "coverage", required_permission: "company_graph.role.approver", finding: { code: "rto_missed_in_drill", severity: "critical", detail: "d" }, money_redacted: false, action_hash: "h2", created_at: "2026-11-28T03:00:00Z" },
  ];
  const rows = inboxRows(proposals);
  assert.deepEqual(rows.map((r) => r.id), ["p-2", "p-1", "p-3"]);
  assert.equal(rows[0].canDecide, true);
  assert.equal(rows[0].canExecute, false);
  assert.equal(rows[1].canDecide, false);
  assert.equal(rows[1].canExecute, true);
  assert.equal(rows[2].canDecide, false);
  assert.equal(rows[1].moneyRedacted, true);
  assert.equal(rows[1].presentedHash, "h1");
});

test("status and finding tones", () => {
  assert.equal(proposalStatusTone("pending_approval"), "warning");
  assert.equal(proposalStatusTone("approved"), "success");
  assert.equal(proposalStatusTone("executed"), "success");
  assert.equal(proposalStatusTone("rejected"), "neutral");
  assert.equal(proposalStatusTone("other"), "neutral");
  assert.equal(findingTone("critical"), "danger");
  assert.equal(findingTone("warning"), "warning");
  assert.equal(findingTone("info"), "neutral");
});

test("queue summary is counts only", () => {
  assert.equal(queueSummary({ total: 0, unacknowledged: 0, unacknowledged_critical: 0 }), "Review queue is empty");
  assert.equal(
    queueSummary({ total: 5, unacknowledged: 3, unacknowledged_critical: 1 }),
    "5 finding(s) · 3 unacknowledged · 1 critical unacknowledged",
  );
  assert.equal(queueSummary(null), "No review queue loaded");
});

test("money is masked for non-owners and shown for owners", () => {
  assert.equal(maskMoney({ PKR: 20000 }, false), "<redacted>");
  assert.equal(maskMoney({ PKR: 20000 }, true), "PKR 20000");
  assert.equal(maskMoney(null, true), "—");
  assert.equal(maskMoney(42, true), "42");
});

test("short fingerprint truncates long hashes only", () => {
  assert.equal(shortFingerprint("a".repeat(64)), `${"a".repeat(12)}…`);
  assert.equal(shortFingerprint("abc"), "abc");
  assert.equal(shortFingerprint(null), "—");
});
