import assert from "node:assert/strict";
import test from "node:test";

import {
  bankLabel,
  needsAttentionCount,
  pendingApprovals,
} from "../src/integrations/connectorsView.mjs";

const JOBS = [
  { id: "1", status: "completed", operation: "slack.send" },
  { id: "2", status: "pending_approval", operation: "email.send" },
  { id: "3", status: "dead_letter", operation: "teams.send" },
  { id: "4", status: "pending_approval", operation: "erp.submit" },
  { id: "5", status: "uncertain", operation: "bank.transfer" },
];

test("pending approvals are only the jobs awaiting a human decision", () => {
  assert.deepEqual(
    pendingApprovals(JOBS).map((job) => job.id),
    ["2", "4"],
  );
  assert.deepEqual(pendingApprovals([]), []);
  assert.deepEqual(pendingApprovals(null), []);
  assert.deepEqual(pendingApprovals(undefined), []);
});

test("needs-attention counts dead letters, uncertain outcomes and pending approvals", () => {
  assert.equal(needsAttentionCount(JOBS), 4);
  assert.equal(needsAttentionCount([]), 0);
  assert.equal(needsAttentionCount(null), 0);
});

test("bank label names the active connector, its source and its mode", () => {
  const label = bankLabel({
    active: "http+mock",
    available: true,
    source: "admin",
    admin_mode: "http+mock",
    environment_mode: "mock",
    http_configured: true,
    http_host: "bank.example.test",
    is_mock: false,
    real_bank: true,
    problem: null,
  });
  assert.equal(label.title, "http+mock");
  assert.equal(
    label.detail,
    "Source: admin · admin mode http+mock · real bank · bank.example.test",
  );
  assert.equal(label.tone, "success");
  assert.equal(label.state, "available");
});

test("bank label falls back to the environment mode and reports mock plainly", () => {
  const label = bankLabel({
    active: "mock",
    available: true,
    source: "environment",
    admin_mode: null,
    environment_mode: "mock",
    http_configured: false,
    http_host: null,
    is_mock: true,
    real_bank: false,
    problem: null,
  });
  assert.equal(label.title, "mock");
  assert.equal(
    label.detail,
    "Source: environment · environment mock · mock (no real bank)",
  );
});

test("bank label reports an unavailable connector as danger", () => {
  const label = bankLabel({
    active: null,
    available: false,
    source: "environment",
    admin_mode: null,
    environment_mode: "http",
    http_configured: false,
    http_host: null,
    is_mock: false,
    real_bank: true,
    problem: "No payment connector is configured",
  });
  assert.equal(label.title, "Not configured");
  assert.equal(label.tone, "danger");
  assert.equal(label.state, "unavailable");
  assert.equal(bankLabel(null), null);
});
