import assert from "node:assert/strict";
import test from "node:test";

import {
  OFFLINE_QUEUE_KEY,
  OFFLINE_QUEUE_MAX_AGE_MS,
  createEntry,
  createOfflineQueue,
  dropExpired,
  enqueue,
  isQueueable,
  loadQueue,
  queueCount,
  replayQueue,
} from "../src/lib/offlineQueue.mjs";

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
}

const NOW = 1_800_000_000_000;

test("safe kinds are queueable, privileged kinds are not", () => {
  assert.equal(isQueueable("knowledge-question", "POST", "/api/knowledge/chat"), true);
  assert.equal(isQueueable("draft-save", "POST", "/api/drafts"), true);
  assert.equal(isQueueable("note-create", "POST", "/api/notes"), true);
  // Privileged kinds are never queueable, whatever the URL says.
  assert.equal(isQueueable("approval", "POST", "/api/knowledge/chat"), false);
  assert.equal(isQueueable("payment", "POST", "/api/finance/pay"), false);
  assert.equal(isQueueable("workflow-run", "POST", "/api/agent-studio/runs"), false);
  // Reads are never queued.
  assert.equal(isQueueable("knowledge-question", "GET", "/api/knowledge/chat"), false);
});

test("denied URLs are never queueable even for an allowed kind", () => {
  const denied = [
    "/api/auth/login",
    "/api/approvals/123/decide",
    "/api/approval/123",
    "/api/finance/pay",
    "/api/finance/payment/123",
    "/api/finance/transfer",
    "/api/agent-studio/runs/abc/decision",
    "/api/agent-studio/runs/abc/cancel",
    "/api/workflows/abc/run",
    "/api/admin/settings",
    "/api/onboarding/accept",
    "https://evil.example/api/knowledge/chat",
  ];
  for (const url of denied) {
    assert.equal(isQueueable("knowledge-question", "POST", url), false, url);
  }
});

test("createEntry stamps an idempotency key and refuses unsafe input", () => {
  const entry = createEntry(
    { kind: "knowledge-question", method: "POST", url: "/api/knowledge/chat", body: { q: "hi" } },
    NOW,
  );
  assert.ok(entry);
  assert.equal(entry.createdAt, NOW);
  assert.equal(entry.attempts, 0);
  assert.ok(entry.idempotencyKey);
  assert.notEqual(entry.idempotencyKey, entry.id);

  assert.equal(
    createEntry({ kind: "approval", method: "POST", url: "/api/approvals/1/decide" }, NOW),
    null,
  );
});

test("entries persist, reload, and survive corrupt storage", () => {
  const storage = memoryStorage();
  assert.deepEqual(loadQueue(storage), []);
  const entry = createEntry(
    { kind: "draft-save", method: "POST", url: "/api/drafts", body: { t: "x" } },
    NOW,
  );
  enqueue(storage, entry);
  assert.equal(queueCount(storage), 1);
  assert.deepEqual(loadQueue(storage)[0].id, entry.id);

  storage.setItem(OFFLINE_QUEUE_KEY, "{not json");
  assert.deepEqual(loadQueue(storage), []);
  storage.setItem(OFFLINE_QUEUE_KEY, JSON.stringify({ not: "an array" }));
  assert.deepEqual(loadQueue(storage), []);
});

test("expired entries are dropped, fresh ones kept", () => {
  const fresh = createEntry(
    { kind: "note-create", method: "POST", url: "/api/notes", body: {} },
    NOW,
  );
  const stale = createEntry(
    { kind: "note-create", method: "POST", url: "/api/notes", body: {} },
    NOW - OFFLINE_QUEUE_MAX_AGE_MS - 1000,
  );
  const { kept, dropped } = dropExpired([fresh, stale], NOW);
  assert.deepEqual(kept.map((e) => e.id), [fresh.id]);
  assert.equal(dropped[0].dropReason, "expired");
});

test("replay syncs in order with the idempotency key and same-origin cookies", async () => {
  const storage = memoryStorage();
  const queue = createOfflineQueue(storage, async (url, init) => {
    assert.equal(init.credentials, "same-origin");
    assert.ok(init.headers["Idempotency-Key"]);
    return { ok: true, status: 200 };
  });
  queue.enqueue({ kind: "knowledge-question", method: "POST", url: "/api/knowledge/chat", body: { q: "a" } }, NOW);
  queue.enqueue({ kind: "draft-save", method: "POST", url: "/api/drafts", body: { t: "b" } }, NOW);
  const summary = await queue.replay(NOW);
  assert.equal(summary.synced.length, 2);
  assert.equal(summary.dropped.length, 0);
  assert.equal(queue.count(), 0);
});

test("replay drops conflicts and rejections but retries network failures", async () => {
  const storage = memoryStorage();
  const calls = [];
  const fetchFn = async (url) => {
    calls.push(url);
    if (url === "/api/knowledge/chat") return { ok: false, status: 409 };
    if (url === "/api/drafts") return { ok: false, status: 400 };
    throw new Error("network down");
  };
  const queue = createOfflineQueue(storage, fetchFn);
  queue.enqueue({ kind: "knowledge-question", method: "POST", url: "/api/knowledge/chat", body: {} }, NOW);
  queue.enqueue({ kind: "draft-save", method: "POST", url: "/api/drafts", body: {} }, NOW);
  queue.enqueue({ kind: "note-create", method: "POST", url: "/api/notes", body: {} }, NOW);
  const summary = await queue.replay(NOW);
  assert.deepEqual(
    summary.dropped.map((e) => e.dropReason).sort(),
    ["conflict", "rejected"],
  );
  assert.equal(summary.kept.length, 1);
  assert.equal(summary.kept[0].attempts, 1);
  assert.equal(queue.count(), 1);
});

test("entries are dropped after max attempts, never replayed forever", async () => {
  const storage = memoryStorage();
  const entry = createEntry({ kind: "note-create", method: "POST", url: "/api/notes", body: {} }, NOW);
  entry.attempts = 3;
  enqueue(storage, entry);
  const summary = await replayQueue(storage, async () => {
    throw new Error("unreachable");
  });
  assert.equal(summary.dropped[0].dropReason, "max-attempts");
  assert.equal(queueCount(storage), 0);
});

test("replay drops entries that expired while offline", async () => {
  const storage = memoryStorage();
  const entry = createEntry(
    { kind: "note-create", method: "POST", url: "/api/notes", body: {} },
    NOW - OFFLINE_QUEUE_MAX_AGE_MS - 1,
  );
  enqueue(storage, entry);
  const summary = await replayQueue(storage, async () => ({ ok: true, status: 200 }), NOW);
  assert.equal(summary.synced.length, 0);
  assert.equal(summary.dropped[0].dropReason, "expired");
  assert.equal(queueCount(storage), 0);
});
