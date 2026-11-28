// InfraSpine AI Admin service worker (Step U, slice 1).
//
// What it does:
// - Caches the app shell (HTML, JS/CSS, manifest, icons) so the admin UI
//   opens while offline.
// - NEVER caches or serves stale API responses: the worker is registered
//   with scope /admin/, so /api requests never reach it at all — they
//   always go to the network exactly like a normal request.
//
// What it never does: touch auth (the session cookie is HttpOnly and is
// sent by the browser exactly like a normal request), replay queued actions
// itself (that is offlineQueue.mjs, with its own policy), or cache POST/PUT/DELETE.
// Slice 4 adds one narrow exception: on a Background Sync event it *nudges*
// an open page to replay — the page runs the normal replay; the worker
// never sees queue contents (it cannot: the queue lives in localStorage,
// which a worker cannot read).
const CACHE_NAME = "infraspine-admin-v1";
const APP_SHELL = [
  "/admin/",
  "/admin/index.html",
  "/admin/manifest.webmanifest",
  "/admin/icons/icon-192.png",
  "/admin/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

// Background Sync (Step U, slice 4).
//
// The page registers a one-off sync for this tag when entries are queued
// (offlineSync.ts). When connectivity returns the browser fires `sync` here.
// The queue lives in localStorage, which this worker cannot read, so the
// worker never replays anything itself:
// - If an admin page is open, message it to replay. The page runs the
//   normal, policy-checked replay; the message carries no data at all.
// - If no page is open (or the nudge fails), re-register the sync so the
//   browser fires it again at the next connectivity change — never silently
//   consume it. The page also replays on start when online (slice 2), so
//   the queue is not stuck either way.
// The handler never rejects: a failed sync must not spin the browser's
// own retry loop; the explicit re-register above is the retry.
const BACKGROUND_SYNC_TAG = "infraspine-offline-queue";

self.addEventListener("sync", (event) => {
  if (event.tag !== BACKGROUND_SYNC_TAG) return;
  event.waitUntil(handleBackgroundSync());
});

async function handleBackgroundSync() {
  let clients = [];
  try {
    clients = await self.clients.matchAll({
      type: "window",
      includeUncontrolled: true,
    });
  } catch {
    clients = [];
  }
  const adminClients = (clients || []).filter((client) =>
    client.url.startsWith(self.location.origin + "/admin/"),
  );
  const target =
    adminClients.find((client) => client.visibilityState === "visible") ||
    adminClients[0];
  if (target) {
    try {
      target.postMessage({ type: "infraspine:replay-queue" });
      return "notified";
    } catch {
      // The nudge may not have been delivered; fall through to re-register.
    }
  }
  try {
    await self.registration.sync.register(BACKGROUND_SYNC_TAG);
  } catch {
    // Nothing left to do here — the page replays on start when online.
  }
  return "reregistered";
}

function isStaticAsset(url) {
  return /\.(?:js|css|woff2?|ttf|svg|png|webp|ico)$/i.test(url.pathname);
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Only same-origin GET requests under /admin/ are handled at all.
  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }
  if (!url.pathname.startsWith("/admin/")) {
    return;
  }

  // API requests (/api/...) sit outside this worker's /admin/ scope, so
  // they are never intercepted, cached, or served stale. Nothing to do here.
  // Navigations: network first, fall back to the cached shell.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put("/admin/", copy));
          return response;
        })
        .catch(() => caches.match("/admin/")),
    );
    return;
  }

  // Static assets: cache first, populate on miss.
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone();
              caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
            }
            return response;
          }),
      ),
    );
  }
});
