import type { AuditEntry, UsageReport } from "../types";

export const OFFLINE_READ_CACHE_KEY: string;
export const OFFLINE_READ_CACHE_MAX_ANSWERS: number;
export const OFFLINE_READ_CACHE_MAX_AGE_MS: number;
export const OFFLINE_READ_CACHE_MAX_ANSWER_CHARS: number;

export interface OfflineReadCacheStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface SavedAnswerCitation {
  filename: string;
  page_number: number;
}

export interface SavedKnowledgeAnswer {
  id: string;
  question: string;
  answer: string;
  citations: SavedAnswerCitation[];
  model: string | null;
  provider: string | null;
  insufficientEvidence: boolean;
  savedAt: number;
}

export interface SavedAnswerInput {
  question: string;
  answer: string;
  citations?: Array<{ filename?: unknown; page_number?: unknown }>;
  model?: string | null;
  provider?: string | null;
  insufficientEvidence?: boolean;
}

// Mirrors DashboardData (components/HomeDashboard) plus the setup-only
// `agents` count, so a cached snapshot renders the same page live data
// does. `usage`/`activity` keep their live types: the snapshot is only
// ever shown, never recomputed from.
export interface OfflineDashboardData {
  aiActive: boolean;
  aiLabel: string;
  departments: number;
  people: number;
  pendingInvites: number;
  readyDocs: number;
  agents: number;
  processingDocs: number | null;
  failedDocs: number | null;
  usage: UsageReport | null;
  openRequests: number | null;
  reviewQueue: number | null;
  activity: AuditEntry[] | null;
}

export interface DashboardSnapshot {
  savedAt: number;
  data: OfflineDashboardData;
}

export function staleLabel(savedAt: number, now?: number): string;
export function createSavedAnswer(
  input: SavedAnswerInput,
  now?: number,
): SavedKnowledgeAnswer | null;
export function recordKnowledgeAnswer(
  storage: OfflineReadCacheStorage,
  input: SavedAnswerInput,
  now?: number,
): SavedKnowledgeAnswer[] | null;
export function loadKnowledgeAnswers(
  storage: OfflineReadCacheStorage,
  now?: number,
): SavedKnowledgeAnswer[];
export function isSnapshotWorthKeeping(data: unknown): data is OfflineDashboardData;
export function recordDashboardSnapshot(
  storage: OfflineReadCacheStorage,
  data: OfflineDashboardData,
  now?: number,
): DashboardSnapshot | null;
export function loadDashboardSnapshot(
  storage: OfflineReadCacheStorage,
  now?: number,
): DashboardSnapshot | null;
export function clearReadCache(storage: OfflineReadCacheStorage): void;
