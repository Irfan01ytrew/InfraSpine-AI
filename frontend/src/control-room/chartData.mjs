/** Pure helpers for the Control Room dashboard (Step AF slice 3). No I/O. */

/** Invoice counts by status, biggest first, each with its share of the total. */
export function statusBars(byStatus) {
  const entries = Object.entries(byStatus ?? {}).filter(([, count]) => Number(count) > 0);
  const total = entries.reduce((sum, [, count]) => sum + Number(count), 0);
  return entries
    .map(([status, count]) => ({
      status,
      count: Number(count),
      share: total ? Number(count) / total : 0,
    }))
    .sort((a, b) => b.count - a.count || a.status.localeCompare(b.status));
}

/** Height of a chart bar as a 0-100 percentage of the tallest bar. */
export function barPercent(value, max) {
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0 || value <= 0) return 0;
  return Math.min(100, (value / max) * 100);
}

/** Plain-language age for a health-strip timestamp ("5m ago", "2h ago"). */
export function relativeAge(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
