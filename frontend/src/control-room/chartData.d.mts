export function statusBars(
  byStatus: Record<string, number>,
): Array<{ status: string; count: number; share: number }>;
export function barPercent(value: number, max: number): number;
export function relativeAge(seconds: number | null | undefined): string;
