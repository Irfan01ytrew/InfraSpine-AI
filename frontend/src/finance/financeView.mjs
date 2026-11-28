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

/** Short, human hash for display; the full hash is what the approver presents. */
export function shortHash(hash) {
  return typeof hash === "string" && hash.length >= 12 ? `${hash.slice(0, 6)}…${hash.slice(-6)}` : String(hash ?? "");
}

// Short month names per language (Step Z slice 5). Roman Urdu month names
// are the common Latin spellings; Urdu uses Urdu-script names and digits.
const EN_MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const ROMAN_URDU_MONTHS_SHORT = ["Janvari", "Farvari", "March", "April", "Mai", "June", "Julai", "Agast", "Sitambar", "Aktobar", "Navambar", "Disambar"];
const URDU_MONTHS = ["جنوری", "فروری", "مارچ", "اپریل", "مئی", "جون", "جولائی", "اگست", "ستمبر", "اکتوبر", "نومبر", "دسمبر"];
const URDU_DIGITS = { 0: "۰", 1: "۱", 2: "۲", 3: "۳", 4: "۴", 5: "۵", 6: "۶", 7: "۷", 8: "۸", 9: "۹" };

function toUrduDigits(text) {
  return String(text).replace(/[0-9]/g, (digit) => URDU_DIGITS[digit]);
}

/**
 * "2026-10" -> "Oct 26" (en), "Aktobar 26" (roman_urdu), "اکتوبر ۲۶" (urdu).
 * Malformed input passes through unchanged instead of guessing.
 */
export function spendMonthLabel(month, language) {
  if (typeof month !== "string") return String(month ?? "");
  const match = /^(\d{4})-(\d{2})$/.exec(month);
  if (!match) return month;
  const index = Number(match[2]) - 1;
  if (index < 0 || index > 11) return month;
  const shortYear = match[1].slice(2);
  if (language === "urdu") return `${URDU_MONTHS[index]} ${toUrduDigits(shortYear)}`;
  if (language === "roman_urdu") return `${ROMAN_URDU_MONTHS_SHORT[index]} ${shortYear}`;
  return `${EN_MONTHS_SHORT[index]} ${shortYear}`;
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
