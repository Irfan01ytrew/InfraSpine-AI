import assert from "node:assert/strict";
import test from "node:test";

import { reviewState, reviewStateLabel, sortDue } from "../src/decisions/reviewState.mjs";

test("reviewState buckets by UTC calendar day", () => {
  assert.equal(reviewState("2026-11-20", "2026-11-28"), "overdue");
  assert.equal(reviewState("2026-11-28", "2026-11-28"), "due");
  assert.equal(reviewState("2026-12-05", "2026-11-28"), "due");
  assert.equal(reviewState("2026-12-06", "2026-11-28"), "ok");
  assert.equal(reviewState("2027-05-27", "2026-11-28"), "ok");
  assert.equal(reviewState(null, "2026-11-28"), "none");
  assert.equal(reviewState("not-a-date", "2026-11-28"), "none");
  assert.equal(reviewState("2026-11-28", "not-a-date"), "none");
});

test("reviewState honours a custom window", () => {
  assert.equal(reviewState("2026-12-20", "2026-11-28", 30), "due");
  assert.equal(reviewState("2026-12-20", "2026-11-28", 7), "ok");
  assert.equal(reviewState("2026-11-28", "2026-11-28", 0), "due");
  assert.equal(reviewState("2026-11-29", "2026-11-28", 0), "ok");
});

test("reviewState accepts datetimes and ignores the time part", () => {
  assert.equal(reviewState("2026-11-28T23:59:00+00:00", "2026-11-28"), "due");
  assert.equal(reviewState("2026-11-27T00:01:00+00:00", "2026-11-28"), "overdue");
});

test("reviewStateLabel reads plainly", () => {
  assert.equal(reviewStateLabel("overdue"), "Review overdue");
  assert.equal(reviewStateLabel("due"), "Review due soon");
  assert.equal(reviewStateLabel("ok"), "Review scheduled");
  assert.equal(reviewStateLabel("none"), "No review date");
});

test("sortDue orders most overdue first, then newest decided", () => {
  const rows = [
    { id: "c", review_at: "2026-12-01", decided_at: "2026-11-20T09:00:00+00:00" },
    { id: "a", review_at: "2026-11-18", decided_at: "2026-11-08T09:00:00+00:00" },
    { id: "b", review_at: "2026-11-18", decided_at: "2026-11-10T09:00:00+00:00" },
    { id: "d", review_at: null, decided_at: "2026-11-22T09:00:00+00:00" },
  ];
  assert.deepEqual(sortDue(rows).map((row) => row.id), ["b", "a", "c", "d"]);
  assert.deepEqual(rows.map((row) => row.id), ["c", "a", "b", "d"]);
});
