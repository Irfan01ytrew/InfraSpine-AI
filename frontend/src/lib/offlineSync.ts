// Offline sync wiring (Step U, slice 2).
//
// Slice 1 shipped the queue transport and policy but never wired it into the
// app: nothing replayed the queue when the connection returned, so the banner
// would have had no live status to show. This module is that wiring:
//
// - On start it drops expired queue entries (older than 24 h) and records
//   them as dropped, so the user sees what was lost and why.
// - When the browser comes back online it replays the queue in order
//   (each entry with its Idempotency-Key, via the same-origin session cookie),
//   then records what synced and what was dropped, and tells the banner.
// - Background Sync (slice 4): when entries are still queued it asks the
//   browser for a `sync` event, so the service worker can nudge this page
//   to replay when connectivity returns — covering `online` events the tab
//   missed. Chromium-only; elsewhere this is a silent no-op.
// - While a replay is in flight the pending entries read as "syncing".
//
// The module is idempotent: calling startOfflineSync() twice (React
// StrictMode mounts twice in dev) registers the listeners only once, and a
// replay already in flight is never started twice. It never touches the
// queue policy — allowlist/denylist/expiry still live in offlineQueue.mjs.
import {
  isReplayQueueMessage,
  registerBackgroundSync,
} from "./offlineBackgroundSync.mjs";
import { dropExpired, loadQueue, replayQueue, saveQueue } from "./offlineQueue.mjs";
import type { OfflineQueueEntry } from "./offlineQueue.mjs";
import {
  loadStatusRecords,
  pruneStatusRecords,
  recordDropped,
  recordSynced,
  saveStatusRecords,
} from "./offlineStatus.mjs";
import type { OfflineStatusRecord } from "./offlineStatus.mjs";

export const OFFLINE_QUEUE_CHANGED_EVENT = "infraspine:offline-queue-changed";

export interface OfflineSnapshot {
  online: boolean;
  queue: OfflineQueueEntry[];
  syncingIds: ReadonlySet<string>;
  records: OfflineStatusRecord[];
}

const syncingIds = new Set<string>();
let started = false;
let replaying = false;

export function notifyQueueChanged(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(OFFLINE_QUEUE_CHANGED_EVENT));
  }
}

function readStorage(): Storage | null {
  try {
    return typeof localStorage !== "undefined" ? localStorage : null;
  } catch {
    return null;
  }
}

// Current queue + outcome records, safe to call when storage is unavailable.
export function readOfflineState(): OfflineSnapshot {
  const storage = readStorage();
  if (!storage) {
    return { online: true, queue: [], syncingIds: new Set(), records: [] };
  }
  try {
    return {
      online: typeof navigator === "undefined" ? true : navigator.onLine,
      queue: loadQueue(storage),
      syncingIds: new Set(syncingIds),
      records: pruneStatusRecords(loadStatusRecords(storage)),
    };
  } catch {
    return { online: true, queue: [], syncingIds: new Set(), records: [] };
  }
}

export function dismissOfflineItem(id: string): void {
  const storage = readStorage();
  if (!storage) return;
  try {
    saveStatusRecords(
      storage,
      loadStatusRecords(storage).filter((record) => record.id !== id),
    );
  } catch {
    // Dismissal is best-effort; the record prunes itself after 7 days.
  }
  notifyQueueChanged();
}

export function dismissAllFinished(): void {
  const storage = readStorage();
  if (!storage) return;
  try {
    saveStatusRecords(storage, []);
  } catch {
    // See dismissOfflineItem.
  }
  notifyQueueChanged();
}

function dropExpiredNow(storage: Storage): void {
  const { kept, dropped } = dropExpired(loadQueue(storage), Date.now());
  if (dropped.length === 0) return;
  saveQueue(storage, kept);
  saveStatusRecords(
    storage,
    pruneStatusRecords(recordDropped(loadStatusRecords(storage), dropped)),
  );
}

// Ask the browser for a Background Sync (slice 4) when entries are still
// queued: the service worker then nudges this page to replay when
// connectivity returns, covering `online` events the tab missed. No-op
// when the queue is empty or the API is unavailable; never throws.
function requestBackgroundSync(storage: Storage): void {
  try {
    if (loadQueue(storage).length === 0) return;
    if (
      typeof navigator === "undefined" ||
      !("serviceWorker" in navigator) ||
      !navigator.serviceWorker.ready
    ) {
      return;
    }
    void navigator.serviceWorker.ready
      .then((registration) => registerBackgroundSync(registration))
      .catch(() => undefined);
  } catch {
    // Best-effort only; the `online`-event path still replays the queue.
  }
}

async function runReplay(storage: Storage): Promise<void> {
  if (replaying) return;
  const queue = loadQueue(storage);
  if (queue.length === 0) return;
  replaying = true;
  for (const entry of queue) syncingIds.add(entry.id);
  notifyQueueChanged();
  try {
    const summary = await replayQueue(storage, fetch, Date.now());
    saveStatusRecords(
      storage,
      pruneStatusRecords(
        recordDropped(recordSynced(loadStatusRecords(storage), summary.synced), summary.dropped),
      ),
    );
  } catch {
    // A replay that throws (storage/network) leaves the queue untouched;
    // the next online event retries. Nothing is dropped on a throw.
  } finally {
    for (const entry of queue) syncingIds.delete(entry.id);
    replaying = false;
    notifyQueueChanged();
    // Entries left over (network still down, attempts left) get a
    // Background Sync so the worker retries them at the next connectivity
    // change even if this tab misses the `online` event.
    requestBackgroundSync(storage);
  }
}

// Retry a replay now (the banner's "Retry now" button). No-op while offline.
export function retryOfflineSync(): void {
  const storage = readStorage();
  if (!storage) return;
  if (typeof navigator !== "undefined" && !navigator.onLine) return;
  void runReplay(storage);
}

export function startOfflineSync(): () => void {
  if (started || typeof window === "undefined") {
    return () => undefined;
  }
  started = true;
  const storage = readStorage();
  if (storage) {
    try {
      dropExpiredNow(storage);
      if (navigator.onLine) void runReplay(storage);
      // Queue already has entries (e.g. added while this tab was closed):
      // make sure a sync is registered for the next connectivity change.
      requestBackgroundSync(storage);
    } catch {
      // Offline support is best-effort; the app works without it.
    }
  }
  const onOnline = () => {
    const current = readStorage();
    if (current) void runReplay(current);
    else notifyQueueChanged();
  };
  const onOffline = () => {
    const current = readStorage();
    if (current) requestBackgroundSync(current);
    notifyQueueChanged();
  };
  // Background Sync (slice 4): the worker messages this page when
  // connectivity returns. The message carries no data — just the nudge.
  const onSWMessage = (event: MessageEvent) => {
    if (isReplayQueueMessage(event.data)) retryOfflineSync();
  };
  window.addEventListener("online", onOnline);
  window.addEventListener("offline", onOffline);
  try {
    if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
      navigator.serviceWorker.addEventListener("message", onSWMessage);
    }
  } catch {
    // Without the listener the `online` event still replays the queue.
  }
  return () => {
    window.removeEventListener("online", onOnline);
    window.removeEventListener("offline", onOffline);
    try {
      if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
        navigator.serviceWorker.removeEventListener("message", onSWMessage);
      }
    } catch {
      // Listener removal is best-effort.
    }
    started = false;
  };
}
