// Pure helpers for the finance workspace (unit tested with node --test).

/** "32 of 50 verified" plus the remaining ids, from a batch summary. */
export function headline(summary) {
  if (!summary || !summary.total) return "No invoices yet";
  return `${summary.verified} of ${summary.total} verified`;
}

/** Count exception reasons across invoices: [{ code, count }] sorted by count, then code. */
export function reasonCounts(invoices) {
  const counts = new Map();
  for (const invoice of invoices ?? []) {
    for (const reason of invoice.reasons ?? []) counts.set(reason.code, (counts.get(reason.code) ?? 0) + 1);
  }
  return [...counts.entries()].map(([code, count]) => ({ code, count }))
    .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code));
}

/** Spend rows -> stacked series per month for one currency: { months, departments, series:[{month,total,parts}] }. */
export function spendSeries(spend, currency) {
  const months = spend?.months ?? [];
  const rows = (spend?.rows ?? []).filter((row) => !currency || row.currency === currency);
  const departments = [...new Set(rows.map((row) => row.department))].sort();
  const series = months.map((month) => {
    const parts = departments.map((department) => {
      const amount = rows.filter((row) => row.month === month && row.department === department)
        .reduce((sum, row) => sum + Number(row.amount), 0);
      return { department, amount: Math.round(amount * 100) / 100 };
    });
    return { month, total: Math.round(parts.reduce((sum, part) => sum + part.amount, 0) * 100) / 100, parts };
  });
  return { months, departments, series, max: Math.max(0, ...series.map((point) => point.total)) };
}

/** Cash-flow forecast months -> chart points for one currency: { months, series:[{month, expected, closing, confidence}] }.
 *  Amounts stay strings in the API; the chart needs numbers, rounded to 2 decimals.
 *  Every point is a forecast — the caller must label it as such in the UI. */
export function cashFlowSeries(cashFlow) {
  const months = (cashFlow?.months ?? []).map((m) => ({
    month: m.month,
    expected: Math.round(Number(m.expected_outflow) * 100) / 100,
    closing: Math.round(Number(m.closing_balance) * 100) / 100,
    confidence: m.confidence,
  }));
  return { months, max: Math.max(0, ...months.map((m) => m.expected)) };
}

/** Short, human hash for display; the full hash is what the approver presents. */
export function shortHash(hash) {
  return typeof hash === "string" && hash.length >= 12 ? `${hash.slice(0, 6)}…${hash.slice(-6)}` : String(hash ?? "");
}

/** Plain-language summary of documents skipped during Knowledge intake, for the toast body. */
export function skippedSummary(skipped) {
  const rows = skipped ?? [];
  if (!rows.length) return "";
  return rows.map((row) => `${row.filename}: ${row.reason}`).join("; ");
}

/** Which workflow step the current person can take now, given their finance permissions. */
export function actionableStep(run, permissions) {
  if (!run || !Array.isArray(run.steps) || !["running", "queued"].includes(run.status)) return null;
  const needs = { verify: "invoice.verify", pay: "funds.transfer" };
  for (const step of run.steps) {
    const permission = needs[step.node_id];
    if (permission && step.status === "action" && (permissions ?? []).includes(permission)) return { ...step, permission };
  }
  return null;
}
