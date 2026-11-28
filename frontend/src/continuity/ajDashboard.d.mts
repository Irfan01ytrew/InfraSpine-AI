export declare const AJ_REFRESH_MS: number;
export declare function overallStatusTone(status: string): "success" | "warning" | "danger";
export declare function overallStatusLabel(status: string): string;
export declare function severityTone(severity: string): "success" | "warning" | "danger" | "primary" | "neutral";
export declare function progressLabel(progressPercent: number | null | undefined): string;
export declare function healthTone(status: string): "success" | "warning" | "danger";
export declare function timeAgo(iso: string | undefined | null, nowMs?: number): string;
