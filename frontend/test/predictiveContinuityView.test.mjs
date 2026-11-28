import assert from "node:assert/strict";
import test from "node:test";

import {
  allowedProposalActions,
  dueProcesses,
  filterRecordsByHorizon,
  findingLabel,
  formatMoneyTotals,
  latestPerRoot,
  proposalActionForFinding,
  proposalStatusLabel,
  reviewQueue,
  shortHash,
  summarizeRecord,
  trustBadge,
  uncoveredProcesses,
} from "../src/predictive-continuity/predictiveContinuityView.mjs";

const record = (overrides = {}) => ({
  id: "r-1",
  tenant_id: "t-1",
  root_id: "p-1",
  root_kind: "person",
  horizon_days: 7,
  fingerprint: "abc",
  as_of: "2026-11-28T10:00:00Z",
  superseded: false,
  drift_score: null,
  trust: "trustworthy",
  forecast: {
    processes: [
      { n: 1, id: "pr-1", name: "Pay suppliers", due_status: "due_within_horizon", reason: "no_backup_recorded" },
      { n: 2, id: "pr-2", name: "File taxes", due_status: "at_risk_later", reason: null },
      { n: 3, id: "pr-3", name: "Ship orders", due_status: "due_within_horizon", reason: null },
    ],
    counts: { processes: 3, processes_due_within_horizon: 2, affected: 4, uncovered: 1 },
    money_at_risk: { totals: { USD: 120.5 }, batch_count: 1, payment_count: 0 },
  },
  ...overrides,
});

test("trust badges map every band, unknown by default", () => {
  assert.deepEqual(trustBadge("trustworthy"), { tone: "success", label: "Trustworthy" });
  assert.deepEqual(trustBadge("review"), { tone: "warning", label: "Review" });
  assert.deepEqual(trustBadge("stale"), { tone: "danger", label: "Stale" });
  assert.deepEqual(trustBadge("unknown"), { tone: "neutral", label: "Unknown" });
  assert.deepEqual(trustBadge("bogus"), { tone: "neutral", label: "Unknown" });
  assert.deepEqual(trustBadge(null), { tone: "neutral", label: "Unknown" });
});

test("finding labels are plain language, unknown codes fall back to the code", () => {
  assert.equal(findingLabel("uncovered_process"), "Uncovered process");
  assert.equal(findingLabel("supplier_spof"), "Supplier single point of failure");
  assert.equal(findingLabel("forecasted_not_drilled"), "Forecasted, drill missed it");
  assert.equal(findingLabel("drilled_not_forecasted"), "Drilled, forecast missed it");
  assert.equal(findingLabel("some_new_code"), "some new code");
});

test("proposal action mapping admits only the three server pairs", () => {
  assert.equal(proposalActionForFinding("forecast", "uncovered_process"), "forecast.request_backup");
  assert.equal(proposalActionForFinding("forecast", "supplier_spof"), "forecast.request_supplier_review");
  assert.equal(proposalActionForFinding("calendar", "backup_overlap"), "forecast.request_backup_review");
  assert.equal(proposalActionForFinding("forecast", "backup_overlap"), null);
  assert.equal(proposalActionForFinding("forecast", "made_up"), null);
  assert.equal(proposalActionForFinding("twin", "uncovered_process"), null);
});

test("proposal buttons follow the approval flow and nothing else", () => {
  assert.deepEqual(allowedProposalActions("pending_approval"), { canDecide: true, canExecute: false, canCancel: true });
  assert.deepEqual(allowedProposalActions("approved"), { canDecide: false, canExecute: true, canCancel: false });
  assert.deepEqual(allowedProposalActions("draft"), { canDecide: false, canExecute: false, canCancel: true });
  for (const status of ["rejected", "expired", "cancelled", "dispatched", "verified", "failed"]) {
    assert.deepEqual(allowedProposalActions(status), { canDecide: false, canExecute: false, canCancel: false });
  }
});

test("proposal status labels cover the whole lifecycle", () => {
  assert.equal(proposalStatusLabel("pending_approval"), "Waiting for approval");
  assert.equal(proposalStatusLabel("approved"), "Approved");
  assert.equal(proposalStatusLabel("dispatched"), "Dispatched");
  assert.equal(proposalStatusLabel("verified"), "Verified");
  assert.equal(proposalStatusLabel("bogus_status"), "bogus status");
});

test("horizon filter keeps one horizon and hides superseded records by default", () => {
  const records = [
    record({ id: "r-1", horizon_days: 7 }),
    record({ id: "r-2", horizon_days: 30 }),
    record({ id: "r-3", horizon_days: 7, superseded: true }),
  ];
  assert.deepEqual(filterRecordsByHorizon(records, 7).map((r) => r.id), ["r-1"]);
  assert.deepEqual(filterRecordsByHorizon(records, 7, true).map((r) => r.id), ["r-1", "r-3"]);
  assert.deepEqual(filterRecordsByHorizon(records, 1), []);
  assert.deepEqual(filterRecordsByHorizon(null, 7), []);
});

test("standing forecast keeps the newest current record per root", () => {
  const records = [
    record({ id: "r-new", root_id: "p-1", created_at: "2026-11-28T10:00:00Z" }),
    record({ id: "r-old", root_id: "p-1", created_at: "2026-11-27T10:00:00Z" }),
    record({ id: "r-sys", root_id: "s-1", root_kind: "system", created_at: "2026-11-28T09:00:00Z" }),
    record({ id: "r-sup", root_id: "p-1", superseded: true, created_at: "2026-11-28T11:00:00Z" }),
  ];
  assert.deepEqual(latestPerRoot(records).map((r) => r.id), ["r-new", "r-sys"]);
});

test("due and uncovered processes come from the forecast, in order", () => {
  const forecast = record().forecast;
  assert.deepEqual(dueProcesses(forecast).map((p) => p.id), ["pr-1", "pr-3"]);
  assert.deepEqual(uncoveredProcesses(forecast).map((p) => p.id), ["pr-1"]);
  assert.deepEqual(dueProcesses(null), []);
  assert.deepEqual(uncoveredProcesses({}), []);
});

test("record summaries count only, never names or amounts", () => {
  assert.equal(summarizeRecord(record()), "3 processes · 2 due in 7 days · 1 uncovered");
  assert.equal(summarizeRecord(record({ horizon_days: 1, forecast: { counts: { processes: 1, processes_due_within_horizon: 0, uncovered: 0 } } })), "1 process · 0 due in 1 day · 0 uncovered");
  assert.equal(summarizeRecord(null), "0 processes · 0 due in 7 days · 0 uncovered");
});

test("money totals format per currency, hide redacted money, zero means none", () => {
  assert.equal(formatMoneyTotals({ totals: { USD: 120.5 }, batch_count: 1, payment_count: 0 }), "$120.50");
  // Intl separates the PKR code from the amount with a non-breaking space (U+00A0).
  assert.equal(formatMoneyTotals({ totals: { USD: 10, PKR: 5000 }, batch_count: 0, payment_count: 0 }), "$10.00 · PKR\u00A05,000");
  assert.equal(formatMoneyTotals({ totals: {}, batch_count: 0, payment_count: 0 }), "No money at risk");
  assert.equal(formatMoneyTotals({ totals: { USD: 0 }, batch_count: 0, payment_count: 0 }), "No money at risk");
  assert.equal(formatMoneyTotals(null), null);
  assert.equal(formatMoneyTotals(undefined), null);
  assert.equal(formatMoneyTotals({ totals: { unspecified: 42 } }), "42 unspecified");
});

test("review queue keeps only unacknowledged reports, newest first", () => {
  const items = [
    { id: "a", acknowledged: true, created_at: "2026-11-28T10:00:00Z" },
    { id: "b", acknowledged: false, created_at: "2026-11-27T10:00:00Z" },
    { id: "c", acknowledged: false, created_at: "2026-11-28T11:00:00Z" },
  ];
  assert.deepEqual(reviewQueue(items).map((item) => item.id), ["c", "b"]);
  assert.deepEqual(reviewQueue(null), []);
});

test("action hashes shorten for display but stay recognisable", () => {
  assert.equal(shortHash("abcdef0123456789abcdef"), "abcdef012345…");
  assert.equal(shortHash("short"), "short");
  assert.equal(shortHash(""), "");
  assert.equal(shortHash(null), "");
});
