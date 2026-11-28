// Twin dashboard view helpers (Step K v2 slice 8). Pure functions
// over the twin API payloads: drift timeline rows, the health gauge,
// the proposal inbox and the review queue. No I/O, no secrets, no
// money amounts — a money value is shown only as "<redacted>" unless
// the caller is the tenant owner (the API already redacts it; the
// helper only formats what it is given).

export function healthBand(score) {
  if (!Number.isFinite(score)) return "unknown";
  if (score >= 75) return "healthy";
  if (score >= 40) return "degraded";
  return "critical";
}

export function trustBand(score) {
  if (score === null || score === undefined || !Number.isFinite(score)) return "unknown";
  if (score >= 75) return "trustworthy";
  if (score >= 40) return "review";
  return "stale";
}

// Gauge segments for a 0-100 health score: the filled share plus the
// three band thresholds (healthy >= 75, degraded >= 40).
export function gaugeSegments(score) {
  const value = Math.max(0, Math.min(100, Number(score) || 0));
  return [
    { band: "critical", from: 0, to: 40, filled: Math.max(0, Math.min(40, value)) },
    { band: "degraded", from: 40, to: 75, filled: Math.max(0, Math.min(35, value - 40)) },
    { band: "healthy", from: 75, to: 100, filled: Math.max(0, value - 75) },
  ];
}

// Newest-first drift timeline: one row per recorded version with
// its drift score (null for the first version — nothing to diff
// against), trust verdict and stale sources.
export function timelineRows(versions) {
  if (!Array.isArray(versions)) return [];
  return [...versions]
    .sort((a, b) => (b.seq ?? 0) - (a.seq ?? 0))
    .map((version) => ({
      fingerprint: version.fingerprint,
      asOf: version.as_of,
      driftScore: version.drift_score ?? null,
      trust: version.trust ?? null,
      staleSources: Array.isArray(version.stale_sources) ? [...version.stale_sources].sort() : [],
      isFirstVersion: Boolean(version.is_first_version),
      label: version.is_first_version
        ? "First version — no drift recorded yet"
        : `Drift ${version.drift_score ?? "—"}`,
    }));
}

// Proposal inbox rows, actionable first (pending approval, then
// approved awaiting execution), then the rest newest first. The
// inbox is read-only until approval: a proposal shows its action
// and approver class, and the Decide / Execute buttons act on the
// exact action hash the approver reviewed.
export function inboxRows(proposals) {
  if (!Array.isArray(proposals)) return [];
  const rank = (status) => (status === "pending_approval" ? 0 : status === "approved" ? 1 : 2);
  return [...proposals]
    .sort((a, b) => rank(a.status) - rank(b.status) || String(b.created_at).localeCompare(String(a.created_at)))
    .map((proposal) => ({
      id: proposal.id,
      status: proposal.status,
      actionType: proposal.action_type,
      approverClass: proposal.approver_class,
      requiredPermission: proposal.required_permission,
      code: proposal.finding?.code ?? "",
      severity: proposal.finding?.severity ?? "",
      detail: proposal.finding?.detail ?? "",
      moneyRedacted: Boolean(proposal.money_redacted),
      presentedHash: proposal.action_hash,
      createdAt: proposal.created_at,
      canDecide: proposal.status === "pending_approval",
      canExecute: proposal.status === "approved",
    }));
}

export function proposalStatusTone(status) {
  if (status === "pending_approval") return "warning";
  if (status === "approved" || status === "executed") return "success";
  if (status === "rejected" || status === "cancelled") return "neutral";
  return "neutral";
}

export function findingTone(severity) {
  if (severity === "critical") return "danger";
  if (severity === "warning") return "warning";
  return "neutral";
}

// One-line queue summary: counts only, no amounts, no names.
export function queueSummary(counts) {
  if (!counts || typeof counts !== "object") return "No review queue loaded";
  const total = counts.total ?? 0;
  const unack = counts.unacknowledged ?? 0;
  const critical = counts.unacknowledged_critical ?? 0;
  if (total === 0) return "Review queue is empty";
  return `${total} finding(s) · ${unack} unacknowledged · ${critical} critical unacknowledged`;
}

export function maskMoney(value, isOwner) {
  if (value === null || value === undefined) return "—";
  if (!isOwner) return "<redacted>";
  if (typeof value === "object") {
    return Object.entries(value).map(([currency, amount]) => `${currency} ${amount}`).join(", ");
  }
  return String(value);
}

export function shortFingerprint(fingerprint) {
  if (typeof fingerprint !== "string" || !fingerprint) return "—";
  return fingerprint.length > 12 ? `${fingerprint.slice(0, 12)}…` : fingerprint;
}
