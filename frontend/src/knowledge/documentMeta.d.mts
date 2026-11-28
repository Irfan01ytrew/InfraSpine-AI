export const EXPIRY_WARN_DAYS: number;
export type ExpiryStatus = "expired" | "due_soon" | "ok";
export function daysUntilExpiry(expiryDate: string | null | undefined, today?: Date | string): number | null;
export function expiryStatus(
  expiryDate: string | null | undefined,
  today?: Date | string,
  warnDays?: number,
): ExpiryStatus | null;
export function expiryBadgeText(expiryDate: string | null | undefined, today?: Date | string): string | null;
export function allTags(documents: readonly { tags?: string[] | null }[] | null | undefined): string[];
export function matchesTag(doc: { tags?: string[] | null }, tag: string): boolean;
export function matchesLibraryQuery(
  doc: { filename: string; tags?: string[] | null; summary?: string | null },
  needle: string,
): boolean;
