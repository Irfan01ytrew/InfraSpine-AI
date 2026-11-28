// Offline action queue (Step U, slice 1).
//
// While the device is offline, the user may keep working: safe actions are
// stored in localStorage and replayed in order when the connection returns,
// each with a unique Idempotency-Key so a replay can never execute twice
// server-side (the server already accepts Idempotency-Key, e.g. Agent Studio
// runs in api.ts).
//
// Safety policy — enforced here, in depth (allowlist AND denylist must pass):
// - Only POST actions of an allowlisted kind may be queued. Reads (GET) are
//   never queued; the service worker serves the cached shell instead.
// - NEVER queued: approvals, decisions, payments, transfers, admin changes,
//   auth, workflow runs/cancels, deletes. If it moves money, grants a
//   permission, or changes what an approval covers, it waits for the user
//   to be online. No exception.
// - Server wins: a replay that comes back 409 (state changed on the server)
//   or 4xx is dropped, never retried, and reported to the user.
// - Entries expire after 24 h; stale entries are dropped, never replayed.
// - The queue holds no credentials and no tokens. Replay uses the browser's
//   same-origin session cookie, exactly like a normal request.
export const OFFLINE_QUEUE_KEY = "infraspine.offlineQueue.v1";
export const OFFLINE_QUEUE_MAX_AGE_MS = 24 * 60 * 60 * 1000;
export const OFFLINE_QUEUE_MAX_ATTEMPTS = 3;

// Non-privileged, replay-safe kinds only.
export const QUEUEABLE_KINDS = new Set([
  "knowledge-question",
  "draft-save",
  "note-create",
]);

// URL patterns that must never be queued, whatever the kind says.
export const DENIED_URL_PATTERNS = [
  /\/api\/auth\//,
  /\/api\/approvals?\b/,
  /\/api\/finance\/(pay|payment|transfer)/,
  /\/api\/agent-studio\/runs\/[^/]+\/decision/,
  /\/api\/agent-studio\/runs\/[^/]+\/cancel/,
  /\/api\/workflows?\//,
  /\/api\/admin\//,
  /\/api\/onboarding\//,
];

function newId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `q-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function isQueueable(kind, method, url) {
  if (method !== "POST") return false;
  if (!QUEUEABLE_KINDS.has(kind)) return false;
  if (typeof url !== "string" || !url.startsWith("/api/")) return false;
  return !DENIED_URL_PATTERNS.some((pattern) => pattern.test(url));
}

// Build a queue entry. Returns null when the action must never be queued.
export function createEntry({ kind, method, url, body }, now = Date.now()) {
  if (!isQueueable(kind, method, url)) return null;
  return {
    id: newId(),
    kind,
    method,
    url,
    body: body ?? null,
    idempotencyKey: newId(),
    createdAt: now,
    attempts: 0,
  };
}

export function loadQueue(storage) {
  try {
    const raw = storage.getItem(OFFLINE_QUEUE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (entry) =>
        entry &&
        typeof entry.id === "string" &&
        typeof entry.url === "string" &&
        typeof entry.idempotencyKey === "string",
    );
  } catch {
    return [];
  }
}

export function saveQueue(storage, entries) {
  storage.setItem(OFFLINE_QUEUE_KEY, JSON.stringify(entries));
}

export function enqueue(storage, entry) {
  const entries = loadQueue(storage);
  entries.push(entry);
  saveQueue(storage, entries);
  return entries;
}

export function dropExpired(entries, now = Date.now()) {
  const kept = [];
  const dropped = [];
  for (const entry of entries) {
    if (now - entry.createdAt > OFFLINE_QUEUE_MAX_AGE_MS) {
      dropped.push({ ...entry, dropReason: "expired" });
    } else {
      kept.push(entry);
    }
  }
  return { kept, dropped };
}

function shouldDropOnStatus(status) {
  // 409 = server state changed (server wins); other 4xx = server rejected it.
  return status === 409 || (status >= 400 && status < 500 && status !== 408 && status !== 429);
}

export async function replayQueue(storage, fetchFn, now = Date.now()) {
  const { kept, dropped } = dropExpired(loadQueue(storage), now);
  const summary = { synced: [], dropped, kept: [], attempted: 0 };
  const remaining = [];
  for (const entry of kept) {
    if (entry.attempts >= OFFLINE_QUEUE_MAX_ATTEMPTS) {
      summary.dropped.push({ ...entry, dropReason: "max-attempts" });
      continue;
    }
    summary.attempted += 1;
    let response = null;
    try {
      response = await fetchFn(entry.url, {
        method: entry.method,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": entry.idempotencyKey,
        },
        credentials: "same-origin",
        body: entry.body === null ? undefined : JSON.stringify(entry.body),
      });
    } catch {
      response = null;
    }
    if (response && response.ok) {
      summary.synced.push(entry);
    } else if (response && shouldDropOnStatus(response.status)) {
      summary.dropped.push({
        ...entry,
        dropReason: response.status === 409 ? "conflict" : "rejected",
      });
    } else {
      remaining.push({ ...entry, attempts: entry.attempts + 1 });
    }
  }
  summary.kept = remaining;
  saveQueue(storage, remaining);
  return summary;
}

export function queueCount(storage) {
  return loadQueue(storage).length;
}

export function createOfflineQueue(storage, fetchFn) {
  return {
    enqueue: (input, now) => {
      const entry = createEntry(input, now);
      if (!entry) return null;
      enqueue(storage, entry);
      return entry;
    },
    replay: (now) => replayQueue(storage, fetchFn, now),
    count: () => queueCount(storage),
    entries: () => loadQueue(storage),
  };
}
