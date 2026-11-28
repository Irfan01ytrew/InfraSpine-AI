import assert from "node:assert/strict";
import test from "node:test";

import {
  filingOutcomeLabel,
  incidentKindLabel,
  isAccessError,
  severityTone,
  sortEscalations,
  statusTone,
} from "../src/self-healing/selfHealing.mjs";

test("severity maps to badge tones, critical first", () => {
  assert.equal(severityTone("critical"), "danger");
  assert.equal(severityTone("high"), "warning");
  assert.equal(severityTone("medium"), "primary");
  assert.equal(severityTone("low"), "neutral");
  assert.equal(severityTone("weird"), "neutral");
});

test("status maps to badge tones", () => {
  assert.equal(statusTone("open"), "danger");
  assert.equal(statusTone("escalated"), "warning");
  assert.equal(statusTone("remedied"), "success");
});

test("incident kinds have human labels and unknown kinds keep their key", () => {
  assert.equal(incidentKindLabel("api_down"), "API down");
  assert.equal(incidentKindLabel("self_test_failed"), "Self-test failed");
  assert.equal(incidentKindLabel("something_new"), "something_new");
});

test("filing outcomes have human labels", () => {
  assert.equal(filingOutcomeLabel("incident"), "Recovery incident filed");
  assert.equal(filingOutcomeLabel("pending_threshold"), "Filed, below the recovery threshold");
  assert.equal(filingOutcomeLabel("duplicate"), "Already filed");
  assert.equal(filingOutcomeLabel("no_target"), "No recovery target in that workspace");
});

test("only 403/404 are access errors", () => {
  assert.equal(isAccessError(403), true);
  assert.equal(isAccessError(404), true);
  assert.equal(isAccessError(500), false);
  assert.equal(isAccessError(undefined), false);
});

test("unacknowledged escalations come first, then newest first", () => {
  const items = [
    { incident_key: "old-ack", acknowledged_at: "2026-01-01T00:00:00Z", delivered_at: "2026-01-01T00:00:00Z" },
    { incident_key: "new-open", acknowledged_at: null, delivered_at: "2026-02-01T00:00:00Z" },
    { incident_key: "old-open", acknowledged_at: null, delivered_at: "2026-01-15T00:00:00Z" },
  ];
  assert.deepEqual(
    sortEscalations(items).map((item) => item.incident_key),
    ["new-open", "old-open", "old-ack"],
  );
});
