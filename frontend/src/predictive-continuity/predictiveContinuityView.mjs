/**
 * Pure view helpers for the Step L Predictive Continuity page.
 *
 * Everything here is presentation logic over the shapes the
 * predictive-continuity API returns — no fetching, no DOM, no
 * secrets. The page renders only what these helpers allow:
 * proposals are actionable only in the states the approval flow
 * permits (decide while pending, execute only once approved,
 * cancel while draft or pending), and money the server redacted
 * is never reconstructed — a null money total renders as hidden.
 */

export const HORIZONS = [1, 7, 14, 30];
export const DEFAULT_HORIZON = 7;

/** Staleness trust band of a stored forecast record. */
export function trustBadge(trust) {
  switch (trust) {
    case "trustworthy":
      return { tone: "success", label: "Trustworthy" };
    case "review":
      return { tone: "warning", label: "Review" };
    case "stale":
      return { tone: "danger", label: "Stale" };
    default:
      return { tone: "neutral", label: "Unknown" };
  }
}

/** Badge tone for a finding / alert severity. */
export function severityTone(severity) {
  switch (severity) {
    case "critical":
      return "danger";
    case "warning":
      return "warning";
    default:
      return "neutral";
  }
}

/** Human label for a forecast / reconciliation finding code. */
export function findingLabel(code) {
  switch (code) {
    case "uncovered_process":
      return "Uncovered process";
    case "supplier_spof":
      return "Supplier single point of failure";
    case "backup_overlap":
      return "Backup overlap";
    case "forecasted_not_drilled":
      return "Forecasted, drill missed it";
    case "drilled_not_forecasted":
      return "Drilled, forecast missed it";
    default:
      return String(code ?? "").replaceAll("_", " ") || "Finding";
  }
}

/** Human label for a proposal lifecycle status. */
export function proposalStatusLabel(status) {
  switch (status) {
    case "draft":
      return "Draft";
    case "pending_approval":
      return "Waiting for approval";
    case "approved":
      return "Approved";
    case "rejected":
      return "Rejected";
    case "expired":
      return "Expired";
    case "cancelled":
      return "Cancelled";
    case "dispatched":
      return "Dispatched";
    case "verified":
      return "Verified";
    case "failed":
      return "Failed";
    default:
      return String(status ?? "").replaceAll("_", " ") || "Unknown";
  }
}

/**
 * The action kind a finding may propose, or null when the finding
 * cannot become a proposal. Mirrors the server mapping
 * (proposals.ACTION_KINDS): only these three pairs exist, and a
 * caller-built finding for any other pair is rejected.
 */
export function proposalActionForFinding(source, code) {
  if (source === "forecast" && code === "uncovered_process") return "forecast.request_backup";
  if (source === "forecast" && code === "supplier_spof") return "forecast.request_supplier_review";
  if (source === "calendar" && code === "backup_overlap") return "forecast.request_backup_review";
  return null;
}

/**
 * Which proposal buttons the page may show, by lifecycle status.
 * This is the read-only-until-approved discipline in one place:
 * a proposal is decided only while it waits for approval, executed
 * only once it is approved, and cancelled only while it is a draft
 * or waiting. Every other state is display-only; the server
 * enforces the same rules independently.
 */
export function allowedProposalActions(status) {
  return {
    canDecide: status === "pending_approval",
    canExecute: status === "approved",
    canCancel: status === "draft" || status === "pending_approval",
  };
}

/** Records for one horizon; superseded ones only when asked for. */
export function filterRecordsByHorizon(records, horizon, includeSuperseded = false) {
  return (records ?? []).filter(
    (record) => record.horizon_days === horizon && (includeSuperseded || !record.superseded),
  );
}

/**
 * The standing forecast: the newest current record per unavailable
 * root, in newest-first order. Superseded records never stand.
 */
export function latestPerRoot(records) {
  const seen = new Set();
  const latest = [];
  for (const record of records ?? []) {
    if (record.superseded || seen.has(record.root_id)) continue;
    seen.add(record.root_id);
    latest.push(record);
  }
  return latest;
}

/** Processes due inside the forecast horizon, in forecast order. */
export function dueProcesses(forecast) {
  return (forecast?.processes ?? []).filter((p) => p.due_status === "due_within_horizon");
}

/** Processes with no backup coverage (their `reason` is set). */
export function uncoveredProcesses(forecast) {
  return (forecast?.processes ?? []).filter((p) => p.reason != null);
}

/**
 * One-line summary of a stored record for the list row.
 * Counts only — no names, no amounts.
 */
export function summarizeRecord(record) {
  const counts = record?.forecast?.counts ?? {};
  const processes = counts.processes ?? 0;
  const due = counts.processes_due_within_horizon ?? 0;
  const uncovered = counts.uncovered ?? 0;
  const horizon = record?.horizon_days ?? DEFAULT_HORIZON;
  return `${processes} process${processes === 1 ? "" : "es"} · ${due} due in ${horizon} day${horizon === 1 ? "" : "s"} · ${uncovered} uncovered`;
}

/**
 * Money-at-risk totals as display text.
 *
 * `money` is the server shape `{totals: {currency: amount},
 * batch_count, payment_count}`. Returns null when the server
 * redacted the money (null in) so the page renders "hidden" —
 * the amount is never guessed or reconstructed.
 */
export function formatMoneyTotals(money) {
  if (money == null) return null;
  const totals = money.totals ?? {};
  const entries = Object.entries(totals).filter(([, amount]) => Number(amount) !== 0);
  if (entries.length === 0) return "No money at risk";
  return entries
    .map(([currency, amount]) => {
      try {
        return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(amount);
      } catch {
        return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(amount)} ${currency}`;
      }
    })
    .join(" · ");
}

/** The review queue: unacknowledged reconciliations, newest first. */
export function reviewQueue(reconciliations) {
  return (reconciliations ?? [])
    .filter((item) => !item.acknowledged)
    .slice()
    .sort((a, b) => String(b.created_at ?? "").localeCompare(String(a.created_at ?? "")));
}

/** Short form of an action hash for the approval confirmation. */
export function shortHash(hash) {
  if (!hash) return "";
  return hash.length > 16 ? `${hash.slice(0, 12)}…` : hash;
}
