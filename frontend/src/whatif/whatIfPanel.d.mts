export interface SeedOption {
  id: string;
  name: string;
  kind: string;
}

export interface GraphLike {
  entities?: readonly { id: string; name?: string; kind?: string; record_type?: string }[];
}

export interface SimulationSummary {
  affectedCount: number;
  failedCount: number;
  degradedCount: number;
  waveCount: number;
  stepCount: number;
  blockedCount: number;
  moneyText: string;
}

export interface RehearsalProgress {
  total: number;
  done: number;
  skipped: number;
  pending: number;
  allDone: boolean;
  headline: string;
}

export function seedOptions(graph: GraphLike | null | undefined): SeedOption[];

export function summarizeSimulation(result: {
  affected?: unknown[];
  failed?: unknown[];
  degraded?: unknown[];
  recovery_plan?: { waves?: unknown[][]; blocked_by_dependency_cycle?: unknown[] };
  money_at_risk?: { totals?: Record<string, number>; batch_count?: number };
}): SimulationSummary;

export function rehearsalProgress(rehearsal: {
  steps?: readonly { status?: string }[];
}): RehearsalProgress;

export function nextPendingStep<T extends { status?: string }>(rehearsal: {
  steps?: readonly T[];
}): T | null;

export function comparisonHeadline(comparison: {
  summary?: {
    simulated_count?: number;
    missing_count?: number;
    coverage?: number | null;
    drill_verified_rate?: number | null;
  };
  missing?: unknown[];
}): string;
