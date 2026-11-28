import assert from "node:assert/strict";
import test from "node:test";

import {
  AJ_REFRESH_MS,
  healthTone,
  overallStatusLabel,
  overallStatusTone,
  progressLabel,
  severityTone,
  timeAgo,
} from "../src/continuity/ajDashboard.mjs";

test("dashboard refreshes every 30 seconds by default (Step AF polling)", () => {
  assert.equal(AJ_REFRESH_MS, 30000);
});

test("overall status maps to alert tones and plain-language labels", () => {
  assert.equal(overallStatusTone("critical"), "danger");
  assert.equal(overallStatusTone("attention"), "warning");
  assert.equal(overallStatusTone("ok"), "success");
  assert.equal(overallStatusTone("anything-else"), "success");
  assert.equal(overallStatusLabel("critical"), "Needs attention now");
  assert.equal(overallStatusLabel("attention"), "Needs attention");
  assert.equal(overallStatusLabel("ok"), "All clear");
});

test("severity maps worst-first to badge tones", () => {
  assert.equal(severityTone("critical"), "danger");
  assert.equal(severityTone("high"), "warning");
  assert.equal(severityTone("medium"), "primary");
  assert.equal(severityTone("low"), "neutral");
  assert.equal(severityTone("unknown"), "neutral");
});

test("recovery progress label shows percent or 'No steps'", () => {
  assert.equal(progressLabel(62.5), "62.5%");
  assert.equal(progressLabel(0), "0%");
  assert.equal(progressLabel(100), "100%");
  assert.equal(progressLabel(null), "No steps");
  assert.equal(progressLabel(undefined), "No steps");
});

test("health roll-up status maps to alert tones", () => {
  assert.equal(healthTone("down"), "danger");
  assert.equal(healthTone("degraded"), "warning");
  assert.equal(healthTone("ok"), "success");
});

test("timeAgo renders relative times and tolerates bad input", () => {
  const now = Date.parse("2026-11-28T12:00:00.000Z");
  assert.equal(timeAgo("2026-11-28T11:59:58.000Z", now), "just now");
  assert.equal(timeAgo("2026-11-28T11:59:15.000Z", now), "45s ago");
  assert.equal(timeAgo("2026-11-28T11:57:00.000Z", now), "3m ago");
  assert.equal(timeAgo("2026-11-28T10:00:00.000Z", now), "2h ago");
  assert.equal(timeAgo("2026-11-27T12:00:00.000Z", now), "1d ago");
  assert.equal(timeAgo("not-a-date", now), "");
  assert.equal(timeAgo(undefined, now), "");
  // A future timestamp never goes negative.
  assert.equal(timeAgo("2026-11-28T12:05:00.000Z", now), "just now");
});
