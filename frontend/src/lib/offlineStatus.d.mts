export const OFFLINE_STATUS_KEY: string;
export const OFFLINE_STATUS_MAX_RECORDS: number;
export const OFFLINE_STATUS_MAX_AGE_MS: number;
export const OFFLINE_STATUSES: readonly string[];
export const DROP_REASONS: readonly string[];

export type OfflineStatus = "queued" | "syncing" | "synced" | "dropped";
export type OfflineDropReason = "expired" | "conflict" | "rejected" | "max-attempts";

export interface OfflineStatusRecord {
  id: string;
  kind: string;
  url: string;
  status: OfflineStatus;
  reason: string | null;
  createdAt: number;
  updatedAt: number;
}

export type OfflineStatusItem = OfflineStatusRecord;

export interface OfflineStatusStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export function kindLabel(kind: string): string;
export function statusLabel(status: string): string;
export function dropReasonLabel(reason: string | null | undefined): string;
export function createStatusRecord(
  entry: { id: string; kind?: string; url?: string; createdAt?: number },
  status: OfflineStatus,
  reason: string | null,
  now?: number,
): OfflineStatusRecord;
export function loadStatusRecords(storage: OfflineStatusStorage): OfflineStatusRecord[];
export function saveStatusRecords(storage: OfflineStatusStorage, records: OfflineStatusRecord[]): void;
export function upsertStatusRecords(
  records: OfflineStatusRecord[],
  updates: OfflineStatusRecord[],
): OfflineStatusRecord[];
export function recordSynced(
  records: OfflineStatusRecord[],
  entries: Array<{ id: string; kind?: string; url?: string; createdAt?: number }>,
  now?: number,
): OfflineStatusRecord[];
export function recordDropped(
  records: OfflineStatusRecord[],
  dropped: Array<{ id: string; kind?: string; url?: string; createdAt?: number; dropReason?: string }>,
  now?: number,
): OfflineStatusRecord[];
export function pruneStatusRecords(records: OfflineStatusRecord[], now?: number): OfflineStatusRecord[];
export function dismissStatusRecord(records: OfflineStatusRecord[], id: string): OfflineStatusRecord[];
export function deriveItems(
  queueEntries: Array<{ id: string; kind: string; url: string; createdAt: number }>,
  statusRecords: OfflineStatusRecord[],
  syncingIds?: ReadonlySet<string>,
): OfflineStatusItem[];
export function countByStatus(items: OfflineStatusItem[]): {
  queued: number;
  syncing: number;
  synced: number;
  dropped: number;
  total: number;
};
