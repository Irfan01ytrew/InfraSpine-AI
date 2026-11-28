// Offline read-model cache (Step U, slice 5).
//
// While the device is offline the user can still read what they have
// already seen: their last Knowledge answers and the last dashboard
// snapshot. Both are served from this browser's localStorage, read-only,
// and always labelled with when they were saved — they are stale the
// moment they are stored, and they are never used for decisions: no
// approval, payment, workflow or admin action ever reads this cache,
// and the cached answers carry no "apply" path of any kind.
//
// Rules (mirroring the queue/status modules):
// - At most 20 saved answers, newest first; same question asked again
//   replaces the older copy instead of duplicating it.
// - Anything older than 7 days is pruned on load (same window as the
//   offline status records); the dashboard snapshot is a single latest
//   row and expires the same way.
// - A snapshot is only worth keeping when at least one live source fed
//   it (see isSnapshotWorthKeeping): an all-empty load — every source
//   failed — must never overwrite a good cached snapshot.
// - Answers are truncated at 20,000 characters and citations keep only
//   filename + page number (no snippets): localStorage is small and the
//   user can re-open the document for the full text.
// - The cache holds no credentials and no tokens — only answers and
//   counts the user already saw on screen.
// - Same per-device limitation as the queue (slices 1–4): localStorage
//   is per origin, not per user, so a second person signing in on this
//   same browser could read the saved answers. Nothing here is sent
//   anywhere; clearing the browser data clears the cache.
// - This module is pure: it takes storage in and returns values out. It
//   never touches the network, the queue, or the DOM. The service worker
//   (sw.js) still never caches API responses — this cache lives in the
//   page, not in the worker.
export const OFFLINE_READ_CACHE_KEY = "infraspine.offlineReadCache.v1";
export const OFFLINE_READ_CACHE_MAX_ANSWERS = 20;
export const OFFLINE_READ_CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
export const OFFLINE_READ_CACHE_MAX_ANSWER_CHARS = 20000;

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function newId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `a-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function normalizeQuestion(question) {
  return question.trim().toLowerCase();
}

function sanitizeCitations(citations) {
  if (!Array.isArray(citations)) return [];
  const kept = [];
  for (const citation of citations) {
    if (!isObject(citation)) continue;
    if (!isNonEmptyString(citation.filename)) continue;
    // Only what the reader needs to re-find the source. Snippets and
    // scores are dropped: they bloat storage and add nothing offline.
    kept.push({
      filename: citation.filename,
      page_number: typeof citation.page_number === "number" ? citation.page_number : 0,
    });
    if (kept.length >= 8) break;
  }
  return kept;
}

function isSavedAnswer(value) {
  return (
    isObject(value) &&
    typeof value.id === "string" &&
    isNonEmptyString(value.question) &&
    typeof value.answer === "string" &&
    Array.isArray(value.citations) &&
    typeof value.savedAt === "number"
  );
}

function isDashboardData(value) {
  if (!isObject(value)) return false;
  const numbers = ["departments", "people", "pendingInvites", "readyDocs", "agents"];
  for (const field of numbers) {
    if (typeof value[field] !== "number") return false;
  }
  if (typeof value.aiActive !== "boolean" || typeof value.aiLabel !== "string") return false;
  const nullableNumbers = ["processingDocs", "failedDocs", "openRequests", "reviewQueue"];
  for (const field of nullableNumbers) {
    if (value[field] !== null && typeof value[field] !== "number") return false;
  }
  if (value.usage !== null && !isObject(value.usage)) return false;
  if (value.activity !== null && !Array.isArray(value.activity)) return false;
  return true;
}

function loadCache(storage) {
  try {
    const raw = storage.getItem(OFFLINE_READ_CACHE_KEY);
    if (!raw) return { answers: [], snapshot: null };
    const parsed = JSON.parse(raw);
    if (!isObject(parsed)) return { answers: [], snapshot: null };
    const answers = Array.isArray(parsed.answers) ? parsed.answers.filter(isSavedAnswer) : [];
    const snapshot =
      isObject(parsed.snapshot) &&
      typeof parsed.snapshot.savedAt === "number" &&
      isDashboardData(parsed.snapshot.data)
        ? parsed.snapshot
        : null;
    return { answers, snapshot };
  } catch {
    return { answers: [], snapshot: null };
  }
}

function saveCache(storage, cache) {
  storage.setItem(OFFLINE_READ_CACHE_KEY, JSON.stringify(cache));
}

// "Saved 5 minutes ago" — same thresholds as format.timeAgo, but over
// epoch milliseconds so the pure module needs no Date parsing.
export function staleLabel(savedAt, now = Date.now()) {
  const seconds = Math.max(0, Math.round((now - savedAt) / 1000));
  if (seconds < 45) return "Saved just now";
  const units = [
    [60, "minute"],
    [3600, "hour"],
    [86400, "day"],
  ];
  for (let i = units.length - 1; i >= 0; i--) {
    const [size, name] = units[i];
    if (seconds >= size) {
      const count = Math.round(seconds / size);
      return `Saved ${count} ${name}${count === 1 ? "" : "s"} ago`;
    }
  }
  return "Saved just now";
}

export function createSavedAnswer(input, now = Date.now()) {
  if (!isObject(input)) return null;
  if (!isNonEmptyString(input.question)) return null;
  if (typeof input.answer !== "string" || input.answer.trim().length === 0) return null;
  const text =
    input.answer.length > OFFLINE_READ_CACHE_MAX_ANSWER_CHARS
      ? `${input.answer.slice(0, OFFLINE_READ_CACHE_MAX_ANSWER_CHARS)}… (truncated — ask again online for the full answer)`
      : input.answer;
  return {
    id: newId(),
    question: input.question.trim(),
    answer: text,
    citations: sanitizeCitations(input.citations),
    model: typeof input.model === "string" ? input.model : null,
    provider: typeof input.provider === "string" ? input.provider : null,
    insufficientEvidence: input.insufficientEvidence === true,
    savedAt: now,
  };
}

// Record an answer the user just received. Same question replaces the
// older copy; newest first; capped at 20; entries older than 7 days are
// pruned. Returns the stored list, or null when the input is not an
// answer worth keeping.
export function recordKnowledgeAnswer(storage, input, now = Date.now()) {
  const entry = createSavedAnswer(input, now);
  if (!entry) return null;
  const cache = loadCache(storage);
  const fresh = cache.answers.filter(
    (saved) =>
      now - saved.savedAt <= OFFLINE_READ_CACHE_MAX_AGE_MS &&
      normalizeQuestion(saved.question) !== normalizeQuestion(entry.question),
  );
  fresh.unshift(entry);
  const answers = fresh.slice(0, OFFLINE_READ_CACHE_MAX_ANSWERS);
  saveCache(storage, { ...cache, answers });
  return answers;
}

export function loadKnowledgeAnswers(storage, now = Date.now()) {
  const cache = loadCache(storage);
  const fresh = cache.answers.filter((saved) => now - saved.savedAt <= OFFLINE_READ_CACHE_MAX_AGE_MS);
  fresh.sort((a, b) => b.savedAt - a.savedAt);
  return fresh.slice(0, OFFLINE_READ_CACHE_MAX_ANSWERS);
}

// True when at least one live source fed the snapshot. An all-empty load
// (every source failed — the browser was offline or the server was down)
// is not a snapshot worth keeping and must never overwrite a good one.
export function isSnapshotWorthKeeping(data) {
  if (!isDashboardData(data)) return false;
  return (
    data.usage !== null ||
    data.activity !== null ||
    data.aiActive ||
    data.departments > 0 ||
    data.people > 0 ||
    data.readyDocs > 0
  );
}

// Keep only the latest dashboard snapshot. Returns the stored snapshot,
// or null when the data is not worth keeping (see above).
export function recordDashboardSnapshot(storage, data, now = Date.now()) {
  if (!isSnapshotWorthKeeping(data)) return null;
  const cache = loadCache(storage);
  const snapshot = { savedAt: now, data };
  saveCache(storage, { ...cache, snapshot });
  return snapshot;
}

export function loadDashboardSnapshot(storage, now = Date.now()) {
  const cache = loadCache(storage);
  if (!cache.snapshot) return null;
  if (now - cache.snapshot.savedAt > OFFLINE_READ_CACHE_MAX_AGE_MS) return null;
  return cache.snapshot;
}

export function clearReadCache(storage) {
  try {
    storage.removeItem(OFFLINE_READ_CACHE_KEY);
  } catch {
    // Clearing is best-effort; a stale cache prunes itself after 7 days.
  }
}
