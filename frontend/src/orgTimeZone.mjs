// Pure helpers for the organisation time-zone setting (tested with node --test).

export const DEFAULT_ORG_TIME_ZONE = "Asia/Karachi";
const PINNED = ["Asia/Karachi", "UTC", "Asia/Dubai", "Asia/Riyadh", "Asia/Singapore", "Europe/London", "America/New_York"];

/** Pinned common zones first (only those the server offers), then the rest; the current zone is always present. */
export function zoneOptions(serverZones, current) {
  const offered = new Set(Array.isArray(serverZones) ? serverZones : []);
  const pinned = PINNED.filter((zone) => offered.has(zone) || zone === current);
  const rest = [...offered].filter((zone) => !pinned.includes(zone)).sort();
  const all = [...pinned, ...rest];
  if (current && !all.includes(current)) all.unshift(current);
  return all.length ? all : [DEFAULT_ORG_TIME_ZONE];
}

/** "Thu 26 Nov 2026" for an ISO date (YYYY-MM-DD), independent of the browser's own time zone. */
export function formatOrgDay(isoDate) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate ?? ""));
  if (!match) return "";
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return date.toLocaleDateString("en-GB", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short", year: "numeric" });
}
