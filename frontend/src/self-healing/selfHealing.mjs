// Pure helpers for the self-healing admin page (no DOM, no React).
// Tested with node --test (frontend/test/selfHealing.test.mjs).

/** Badge tone for an incident severity. */
export function severityTone(severity) {
  if (severity === "critical") return "danger";
  if (severity === "high") return "warning";
  if (severity === "medium") return "primary";
  return "neutral";
}

/** Badge tone for an incident status. */
export function statusTone(status) {
  if (status === "open") return "danger";
  if (status === "escalated") return "warning";
  return "success"; // remedied
}

/** Human label for an incident kind. Unknown kinds keep their key. */
export function incidentKindLabel(kind) {
  const labels = {
    api_down: "API down",
    database_unreachable: "Database unreachable",
    worker_stalled: "Worker stalled",
    error_rate_spike: "Error rate spike",
    stale_lock: "Stale lock",
    ingestion_backlog: "Ingestion backlog",
    self_test_failed: "Self-test failed",
    unknown: "Unknown fault",
  };
  return labels[kind] ?? kind;
}

/** Human label for a recovery filing outcome. */
export function filingOutcomeLabel(outcome) {
  const labels = {
    incident: "Recovery incident filed",
    pending_threshold: "Filed, below the recovery threshold",
    duplicate: "Already filed",
    no_target: "No recovery target in that workspace",
  };
  return labels[outcome] ?? outcome;
}

/** True when the API refused the page (missing grant or unknown scope): show a muted note, not an error. */
export function isAccessError(status) {
  return status === 403 || status === 404;
}

/** Unacknowledged escalations first, then newest first. */
export function sortEscalations(items) {
  return [...items].sort((a, b) => {
    const aAck = a.acknowledged_at ? 1 : 0;
    const bAck = b.acknowledged_at ? 1 : 0;
    if (aAck !== bAck) return aAck - bAck;
    return String(b.delivered_at ?? "").localeCompare(String(a.delivered_at ?? ""));
  });
}
