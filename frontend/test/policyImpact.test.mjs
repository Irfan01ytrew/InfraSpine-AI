import assert from "node:assert/strict";
import test from "node:test";

import { reassignableAssignees } from "../src/policyImpact.mjs";

test("reassignment choices exclude the current owner without mutating the source list", () => {
  const current = { user_id: "owner-1", email: "current@example.com" };
  const replacement = { user_id: "owner-2", email: "replacement@example.com" };
  const assignees = [current, replacement];

  assert.deepEqual(reassignableAssignees(assignees, current.user_id), [replacement]);
  assert.deepEqual(assignees, [current, replacement]);
});

test("reassignment choices are empty when no other assignee is eligible", () => {
  assert.deepEqual(reassignableAssignees([{ user_id: "owner-1" }], "owner-1"), []);
});
