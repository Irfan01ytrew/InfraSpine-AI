import assert from "node:assert/strict";
import test from "node:test";

import { barPercent, relativeAge, statusBars } from "../src/control-room/chartData.mjs";

test("statusBars sorts by count and reports each share of the total", () => {
  const bars = statusBars({ pending: 3, verified: 10, exception: 2, paid: 5 });
  assert.deepEqual(
    bars.map((row) => row.status),
    ["verified", "paid", "pending", "exception"],
  );
  assert.equal(bars[0].count, 10);
  assert.equal(bars[0].share, 0.5);
  assert.equal(bars.reduce((sum, row) => sum + row.share, 0), 1);
});

test("statusBars ignores zero counts and handles an empty map", () => {
  assert.deepEqual(statusBars({ pending: 0, verified: 0 }), []);
  assert.deepEqual(statusBars({}), []);
  assert.deepEqual(statusBars(null), []);
});

test("barPercent scales to the tallest bar and never exceeds 100", () => {
  assert.equal(barPercent(50, 100), 50);
  assert.equal(barPercent(100, 100), 100);
  assert.equal(barPercent(150, 100), 100);
  assert.equal(barPercent(0, 100), 0);
  assert.equal(barPercent(10, 0), 0);
  assert.equal(barPercent(-5, 100), 0);
});

test("relativeAge describes health-strip ages in plain language", () => {
  assert.equal(relativeAge(null), "—");
  assert.equal(relativeAge(undefined), "—");
  assert.equal(relativeAge(-1), "—");
  assert.equal(relativeAge(0), "just now");
  assert.equal(relativeAge(45), "just now");
  assert.equal(relativeAge(300), "5m ago");
  assert.equal(relativeAge(3600), "1h ago");
  assert.equal(relativeAge(90000), "25h ago");
  assert.equal(relativeAge(180000), "2d ago");
});
