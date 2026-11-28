import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

import {
  BACKGROUND_SYNC_TAG,
  REPLAY_QUEUE_MESSAGE_TYPE,
  isReplayQueueMessage,
  registerBackgroundSync,
} from "../src/lib/offlineBackgroundSync.mjs";

test("replay message is recognised by type only", () => {
  assert.equal(BACKGROUND_SYNC_TAG, "infraspine-offline-queue");
  assert.equal(REPLAY_QUEUE_MESSAGE_TYPE, "infraspine:replay-queue");
  assert.equal(isReplayQueueMessage({ type: "infraspine:replay-queue" }), true);
  assert.equal(
    isReplayQueueMessage({ type: "infraspine:replay-queue", extra: 1 }),
    true,
  );
  assert.equal(isReplayQueueMessage({ type: "SKIP_WAITING" }), false);
  assert.equal(isReplayQueueMessage({}), false);
  assert.equal(isReplayQueueMessage(null), false);
  assert.equal(isReplayQueueMessage("infraspine:replay-queue"), false);
});

test("registerBackgroundSync registers the tag when supported", async () => {
  const registered = [];
  const registration = { sync: { register: async (tag) => registered.push(tag) } };
  assert.equal(await registerBackgroundSync(registration), true);
  assert.deepEqual(registered, [BACKGROUND_SYNC_TAG]);
});

test("registerBackgroundSync degrades to false, never throws", async () => {
  assert.equal(await registerBackgroundSync(null), false);
  assert.equal(await registerBackgroundSync(undefined), false);
  assert.equal(await registerBackgroundSync({}), false);
  assert.equal(await registerBackgroundSync({ sync: {} }), false);
  assert.equal(
    await registerBackgroundSync({
      sync: {
        register: async () => {
          throw new Error("denied");
        },
      },
    }),
    false,
  );
});

// --- Service worker `sync` handler, exercised against the real sw.js ---
//
// sw.js is a classic worker script (no imports), so the test loads its
// source into a vm context with a mocked `self` and invokes the recorded
// `sync` handler directly. Install/activate/fetch handlers are never
// invoked here; only the sync flow is under test.

const SW_SOURCE = readFileSync(new URL("../public/sw.js", import.meta.url), "utf8");

function loadWorker({ clients, matchAllError = null }) {
  const handlers = {};
  const registerCalls = [];
  const sandbox = {
    self: {
      location: { origin: "https://example.test" },
      addEventListener: (type, handler) => {
        handlers[type] = handler;
      },
      skipWaiting: () => {},
      clients: {
        matchAll: async () => {
          if (matchAllError) throw matchAllError;
          return clients;
        },
      },
      registration: {
        sync: {
          register: async (tag) => {
            registerCalls.push(tag);
          },
        },
      },
    },
    caches: {
      open: async () => ({ addAll: async () => {}, put: async () => {} }),
      keys: async () => [],
      match: async () => null,
      delete: async () => true,
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(SW_SOURCE, sandbox);
  return { handlers, registerCalls };
}

function fakeClient({ url, visibilityState = "visible", postMessage = null }) {
  return {
    url,
    visibilityState,
    postMessage:
      postMessage ||
      ((message) => {
        throw new Error("postMessage not stubbed for this client");
      }),
  };
}

async function fireSync(handlers, tag) {
  let waited = null;
  handlers.sync({ tag, waitUntil: (promise) => (waited = promise) });
  assert.notEqual(waited, null, "sync handler must call waitUntil for our tag");
  return waited;
}

test("sw: unknown sync tags are ignored", () => {
  const { handlers } = loadWorker({ clients: [] });
  let waited = "not-called";
  handlers.sync({ tag: "some-other-tag", waitUntil: (p) => (waited = p) });
  assert.equal(waited, "not-called");
});

test("sw: no open page re-registers the sync, messages nothing", async () => {
  const { handlers, registerCalls } = loadWorker({ clients: [] });
  assert.equal(await fireSync(handlers, BACKGROUND_SYNC_TAG), "reregistered");
  assert.deepEqual(registerCalls, [BACKGROUND_SYNC_TAG]);
});

test("sw: an open admin page is nudged to replay, no re-register", async () => {
  const posted = [];
  const { handlers, registerCalls } = loadWorker({
    clients: [
      fakeClient({
        url: "https://example.test/admin/",
        postMessage: (message) => posted.push(message),
      }),
    ],
  });
  assert.equal(await fireSync(handlers, BACKGROUND_SYNC_TAG), "notified");
  // Compare fields, not the object itself: the message is created inside
  // the vm realm, so its prototype differs from this realm's Object.
  assert.equal(posted.length, 1);
  assert.equal(posted[0].type, REPLAY_QUEUE_MESSAGE_TYPE);
  assert.deepEqual(registerCalls, []);
});

test("sw: non-admin clients are ignored, sync is re-registered", async () => {
  const posted = [];
  const { handlers, registerCalls } = loadWorker({
    clients: [
      fakeClient({
        url: "https://other.example/",
        postMessage: (message) => posted.push(message),
      }),
      fakeClient({
        url: "https://example.test/other-app/",
        postMessage: (message) => posted.push(message),
      }),
    ],
  });
  assert.equal(await fireSync(handlers, BACKGROUND_SYNC_TAG), "reregistered");
  assert.deepEqual(posted, []);
  assert.deepEqual(registerCalls, [BACKGROUND_SYNC_TAG]);
});

test("sw: the visible page is preferred over a hidden one", async () => {
  const posted = [];
  const { handlers, registerCalls } = loadWorker({
    clients: [
      fakeClient({
        url: "https://example.test/admin/",
        visibilityState: "hidden",
        postMessage: () => posted.push("hidden"),
      }),
      fakeClient({
        url: "https://example.test/admin/knowledge",
        visibilityState: "visible",
        postMessage: () => posted.push("visible"),
      }),
    ],
  });
  assert.equal(await fireSync(handlers, BACKGROUND_SYNC_TAG), "notified");
  assert.deepEqual(posted, ["visible"]);
  assert.deepEqual(registerCalls, []);
});

test("sw: a hidden page is used when no page is visible", async () => {
  const posted = [];
  const { handlers, registerCalls } = loadWorker({
    clients: [
      fakeClient({
        url: "https://example.test/admin/",
        visibilityState: "hidden",
        postMessage: () => posted.push("hidden"),
      }),
    ],
  });
  assert.equal(await fireSync(handlers, BACKGROUND_SYNC_TAG), "notified");
  assert.deepEqual(posted, ["hidden"]);
  assert.deepEqual(registerCalls, []);
});

test("sw: a failed nudge re-registers the sync instead of losing it", async () => {
  const { handlers, registerCalls } = loadWorker({
    clients: [
      fakeClient({
        url: "https://example.test/admin/",
        postMessage: () => {
          throw new Error("client gone");
        },
      }),
    ],
  });
  assert.equal(await fireSync(handlers, BACKGROUND_SYNC_TAG), "reregistered");
  assert.deepEqual(registerCalls, [BACKGROUND_SYNC_TAG]);
});

test("sw: a failed client lookup re-registers the sync", async () => {
  const { handlers, registerCalls } = loadWorker({
    clients: [],
    matchAllError: new Error("clients unavailable"),
  });
  assert.equal(await fireSync(handlers, BACKGROUND_SYNC_TAG), "reregistered");
  assert.deepEqual(registerCalls, [BACKGROUND_SYNC_TAG]);
});
