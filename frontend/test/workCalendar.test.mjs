import assert from "node:assert/strict";
import test from "node:test";

import { createMonthGrid, groupCalendarItems, localDayKey } from "../src/workCalendar.mjs";

test("month grid starts Monday, includes the month, and always has six weeks", () => {
  const grid = createMonthGrid(new Date(2026, 9, 26));

  assert.equal(grid.length, 42);
  assert.equal(grid[0].getDay(), 1);
  assert.equal(localDayKey(grid[0]), "2026-09-28");
  assert.equal(grid[grid.length - 1].getDay(), 0);
  assert.ok(grid.some((date) => localDayKey(date) === "2026-10-01"));
  assert.ok(grid.some((date) => localDayKey(date) === "2026-10-31"));
});

test("month grid includes leap day and does not mutate its anchor", () => {
  const anchor = new Date(2024, 1, 14);
  const before = anchor.valueOf();
  const grid = createMonthGrid(anchor);

  assert.equal(anchor.valueOf(), before);
  assert.ok(grid.some((date) => localDayKey(date) === "2024-02-29"));
});

test("calendar grouping uses local due dates and ignores absent or invalid dates", () => {
  const due = new Date(2026, 9, 27, 10, 30).toISOString();
  const items = [
    { id: "a", due_at: due },
    { id: "b", due_at: due },
    { id: "none", due_at: null },
    { id: "bad", due_at: "not-a-date" },
  ];
  const grouped = groupCalendarItems(items);

  assert.deepEqual([...grouped.keys()], ["2026-10-27"]);
  assert.deepEqual(grouped.get("2026-10-27").map((item) => item.id), ["a", "b"]);
});
