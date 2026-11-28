import test from "node:test";
import assert from "node:assert/strict";
import {
  attendanceTotals, balanceText, canApprove, canManage, currentMonth,
  leaveDaysTotal, leaveStatusCounts, monthLabel, upcomingReminders,
} from "../src/hr/hrView.mjs";

test("monthLabel formats YYYY-MM and passes junk through", () => {
  assert.equal(monthLabel("2026-11"), "November 2026");
  assert.equal(monthLabel("2026-01"), "January 2026");
  assert.equal(monthLabel("2026-13"), "2026-13");
  assert.equal(monthLabel("junk"), "junk");
});

test("leaveStatusCounts buckets known statuses and counts the rest as other", () => {
  const counts = leaveStatusCounts([
    { status: "pending", days: "1" }, { status: "pending", days: "2" },
    { status: "approved", days: "3" }, { status: "applied", days: "1" },
    { status: "weird", days: "1" },
  ]);
  assert.equal(counts.pending, 2);
  assert.equal(counts.approved, 1);
  assert.equal(counts.applied, 1);
  assert.equal(counts.other, 1);
  assert.equal(leaveStatusCounts(null).pending, 0);
});

test("leaveDaysTotal sums numeric day strings", () => {
  assert.equal(leaveDaysTotal([{ status: "applied", days: "2.5" }, { status: "pending", days: "1" }]), 3.5);
  assert.equal(leaveDaysTotal([]), 0);
});

test("balanceText reads remaining/entitled/used/pending", () => {
  assert.equal(
    balanceText({ remaining: 17, entitled: 20, used: 3, pending: 0 }),
    "17 of 20 days left · 3 used · 0 awaiting decision",
  );
  assert.equal(balanceText(null), "");
});

test("attendanceTotals merges present + work from home and computes a percentage", () => {
  const totals = attendanceTotals({
    business_days: 20, present: 14, work_from_home: 2, leave: 3, holiday: 0, unrecorded: 1,
  });
  assert.equal(totals.attended, 16);
  assert.equal(totals.leave, 3);
  assert.equal(totals.unrecorded, 1);
  assert.equal(totals.pct, 80);
  assert.equal(attendanceTotals(null).pct, 0);
});

test("upcomingReminders puts pending deliveries first, then earliest pay date", () => {
  const rows = upcomingReminders([
    { pay_date: "2026-11-20", pending_delivery: false },
    { pay_date: "2026-11-25", pending_delivery: true },
    { pay_date: "2026-11-15", pending_delivery: true },
  ]);
  assert.deepEqual(rows.map((r) => r.pay_date), ["2026-11-15", "2026-11-25", "2026-11-20"]);
});

test("canApprove / canManage read the permission list", () => {
  assert.equal(canApprove(["hr.leave.approve"]), true);
  assert.equal(canApprove(["hr.manage"]), true);
  assert.equal(canApprove(["hr.leave.request"]), false);
  assert.equal(canManage(["hr.manage"]), true);
  assert.equal(canManage(["hr.leave.approve"]), false);
});

test("currentMonth returns the current YYYY-MM", () => {
  const now = new Date();
  const expected = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  assert.equal(currentMonth(), expected);
});
