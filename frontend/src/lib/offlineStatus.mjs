// Offline queue status (Step U, slice 2).
//
// The queue itself (offlineQueue.mjs, slice 1) only stores pending entries.
// This module stores what happened to them afterwards, so the UI can show
// per-item status: queued / syncing / synced / dropped + reason.
//
// Rules:
// - Live queue entries are always shown as queued (or syncing while a replay
//   is in flight). A live entry is never shown as synced or dropped.
// - Only terminal outcomes are persisted: synced and dropped records, kept
//   in localStorage under OFFLINE_STATUS_KEY. They are notices, not data:
//   at most 20 records, pruned after 7 days, dismissible by the user.
// - Drop reasons come straight from the queue module: "expired" (older than
//   24 h), "conflict" (409 — server wins), "rejected" (other 4xx), and
//   "max-attempts" (3 failed tries). The reason is always shown with a
//   dropped item, so the user knows what was lost and why.
// - This module is pure: it takes arrays in and returns arrays out. It never
//   touches the network, the queue, or the DOM.
export const OFFLINE_STATUS_KEY = "infraspine.offlineStatus.v1";
export const OFFLINE_STATUS_MAX_RECORDS = 20;
export const OFFLINE_STATUS_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

export const OFFLINE_STATUSES = ["queued", "syncing", "synced", "dropped"];
export const DROP_REASONS = ["expired", "conflict", "rejected", "max-attempts"];

export function kindLabel(kind) {
  switch (kind) {
    case "knowledge-question":
      return "Knowledge question";
    case "draft-save":
      return "Draft saved";
    case "note-create":
      return "Note";
    default:
      return typeof kind === "string" && kind ? kind : "Action";
  }
}

export function statusLabel(status) {
  switch (status) {
    case "queued":
      return "Queued";
    case "syncing":
      return "Syncing…";
    case "synced":
      return "Synced";
    case "dropped":
      return "Dropped";
    default:
      return typeof status === "string" && status ? status : "Unknown";
  }
}

export function dropReasonLabel(reason) {
  switch (reason) {
    case "expired":
      return "Expired — older than 24 hours, never sent";
    case "conflict":
      return "Changed on the server — the server version was kept";
    case "rejected":
      return "Rejected by the server";
    case "max-attempts":
      return "Could not reach the server after 3 tries";
    default:
      return typeof reason === "string" && reason ? reason : "Dropped";
  }
}

export function createStatusRecord(entry, status, reason, now = Date.now()) {
  return {
    id: entry.id,
    kind: typeof entry.kind === "string" ? entry.kind : "unknown",
    url: typeof entry.url === "string" ? entry.url : "",
    status,
    reason: reason ?? null,
    createdAt: typeof entry.createdAt === "number" ? entry.createdAt : now,
    updatedAt: now,
  };
}

function isRecord(value) {
  return (
    value &&
    typeof value.id === "string" &&
    typeof value.kind === "string" &&
    typeof value.status === "string" &&
    OFFLINE_STATUSES.includes(value.status) &&
    typeof value.updatedAt === "number"
  );
}

export function loadStatusRecords(storage) {
  try {
    const raw = storage.getItem(OFFLINE_STATUS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isRecord);
  } catch {
    return [];
  }
}

export function saveStatusRecords(storage, records) {
  storage.setItem(OFFLINE_STATUS_KEY, JSON.stringify(records));
}

export function upsertStatusRecords(records, updates) {
  const byId = new Map(records.map((record) => [record.id, record]));
  for (const update of updates) {
    byId.set(update.id, { ...byId.get(update.id), ...update });
  }
  return [...byId.values()];
}

export function recordSynced(records, entries, now = Date.now()) {
  return upsertStatusRecords(
    records,
    entries.map((entry) => createStatusRecord(entry, "synced", null, now)),
  );
}

export function recordDropped(records, dropped, now = Date.now()) {
  return upsertStatusRecords(
    records,
    dropped.map((entry) =>
      createStatusRecord(entry, "dropped", entry.dropReason ?? "rejected", now),
    ),
  );
}

export function pruneStatusRecords(records, now = Date.now()) {
  const fresh = records.filter((record) => now - record.updatedAt <= OFFLINE_STATUS_MAX_AGE_MS);
  fresh.sort((a, b) => b.updatedAt - a.updatedAt);
  return fresh.slice(0, OFFLINE_STATUS_MAX_RECORDS);
}

export function dismissStatusRecord(records, id) {
  return records.filter((record) => record.id !== id);
}

// Merge the live queue with persisted outcomes into one display list.
// A live entry is always queued (or syncing while a replay is in flight);
// a persisted record only shows when its entry is no longer in the queue.
export function deriveItems(queueEntries, statusRecords, syncingIds = new Set()) {
  const live = new Map();
  for (const entry of queueEntries) {
    live.set(entry.id, {
      id: entry.id,
      kind: entry.kind,
      url: entry.url,
      status: syncingIds.has(entry.id) ? "syncing" : "queued",
      reason: null,
      createdAt: entry.createdAt,
      updatedAt: entry.createdAt,
    });
  }
  const items = [...live.values()];
  for (const record of statusRecords) {
    if (live.has(record.id)) continue;
    items.push({ ...record });
  }
  items.sort((a, b) => b.updatedAt - a.updatedAt || b.createdAt - a.createdAt);
  return items;
}

export function countByStatus(items) {
  const counts = { queued: 0, syncing: 0, synced: 0, dropped: 0, total: items.length };
  for (const item of items) {
    if (Object.prototype.hasOwnProperty.call(counts, item.status)) {
      counts[item.status] += 1;
    }
  }
  return counts;
}
