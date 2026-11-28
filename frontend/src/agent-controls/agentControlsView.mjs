/**
 * Step AK (slice 4): pure display helpers for the agent controls admin page.
 *
 * Pure functions only — no DOM, no fetch, no React — so they are unit-tested
 * with plain node. Money is rendered from the API's 4dp decimal strings;
 * this module never parses a float.
 *
 * Spend states come from the backend evaluator (`app/agent_controls/evaluator.py`
 * `spend_status`): "ok" | "near_limit" | "at_limit" | "over_limit". An agent
 * with no limit is always "ok" and has no percent.
 */

export const SPEND_STATE_LABELS = {
  ok: "Within budget",
  near_limit: "Near limit",
  at_limit: "At limit",
  over_limit: "Over limit",
};

export const CONTROL_STATUS_LABELS = {
  active: "Active",
  paused: "Paused",
  disabled: "Disabled",
  killed: "Killed",
};

/** Percent of the spend limit used, 0–100, clamped. null when there is no limit. */
export function spendPercent(used, limit) {
  if (limit === null || limit === undefined) return null;
  const usedNum = Number(used);
  const limitNum = Number(limit);
  if (!Number.isFinite(usedNum) || !Number.isFinite(limitNum) || limitNum <= 0) return null;
  return Math.min(100, Math.max(0, (usedNum / limitNum) * 100));
}

/** Human label for a backend spend state; unknown states fall back to the raw value. */
export function spendStateLabel(state) {
  return SPEND_STATE_LABELS[state] ?? String(state);
}

/** Human label for a control record status. */
export function statusLabel(status) {
  return CONTROL_STATUS_LABELS[status] ?? String(status);
}

/** "USD 12.3456" for a 4dp decimal string, "No limit" for null. */
export function formatUsd(value) {
  if (value === null || value === undefined) return "No limit";
  return `USD ${String(value)}`;
}

/**
 * Whether a control record needs an administrator's attention: killed,
 * disabled, paused, or at/over its spend limit. Near-limit is a warning,
 * not an attention item — it still allows work.
 */
export function needsAttention(control) {
  if (!control) return false;
  if (control.status !== "active") return true;
  return control.spend_state === "at_limit" || control.spend_state === "over_limit";
}

/** Count of records needing attention, for the page header summary. */
export function attentionCount(controls) {
  return (controls ?? []).filter((control) => needsAttention(control)).length;
}

/** Sort key for the controls list: attention first, then killed, then by spend percent desc. */
export function sortControls(controls) {
  return [...(controls ?? [])].sort((a, b) => {
    const attention = Number(needsAttention(b)) - Number(needsAttention(a));
    if (attention !== 0) return attention;
    const percentA = spendPercent(a.spend_used_usd, a.spend_limit_usd) ?? -1;
    const percentB = spendPercent(b.spend_used_usd, b.spend_limit_usd) ?? -1;
    return percentB - percentA;
  });
}
