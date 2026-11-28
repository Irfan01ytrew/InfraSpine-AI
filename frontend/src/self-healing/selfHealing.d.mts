export declare function severityTone(severity: string): "danger" | "warning" | "primary" | "neutral";
export declare function statusTone(status: string): "danger" | "warning" | "success";
export declare function incidentKindLabel(kind: string): string;
export declare function filingOutcomeLabel(outcome: string): string;
export declare function isAccessError(status: number | undefined): boolean;
export declare function sortEscalations<T extends { acknowledged_at: string | null; delivered_at: string }>(items: readonly T[]): T[];
