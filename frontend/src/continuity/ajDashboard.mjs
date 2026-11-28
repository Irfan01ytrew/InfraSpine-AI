// Pure display helpers for the Step AJ continuity dashboard page (tested with node --test).
// Standard library only; no I/O, no network. The numbers themselves come from the
// server (GET /api/continuity/scopes/{scope_id}/dashboard); these helpers only
// decide how to show them.

/** Polling interval for the dashboard page (Step AF 30 s default, plan §8). */
export const AJ_REFRESH_MS = 30000;

/** Alert tone for the overall dashboard status. */
export function overallStatusTone(status) {
  if (status === "critical") return "danger";
  if (status === "attention") return "warning";
  return "success";
}

/** Plain-language label for the overall dashboard status. */
export function overallStatusLabel(status) {
  if (status === "critical") return "Needs attention now";
  if (status === "attention") return "Needs attention";
  return "All clear";
}

/** Badge tone for a risk/incident severity. */
export function severityTone(severity) {
  if (severity === "critical") return "danger";
  if (severity === "high") return "warning";
  if (severity === "medium") return "primary";
  return "neutral";
}

/** "62%" for a recovery item, or "No steps" when it has no steps to measure. */
export function progressLabel(progressPercent) {
  if (progressPercent === null || progressPercent === undefined) return "No steps";
  return `${progressPercent}%`;
}

/** Alert tone for the Step G health roll-up status (ok / degraded / down). */
export function healthTone(status) {
  if (status === "down") return "danger";
  if (status === "degraded") return "warning";
  return "success";
}

/** "just now" / "45s ago" / "3m ago" / "2h ago" / "1d ago" for an ISO timestamp. */
export function timeAgo(iso, nowMs = Date.now()) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.floor((nowMs - then) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
