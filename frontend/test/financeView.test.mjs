import assert from "node:assert/strict";
import test from "node:test";

import { actionableStep, anomalyFlags, canApproveAnomaly, flagDetailLines, headline, isAnomalyFlagged, reasonCounts, shortHash, skippedSummary, spendSeries } from "../src/finance/financeView.mjs";

test("headline reads like the founder scenario", () => {
  assert.equal(headline({ verified: 32, total: 50 }), "32 of 50 verified");
  assert.equal(headline({ verified: 0, total: 0 }), "No invoices yet");
});

test("reason counts are grouped and sorted", () => {
  const invoices = [
    { reasons: [{ code: "po_missing" }] },
    { reasons: [{ code: "duplicate_invoice" }, { code: "po_missing" }] },
    { reasons: [] },
  ];
  assert.deepEqual(reasonCounts(invoices), [{ code: "po_missing", count: 2 }, { code: "duplicate_invoice", count: 1 }]);
});

test("spend series stacks departments per month for one currency", () => {
  const spend = {
    months: ["2026-10", "2026-11"],
    rows: [
      { month: "2026-11", department: "Operations", currency: "PKR", amount: "100.50", count: 1 },
      { month: "2026-11", department: "Facilities", currency: "PKR", amount: "50.25", count: 1 },
      { month: "2026-11", department: "Facilities", currency: "USD", amount: "9.00", count: 1 },
    ],
  };
  const result = spendSeries(spend, "PKR");
  assert.deepEqual(result.departments, ["Facilities", "Operations"]);
  assert.equal(result.series[0].total, 0);
  assert.equal(result.series[1].total, 150.75);
  assert.equal(result.max, 150.75);
});

test("skipped Knowledge documents are summarised plainly", () => {
  assert.equal(skippedSummary([]), "");
  assert.equal(skippedSummary(null), "");
  assert.equal(
    skippedSummary([{ filename: "old.pdf", reason: "Wait until the current document version is indexed." }]),
    "old.pdf: Wait until the current document version is indexed.",
  );
});

test("only the step the person may perform is offered", () => {
  const run = { status: "running", steps: [{ id: "1", node_id: "verify", status: "action" }, { id: "2", node_id: "pay", status: "blocked" }] };
  assert.equal(actionableStep(run, ["invoice.verify"]).id, "1");
  assert.equal(actionableStep(run, ["funds.transfer"]), null);
  assert.equal(actionableStep({ ...run, status: "paused" }, ["invoice.verify"]), null);
  assert.equal(shortHash("a".repeat(64)), "aaaaaa…aaaaaa");
});

test("anomaly queue helpers separate advisory flags from deterministic reasons", () => {
  const flagged = { reasons: [
    { code: "near_duplicate", message: "Same vendor, amount and date", details: { amount: "40000.00", prior_id: "x" } },
    { code: "first_time_payee", message: "No previous invoice", details: {} },
  ] };
  const mixed = { reasons: [{ code: "near_duplicate", message: "dup", details: {} }, { code: "po_missing", message: "No PO", details: {} }] };
  const held = { reasons: [{ code: "bank_account_changed", message: "changed", details: { previous_account: "********0001" } }, { code: "payee_verification_hold", message: "held", details: {} }] };
  const deterministic = { reasons: [{ code: "po_missing", message: "No PO", details: {} }] };
  assert.deepEqual(anomalyFlags(flagged).map((f) => f.code), ["near_duplicate", "first_time_payee"]);
  assert.equal(isAnomalyFlagged(flagged), true);
  assert.equal(isAnomalyFlagged(deterministic), false);
  assert.equal(isAnomalyFlagged({ reasons: [] }), false);
  assert.equal(isAnomalyFlagged(null), false);
  assert.equal(canApproveAnomaly(flagged), true);
  assert.equal(canApproveAnomaly(held), true);
  assert.equal(canApproveAnomaly(mixed), false);
  assert.equal(canApproveAnomaly(deterministic), false);
  assert.equal(canApproveAnomaly({ reasons: [] }), false);
  assert.deepEqual(flagDetailLines(flagged.reasons[0]), ["amount: 40000.00", "prior_id: x"]);
  assert.deepEqual(flagDetailLines(flagged.reasons[1]), []);
  assert.deepEqual(flagDetailLines(null), []);
});
