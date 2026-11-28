import type {
  ForecastDict,
  ForecastProcess,
  ForecastRecordView,
  ForecastReconciliation,
} from "./types";

export declare const HORIZONS: number[];
export declare const DEFAULT_HORIZON: number;
export declare function trustBadge(trust: string | null | undefined): {
  tone: "success" | "warning" | "danger" | "neutral";
  label: string;
};
export declare function severityTone(severity: string | null | undefined): "danger" | "warning" | "neutral";
export declare function findingLabel(code: string | null | undefined): string;
export declare function proposalStatusLabel(status: string | null | undefined): string;
export declare function proposalActionForFinding(source: string, code: string): string | null;
export declare function allowedProposalActions(status: string): {
  canDecide: boolean;
  canExecute: boolean;
  canCancel: boolean;
};
export declare function filterRecordsByHorizon(
  records: ForecastRecordView[] | null | undefined,
  horizon: number,
  includeSuperseded?: boolean,
): ForecastRecordView[];
export declare function latestPerRoot(
  records: ForecastRecordView[] | null | undefined,
): ForecastRecordView[];
export declare function dueProcesses(forecast: ForecastDict | null | undefined): ForecastProcess[];
export declare function uncoveredProcesses(forecast: ForecastDict | null | undefined): ForecastProcess[];
export declare function summarizeRecord(record: ForecastRecordView | null | undefined): string;
export declare function formatMoneyTotals(
  money: { totals?: Record<string, number> } | null | undefined,
): string | null;
export declare function reviewQueue(
  reconciliations: ForecastReconciliation[] | null | undefined,
): ForecastReconciliation[];
export declare function shortHash(hash: string | null | undefined): string;
