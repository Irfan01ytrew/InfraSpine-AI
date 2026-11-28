// Background Sync helpers (Step U, slice 4).
//
// The `online` event (slice 2) replays the offline queue when the page is
// open and notices the connection return — but a tab can miss that event
// (backgrounded tab, flaky radio, browser restarted while offline). The
// Background Sync API covers the gap: the page asks the browser to fire a
// `sync` event in the service worker when connectivity returns, and the
// worker nudges an open page to replay.
//
// Two hard limits shape this slice, and both are honoured, not worked around:
// - Background Sync is Chromium-only. Where `registration.sync` is missing
//   (Safari, Firefox) every helper below degrades to a silent no-op and the
//   `online`-event path from slice 2 stays the only trigger.
// - The queue lives in localStorage, which a service worker cannot read. So
//   the worker never replays anything itself: it only messages an open page
//   (which replays through the normal, policy-checked path) or, when no page
//   is open, re-registers the sync so the queue is retried at the next
//   connectivity change instead of being silently consumed. The page replays
//   on start when online (slice 2) as the backstop.
//
// Safety: registration is best-effort and never throws; a failed or
// unsupported registration changes nothing about the queue. The sync tag
// and the replay message carry no data at all — no entry contents, no keys,
// no credentials — so there is nothing to leak or replay twice. A replay
// triggered this way is the same replay as any other: same allowlist,
// same 24 h expiry, same Idempotency-Key per entry (server-side dedupe).

export const BACKGROUND_SYNC_TAG = "infraspine-offline-queue";
export const REPLAY_QUEUE_MESSAGE_TYPE = "infraspine:replay-queue";

// True when a service-worker message asks the page to replay the queue.
export function isReplayQueueMessage(data) {
  return (
    data !== null &&
    typeof data === "object" &&
    data.type === REPLAY_QUEUE_MESSAGE_TYPE
  );
}

// Ask the browser to fire a `sync` event for our tag when connectivity
// returns. Returns true when the sync was registered, false when the API
// is unavailable or registration failed. Never throws.
export async function registerBackgroundSync(registration) {
  try {
    if (!registration || !registration.sync) return false;
    if (typeof registration.sync.register !== "function") return false;
    await registration.sync.register(BACKGROUND_SYNC_TAG);
    return true;
  } catch {
    return false;
  }
}
