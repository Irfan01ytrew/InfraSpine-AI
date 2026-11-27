/** 384000 -> "384K", 32768 -> "32K", 1000000 -> "1M". */
export function formatTokens(n: number): string {
  // Provider limits are published in decimal units (384K = 384,000); token budgets that
  // are powers of two (32,768) read naturally in binary units (32K).
  if (n >= 1_000_000 && n % 1_000_000 === 0) return `${n / 1_000_000}M`;
  if (n >= 1000 && n % 1000 === 0) return `${n / 1000}K`;
  if (n >= 1024 && n % 1024 === 0) return `${n / 1024}K`;
  return n.toLocaleString();
}

/** "page 3", or the source location for other formats ("Sheet 'Q1' A5:C40", "Slide 2 – Agenda"). */
export function citationPlace(c: { page_number: number; location?: string | null }): string {
  return c.location ?? `page ${c.page_number}`;
}

/** Only PDF pages can be rendered as images. */
export function isPageCitation(c: { locator?: { kind: string } | null }): boolean {
  return !c.locator || c.locator.kind === "page";
}

/** Link to the ACL-checked original media, seeking to the cited audio/video timestamp when available. */
export function mediaPreviewHref(c: {
  document_id: string;
  filename: string;
  locator?: { [key: string]: unknown } | null;
}): string | null {
  if (!/\.(wav|mp3|m4a|flac|ogg|opus|aac|mp4|mov|mkv|webm|avi|m4v)$/i.test(c.filename)) return null;
  const start = c.locator?.start_seconds;
  const frame = c.locator?.timestamp_seconds;
  const seconds = typeof start === "number" ? start : frame;
  const fragment = typeof seconds === "number" && Number.isFinite(seconds) && seconds >= 0
    ? `#t=${seconds.toFixed(3)}`
    : "";
  return `/api/rag/documents/${encodeURIComponent(c.document_id)}/media${fragment}`;
}

/** Organization-wide formats from Company setup; undefined fields follow the viewer's browser. */
export interface RegionalSettings { locale?: string; timeZone?: string; currency?: string }
let regional: RegionalSettings = {};

export function setRegionalSettings(next: { locale?: string | null; time_zone?: string | null; currency?: string | null }): void {
  regional = {
    locale: next.locale ?? undefined,
    timeZone: next.time_zone ?? undefined,
    currency: next.currency ?? undefined,
  };
}

export function regionalSettings(): RegionalSettings {
  return regional;
}

function toDate(value: string | number | Date): Date {
  return value instanceof Date ? value : new Date(value);
}

/** Date and time, e.g. "Oct 30, 2026, 4:30 PM", in the organization's locale and time zone. */
export function formatDate(value: string | number | Date): string {
  const date = toDate(value);
  if (Number.isNaN(date.valueOf())) return "Date unavailable";
  return date.toLocaleString(regional.locale, { dateStyle: "medium", timeStyle: "short", timeZone: regional.timeZone });
}

/** Calendar date only, e.g. "Oct 30, 2026". */
export function formatDay(value: string | number | Date): string {
  const date = toDate(value);
  if (Number.isNaN(date.valueOf())) return "Date unavailable";
  return date.toLocaleDateString(regional.locale, { dateStyle: "medium", timeZone: regional.timeZone });
}

/** A number in the organization's locale. */
export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(regional.locale, options).format(value);
}

/** Money in the given currency, or the organization's default currency. */
export function formatMoney(amount: number, currency?: string | null): string {
  const code = currency || regional.currency;
  if (!code) return formatNumber(amount, { maximumFractionDigits: 2 });
  try {
    return new Intl.NumberFormat(regional.locale, { style: "currency", currency: code }).format(amount);
  } catch {
    return `${formatNumber(amount, { maximumFractionDigits: 2 })} ${code}`;
  }
}

export function timeAgo(value: string): string {
  const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 45) return "just now";
  const units: [number, string][] = [
    [60, "minute"],
    [3600, "hour"],
    [86400, "day"],
  ];
  for (let i = units.length - 1; i >= 0; i--) {
    const [size, name] = units[i];
    if (seconds >= size) {
      const count = Math.round(seconds / size);
      return `${count} ${name}${count === 1 ? "" : "s"} ago`;
    }
  }
  return "just now";
}

export function initials(email: string): string {
  return email.slice(0, 2).toUpperCase();
}
