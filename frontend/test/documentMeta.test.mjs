import assert from "node:assert/strict";
import test from "node:test";

import {
  allTags,
  daysUntilExpiry,
  expiryBadgeText,
  expiryStatus,
  matchesLibraryQuery,
  matchesTag,
} from "../src/knowledge/documentMeta.mjs";

const TODAY = new Date("2026-11-28T12:00:00Z");

test("daysUntilExpiry counts whole UTC days and never shifts with timezone", () => {
  assert.equal(daysUntilExpiry("2026-11-28", TODAY), 0);
  assert.equal(daysUntilExpiry("2026-11-29", TODAY), 1);
  assert.equal(daysUntilExpiry("2026-12-28", TODAY), 30);
  assert.equal(daysUntilExpiry("2026-11-27", TODAY), -1);
  assert.equal(daysUntilExpiry(null, TODAY), null);
  assert.equal(daysUntilExpiry("not-a-date", TODAY), null);
  assert.equal(daysUntilExpiry("", TODAY), null);
});

test("expiryStatus flags expired, due-soon (30-day window) and ok", () => {
  assert.equal(expiryStatus("2026-11-27", TODAY), "expired");
  assert.equal(expiryStatus("2026-11-28", TODAY), "due_soon");
  assert.equal(expiryStatus("2026-12-28", TODAY), "due_soon");
  assert.equal(expiryStatus("2026-12-29", TODAY), "ok");
  assert.equal(expiryStatus(null, TODAY), null);
  assert.equal(expiryStatus("2026-12-05", TODAY, 7), "due_soon");
  assert.equal(expiryStatus("2026-12-05", TODAY, 5), "ok");
});

test("expiryBadgeText names the state and the date", () => {
  assert.equal(expiryBadgeText("2026-11-27", TODAY), "Expired 27 Nov 2026");
  assert.equal(expiryBadgeText("2026-11-28", TODAY), "Expires today");
  assert.equal(expiryBadgeText("2026-11-29", TODAY), "Expires tomorrow");
  assert.equal(expiryBadgeText("2026-12-03", TODAY), "Expires in 5 days (3 Dec 2026)");
  assert.equal(expiryBadgeText("2027-06-01", TODAY), "Expires 1 Jun 2027");
  assert.equal(expiryBadgeText(null, TODAY), null);
});

test("allTags collects sorted unique tags and ignores missing ones", () => {
  const docs = [
    { tags: ["contract", "finance"] },
    { tags: ["contract"] },
    { tags: [] },
    {},
    { tags: null },
  ];
  assert.deepEqual(allTags(docs), ["contract", "finance"]);
  assert.deepEqual(allTags([]), []);
  assert.deepEqual(allTags(null), []);
});

test("matchesTag passes everything on 'all' and only tagged docs otherwise", () => {
  const doc = { tags: ["contract", "legal"] };
  assert.equal(matchesTag(doc, "all"), true);
  assert.equal(matchesTag(doc, "contract"), true);
  assert.equal(matchesTag(doc, "invoice"), false);
  assert.equal(matchesTag({}, "contract"), false);
});

test("matchesLibraryQuery searches filename, tags and summary", () => {
  const doc = { filename: "Vendor Agreement.pdf", tags: ["contract", "vendor"], summary: "Payment is due net 30." };
  assert.equal(matchesLibraryQuery(doc, ""), true);
  assert.equal(matchesLibraryQuery(doc, "vendor"), true);
  assert.equal(matchesLibraryQuery(doc, "CONTRACT"), true);
  assert.equal(matchesLibraryQuery(doc, "net 30"), true);
  assert.equal(matchesLibraryQuery(doc, "agreement"), true);
  assert.equal(matchesLibraryQuery(doc, "invoice"), false);
});
