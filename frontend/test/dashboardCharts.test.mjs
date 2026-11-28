import assert from "node:assert/strict";
import test from "node:test";

import { donutSegments, fillRunDays, shortModuleName, topModules } from "../src/lib/dashboardCharts.mjs";

test("fillRunDays fills quiet days with zeros and keeps oldest-first order", () => {
  const today = new Date(2026, 10, 28);
  const days = fillRunDays(3, [
    { date: "2026-11-28", completed: 4, failed: 1 },
    { date: "2026-11-26", completed: 2, failed: 0 },
  ], today);
  assert.equal(days.length, 3);
  assert.deepEqual(days.map((d) => d.day), ["2026-11-26", "2026-11-27", "2026-11-28"]);
  assert.deepEqual(days[1], { day: "2026-11-27", label: days[1].label, completed: 0, failed: 0 });
  assert.equal(days[2].completed, 4);
  assert.equal(days[2].failed, 1);
  assert.ok(days[0].label.length > 0);
});

test("fillRunDays clamps negative and non-numeric counts to zero", () => {
  const days = fillRunDays(1, [{ date: "2026-11-28", completed: -3, failed: "oops" }], new Date(2026, 10, 28));
  assert.deepEqual([days[0].completed, days[0].failed], [0, 0]);
});

test("donutSegments fractions sum to 1 and offsets accumulate", () => {
  const { total, segments } = donutSegments([
    { key: "completed", label: "Completed", value: 8, color: "var(--success)" },
    { key: "failed", label: "Failed", value: 2, color: "var(--danger)" },
  ]);
  assert.equal(total, 10);
  assert.equal(segments.length, 2);
  const sum = segments.reduce((acc, s) => acc + s.fraction, 0);
  assert.ok(Math.abs(sum - 1) < 1e-9);
  assert.equal(segments[0].fraction, 0.8);
  assert.equal(segments[1].offset, -0.8 * 2 * Math.PI * 56);
  assert.ok(segments[0].dash.startsWith("281."));
});

test("donutSegments yields no segments for a zero total", () => {
  assert.deepEqual(donutSegments([]), { total: 0, segments: [] });
  assert.deepEqual(donutSegments([{ key: "x", label: "X", value: 0, color: "red" }]), { total: 0, segments: [] });
});

test("donutSegments gives a single part the whole circle", () => {
  const { segments } = donutSegments([{ key: "only", label: "Only", value: 5, color: "red" }]);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].fraction, 1);
  assert.equal(segments[0].offset, 0);
});

test("topModules ranks by total runs, caps the list and shares against the max", () => {
  const top = topModules([
    { id: "a", name: "InfraSpine Module 01 — Knowledge & AI Brain", total_runs: 10, completed: 9, failed: 1 },
    { id: "b", name: "InfraSpine Module 02 — IRFANIX AI Agent", total_runs: 4, completed: 4, failed: 0 },
    { id: "c", name: "Quiet module", total_runs: 0, completed: 0, failed: 0 },
  ], 2);
  assert.equal(top.length, 2);
  assert.equal(top[0].name, "Knowledge & AI Brain");
  assert.equal(top[0].share, 1);
  assert.equal(top[1].share, 0.4);
  assert.ok(!top.some((m) => m.name === "Quiet module"));
});

test("topModules returns an empty list when nothing ran", () => {
  assert.deepEqual(topModules([]), []);
  assert.deepEqual(topModules([{ id: "a", name: "A", total_runs: 0, completed: 0, failed: 0 }]), []);
});

test("shortModuleName strips the module prefix and leaves other names alone", () => {
  assert.equal(shortModuleName("InfraSpine Module 12 — QA & Benchmarking"), "QA & Benchmarking");
  assert.equal(shortModuleName("Custom agent"), "Custom agent");
});
