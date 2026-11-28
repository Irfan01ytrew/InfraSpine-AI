// Step AB slice 5 — pure helpers for the document library's enrichment display.
//
// Tags, summaries and expiry dates are derived metadata *about* a document
// (app/rag/auto_enrich.py); they never change permissions, payees or approvals.

/** Default expiry warning window in days (plan default: 30). */
export const EXPIRY_WARN_DAYS = 30;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function dayNumber(value) {
  // Parse a "YYYY-MM-DD" date as UTC midnight so the day count never shifts
  // with the viewer's timezone. Returns null for anything unparseable.
  if (typeof value !== "string") return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return null;
  const ms = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  if (Number.isNaN(ms)) return null;
  return Math.floor(ms / 86400000);
}

function todayNumber(today) {
  const d = today instanceof Date ? today : new Date(today);
  if (Number.isNaN(d.getTime())) return null;
  return Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) / 86400000);
}

function formatShortDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return value;
  return `${Number(match[3])} ${MONTHS[Number(match[2]) - 1]} ${match[1]}`;
}

/** Whole days from today until the expiry date (negative = already expired), or null. */
export function daysUntilExpiry(expiryDate, today = new Date()) {
  const expiry = dayNumber(expiryDate);
  const now = todayNumber(today);
  if (expiry === null || now === null) return null;
  return expiry - now;
}

/**
 * Expiry state for the library badge: "expired" (date passed), "due_soon"
 * (within EXPIRY_WARN_DAYS), "ok" (later), or null when there is no expiry date.
 */
export function expiryStatus(expiryDate, today = new Date(), warnDays = EXPIRY_WARN_DAYS) {
  const days = daysUntilExpiry(expiryDate, today);
  if (days === null) return null;
  if (days < 0) return "expired";
  if (days <= warnDays) return "due_soon";
  return "ok";
}

/** Badge text for an expiry date, e.g. "Expired 12 Mar 2026" / "Expires in 5 days". */
export function expiryBadgeText(expiryDate, today = new Date()) {
  const days = daysUntilExpiry(expiryDate, today);
  if (days === null) return null;
  const label = formatShortDate(expiryDate);
  if (days < 0) return `Expired ${label}`;
  if (days === 0) return "Expires today";
  if (days === 1) return "Expires tomorrow";
  if (days <= EXPIRY_WARN_DAYS) return `Expires in ${days} days (${label})`;
  return `Expires ${label}`;
}

/** Sorted unique tags across the loaded documents. */
export function allTags(documents) {
  const seen = new Set();
  for (const doc of documents ?? []) {
    for (const tag of doc.tags ?? []) seen.add(tag);
  }
  return [...seen].sort((a, b) => a.localeCompare(b));
}

/** Whether the document passes the selected tag filter ("all" passes everything). */
export function matchesTag(doc, tag) {
  return tag === "all" || (doc.tags ?? []).includes(tag);
}

/** Whether the document matches the library search needle (filename, tags, summary). */
export function matchesLibraryQuery(doc, needle) {
  const q = (needle ?? "").trim().toLowerCase();
  if (!q) return true;
  const haystack = [doc.filename, ...(doc.tags ?? []), doc.summary ?? ""].join(" ").toLowerCase();
  return haystack.includes(q);
}
