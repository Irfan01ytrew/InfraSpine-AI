export const OFFLINE_QUEUE_KEY: string;
export const OFFLINE_QUEUE_MAX_AGE_MS: number;
export const OFFLINE_QUEUE_MAX_ATTEMPTS: number;
export const QUEUEABLE_KINDS: ReadonlySet<string>;
export const DENIED_URL_PATTERNS: readonly RegExp[];

export interface OfflineQueueEntry {
  id: string;
  kind: string;
  method: string;
  url: string;
  body: unknown;
  idempotencyKey: string;
  createdAt: number;
  attempts: number;
}

export interface OfflineQueueInput {
  kind: string;
  method: string;
  url: string;
  body?: unknown;
}

export interface OfflineQueueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface ReplaySummary {
  synced: OfflineQueueEntry[];
  dropped: Array<OfflineQueueEntry & { dropReason: string }>;
  kept: OfflineQueueEntry[];
  attempted: number;
}

export function isQueueable(kind: string, method: string, url: string): boolean;
export function createEntry(input: OfflineQueueInput, now?: number): OfflineQueueEntry | null;
export function loadQueue(storage: OfflineQueueStorage): OfflineQueueEntry[];
export function saveQueue(storage: OfflineQueueStorage, entries: OfflineQueueEntry[]): void;
export function enqueue(storage: OfflineQueueStorage, entry: OfflineQueueEntry): OfflineQueueEntry[];
export function dropExpired(
  entries: OfflineQueueEntry[],
  now?: number,
): { kept: OfflineQueueEntry[]; dropped: Array<OfflineQueueEntry & { dropReason: string }> };
export function replayQueue(
  storage: OfflineQueueStorage,
  fetchFn: typeof fetch,
  now?: number,
): Promise<ReplaySummary>;
export function queueCount(storage: OfflineQueueStorage): number;
export function createOfflineQueue(
  storage: OfflineQueueStorage,
  fetchFn: typeof fetch,
): {
  enqueue(input: OfflineQueueInput, now?: number): OfflineQueueEntry | null;
  replay(now?: number): Promise<ReplaySummary>;
  count(): number;
  entries(): OfflineQueueEntry[];
};
