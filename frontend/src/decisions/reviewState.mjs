// Step M slice 6: pure review-date helpers for the Decisions page.
// No imports; tested by frontend/test/decisionsReview.test.mjs.
// Dates are compared as UTC calendar days, so the bucket a decision
// lands in never depends on the viewer's timezone.

function toDay(value) {
  if (typeof value !== "string" || value.length < 10) return null;
  const parts = value.slice(0, 10).split("-").map(Number);
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return null;
  const [year, month, day] = parts;
  const parsed = new Date(Date.UTC(year, month - 1, day));
  if (Number.isNaN(parsed.getTime())) return null;
  if (parsed.getUTCFullYear() !== year || parsed.getUTCMonth() !== month - 1 || parsed.getUTCDate() !== day) return null;
  return parsed;
}

export function reviewState(reviewAt, asOf, daysAhead = 7) {
  if (!reviewAt) return "none";
  const review = toDay(reviewAt);
  const anchor = toDay(asOf);
  if (review === null || anchor === null) return "none";
  const diffDays = Math.round((review.getTime() - anchor.getTime()) / 86400000);
  if (diffDays < 0) return "overdue";
  if (diffDays <= daysAhead) return "due";
  return "ok";
}

export function reviewStateLabel(state) {
  if (state === "overdue") return "Review overdue";
  if (state === "due") return "Review due soon";
  if (state === "ok") return "Review scheduled";
  return "No review date";
}

export function sortDue(decisions) {
  return [...decisions].sort((a, b) => {
    // Decisions without a review date sort last.
    const reviewA = a.review_at || "9999-12-31";
    const reviewB = b.review_at || "9999-12-31";
    if (reviewA !== reviewB) return reviewA < reviewB ? -1 : 1;
    const decidedA = a.decided_at || "";
    const decidedB = b.decided_at || "";
    if (decidedA !== decidedB) return decidedA > decidedB ? -1 : 1;
    if (a.id === b.id) return 0;
    return a.id < b.id ? -1 : 1;
  });
}
