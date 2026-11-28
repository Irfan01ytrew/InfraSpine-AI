export declare function healthBand(score: number | null | undefined): string;
export declare function trustBand(score: number | null | undefined): string;
export declare function gaugeSegments(score: number): { band: string; from: number; to: number; filled: number }[];
export declare function timelineRows(versions: any[]): {
  fingerprint: string; asOf: string; driftScore: number | null; trust: string | null;
  staleSources: string[]; isFirstVersion: boolean; label: string;
}[];
export declare function inboxRows(proposals: any[]): {
  id: string; status: string; actionType: string; approverClass: string; requiredPermission: string;
  code: string; severity: string; detail: string; moneyRedacted: boolean; presentedHash: string;
  createdAt: string; canDecide: boolean; canExecute: boolean;
}[];
export declare function proposalStatusTone(status: string): "success" | "warning" | "neutral";
export declare function findingTone(severity: string): "danger" | "warning" | "neutral";
export declare function queueSummary(counts: Record<string, number> | null | undefined): string;
export declare function maskMoney(value: unknown, isOwner: boolean): string;
export declare function shortFingerprint(fingerprint: string | null | undefined): string;
