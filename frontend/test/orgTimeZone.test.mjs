import assert from "node:assert/strict";
import test from "node:test";

import { DEFAULT_ORG_TIME_ZONE, formatOrgDay, zoneOptions } from "../src/orgTimeZone.mjs";

test("pinned zones come first and the current zone is always offered", () => {
  const options = zoneOptions(["Europe/Paris", "UTC", "Asia/Karachi"], "Asia/Tokyo");
  assert.equal(options[0], "Asia/Tokyo");
  assert.deepEqual(options.slice(1, 3), ["Asia/Karachi", "UTC"]);
  assert.ok(options.includes("Europe/Paris"));
  assert.equal(new Set(options).size, options.length);
});

test("falls back to the default zone when the server list is unavailable", () => {
  assert.deepEqual(zoneOptions(undefined, undefined), [DEFAULT_ORG_TIME_ZONE]);
  assert.deepEqual(zoneOptions([], "Asia/Karachi"), ["Asia/Karachi"]);
});

test("formats the organisation day without shifting it by the browser zone", () => {
  assert.match(formatOrgDay("2026-11-26"), /^Thu,? 26 Nov 2026$/);
  assert.equal(formatOrgDay("bad"), "");
});
