// Pure helpers for the Home dashboard charts. No DOM, no network: every
// number shown comes from a live API payload passed in by the caller, so a
// chart with no data renders its designed empty state instead of a guess.

function dayKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function dayLabel(date) {
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function nonNegative(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

/**
 * The last `count` calendar days, oldest first, from Agent Studio analytics
 * daily rows ({ date, completed, failed }). Days with no row count as zero,
 * so the trend never invents activity and never hides a quiet day.
 */
export function fillRunDays(count, rows, today = new Date()) {
  const byDay = new Map((rows ?? []).map((row) => [String(row?.date ?? ""), row]));
  const out = [];
  for (let offset = count - 1; offset >= 0; offset--) {
    const date = new Date(today);
    date.setDate(date.getDate() - offset);
    const key = dayKey(date);
    const row = byDay.get(key);
    out.push({
      day: key,
      label: dayLabel(date),
      completed: nonNegative(row?.completed),
      failed: nonNegative(row?.failed),
    });
  }
  return out;
}

/** Donut geometry for one circle (r = 56, circumference 2*pi*56). */
const DONUT_CIRCUMFERENCE = 2 * Math.PI * 56;

/**
 * Turn labelled counts into donut segments. Zero and negative values are
 * dropped; a zero total yields no segments (the caller shows the empty
 * state instead of a fabricated chart). Fractions always sum to 1.
 */
export function donutSegments(parts) {
  const kept = (parts ?? [])
    .map((part) => ({ key: part.key, label: part.label, value: nonNegative(part.value), color: part.color }))
    .filter((part) => part.value > 0);
  const total = kept.reduce((sum, part) => sum + part.value, 0);
  if (total === 0) return { total: 0, segments: [] };
  let offset = 0;
  const segments = kept.map((part) => {
    const fraction = part.value / total;
    const segment = {
      ...part,
      fraction,
      dash: `${fraction * DONUT_CIRCUMFERENCE} ${DONUT_CIRCUMFERENCE - fraction * DONUT_CIRCUMFERENCE}`,
      // Avoid a -0 offset for the first segment (strict equality and SVG both prefer a plain 0).
      offset: offset === 0 ? 0 : -offset * DONUT_CIRCUMFERENCE,
    };
    offset += fraction;
    return segment;
  });
  return { total, segments };
}

/** "InfraSpine Module 01 — Knowledge & AI Brain" -> "Knowledge & AI Brain". */
export function shortModuleName(name) {
  return String(name ?? "").replace(/^InfraSpine Module \d+ — /, "");
}

/**
 * The busiest module specialists by total runs, most active first. Agents
 * with no runs are left out (there is nothing to chart for them).
 */
export function topModules(agents, count = 5) {
  const ranked = (agents ?? [])
    .map((agent) => ({
      id: agent.id,
      name: shortModuleName(agent.name),
      completed: nonNegative(agent.completed),
      failed: nonNegative(agent.failed),
      total: nonNegative(agent.total_runs),
    }))
    .filter((agent) => agent.total > 0)
    .sort((a, b) => b.total - a.total || a.name.localeCompare(b.name));
  const top = ranked.slice(0, Math.max(0, count));
  const max = top.length ? top[0].total : 0;
  return top.map((agent) => ({ ...agent, share: max ? agent.total / max : 0 }));
}
