import assert from "node:assert/strict";
import test from "node:test";

import {
  OFFLINE_STATUS_KEY,
  countByStatus,
  createStatusRecord,
  deriveItems,
  dismissStatusRecord,
  dropReasonLabel,
  kindLabel,
  loadStatusRecords,
  pruneStatusRecords,
  recordDropped,
  recordSynced,
  saveStatusRecords,
  statusLabel,
  upsertStatusRecords,
} from "../src/lib/offlineStatus.mjs";

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
}

const NOW = 1_800_000_000_000;

function entry(id, kind = "knowledge-question", createdAt = NOW) {
  return { id, kind, method: "POST", url: "/api/knowledge/chat", body: {}, idempotencyKey: `key-${id}`, createdAt, attempts: 0 };
}

test("labels name every kind, status and drop reason in plain language", () => {
  assert.equal(kindLabel("knowledge-question"), "Knowledge question");
  assert.equal(kindLabel("draft-save"), "Draft saved");
  assert.equal(kindLabel("note-create"), "Note");
  assert.equal(kindLabel("something-else"), "something-else");
  assert.equal(statusLabel("queued"), "Queued");
  assert.equal(statusLabel("syncing"), "Syncing…");
  assert.equal(statusLabel("synced"), "Synced");
  assert.equal(statusLabel("dropped"), "Dropped");
  assert.equal(dropReasonLabel("expired"), "Expired — older than 24 hours, never sent");
  assert.equal(dropReasonLabel("conflict"), "Changed on the server — the server version was kept");
  assert.equal(dropReasonLabel("rejected"), "Rejected by the server");
  assert.equal(dropReasonLabel("max-attempts"), "Could not reach the server after 3 tries");
});

test("load/save round-trips records and ignores corrupt storage", () => {
  const storage = memoryStorage();
  assert.deepEqual(loadStatusRecords(storage), []);
  const records = [createStatusRecord(entry("a"), "synced", null, NOW)];
  saveStatusRecords(storage, records);
  assert.deepEqual(loadStatusRecords(storage), records);
  assert.equal(storage.getItem(OFFLINE_STATUS_KEY).includes("synced"), true);

  const broken = memoryStorage();
  broken.setItem(OFFLINE_STATUS_KEY, "not json{");
  assert.deepEqual(loadStatusRecords(broken), []);
  broken.setItem(OFFLINE_STATUS_KEY, JSON.stringify({ not: "an array" }));
  assert.deepEqual(loadStatusRecords(broken), []);
  broken.setItem(OFFLINE_STATUS_KEY, JSON.stringify([{ id: "x" }, records[0]]));
  assert.deepEqual(loadStatusRecords(broken), [records[0]]);
});

test("recordSynced and recordDropped upsert by id, never duplicate", () => {
  const first = recordSynced([], [entry("a"), entry("b")], NOW);
  assert.equal(first.length, 2);
  assert.equal(first[0].status, "synced");
  assert.equal(first[0].reason, null);
  const again = recordSynced(first, [entry("a")], NOW + 1000);
  assert.equal(again.length, 2);
  assert.equal(again.find((r) => r.id === "a").updatedAt, NOW + 1000);

  const dropped = recordDropped(again, [{ ...entry("b"), dropReason: "conflict" }], NOW + 2000);
  assert.equal(dropped.length, 2);
  const b = dropped.find((r) => r.id === "b");
  assert.equal(b.status, "dropped");
  assert.equal(b.reason, "conflict");
  // A dropped entry with no reason is recorded as rejected, never blank.
  const noReason = recordDropped([], [entry("c")], NOW);
  assert.equal(noReason[0].reason, "rejected");
});

test("pruneStatusRecords drops old records and caps at the newest 20", () => {
  const old = Array.from({ length: 25 }, (_, i) =>
    createStatusRecord(entry(`r${i}`), "synced", null, NOW - i * 1000),
  );
  const pruned = pruneStatusRecords(old, NOW);
  assert.equal(pruned.length, 20);
  assert.equal(pruned[0].id, "r0");
  assert.equal(pruned[19].id, "r19");

  const stale = [createStatusRecord(entry("old"), "dropped", "expired", NOW - 8 * 24 * 60 * 60 * 1000)];
  assert.deepEqual(pruneStatusRecords(stale, NOW), []);
  const edge = [createStatusRecord(entry("edge"), "synced", null, NOW - 7 * 24 * 60 * 60 * 1000)];
  assert.equal(pruneStatusRecords(edge, NOW).length, 1);
});

test("deriveItems shows live entries as queued, syncing ones as syncing", () => {
  const live = [entry("a", "draft-save"), entry("b", "note-create")];
  const items = deriveItems(live, [], new Set(["b"]));
  assert.deepEqual(
    items.map((i) => [i.id, i.status]),
    [
      ["a", "queued"],
      ["b", "syncing"],
    ],
  );
  assert.equal(items[0].reason, null);
});

test("deriveItems never shows a live entry as synced or dropped", () => {
  const live = [entry("a")];
  const records = [
    createStatusRecord(entry("a"), "synced", null, NOW + 5000),
    createStatusRecord(entry("gone"), "dropped", "expired", NOW + 4000),
  ];
  const items = deriveItems(live, records);
  // Newest first: the dropped record (NOW + 4000) sorts before the live entry (NOW).
  assert.deepEqual(
    items.map((i) => [i.id, i.status]),
    [
      ["gone", "dropped"],
      ["a", "queued"],
    ],
  );
  assert.equal(items[0].reason, "expired");
});

test("deriveItems orders newest first and countByStatus counts every status", () => {
  const live = [entry("a", "knowledge-question", NOW - 2000)];
  const records = [
    createStatusRecord(entry("b"), "synced", null, NOW - 1000),
    createStatusRecord(entry("c"), "dropped", "conflict", NOW),
  ];
  const items = deriveItems(live, records);
  assert.deepEqual(
    items.map((i) => i.id),
    ["c", "b", "a"],
  );
  assert.deepEqual(countByStatus(items), { queued: 1, syncing: 0, synced: 1, dropped: 1, total: 3 });
  assert.deepEqual(countByStatus([]), { queued: 0, syncing: 0, synced: 0, dropped: 0, total: 0 });
});

test("dismissStatusRecord removes one record and leaves the rest", () => {
  const records = [
    createStatusRecord(entry("a"), "synced", null, NOW),
    createStatusRecord(entry("b"), "dropped", "rejected", NOW),
  ];
  assert.deepEqual(
    dismissStatusRecord(records, "a").map((r) => r.id),
    ["b"],
  );
  assert.equal(dismissStatusRecord(records, "missing").length, 2);
});

test("upsertStatusRecords merges updates over existing records", () => {
  const base = [createStatusRecord(entry("a"), "synced", null, NOW)];
  const merged = upsertStatusRecords(base, [{ ...base[0], status: "dropped", reason: "conflict", updatedAt: NOW + 9 }]);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].status, "dropped");
  assert.equal(merged[0].reason, "conflict");
});
