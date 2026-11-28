import type { DrillComparison } from "./types";

export interface DrillComparisonSummary {
  matches: boolean;
  total: number;
  byCode: Record<string, number>;
  headline: string;
  affected: number;
  recovered: number;
  simulated: number;
}

export function summarizeDrillComparison(comparison: DrillComparison | null | undefined): DrillComparisonSummary;
