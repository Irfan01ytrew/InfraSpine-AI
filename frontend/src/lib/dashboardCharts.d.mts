export function fillRunDays(
  count: number,
  rows: Array<{ date: string; completed: number; failed: number }>,
  today?: Date,
): Array<{ day: string; label: string; completed: number; failed: number }>;
export function donutSegments(
  parts: Array<{ key: string; label: string; value: number; color: string }>,
): {
  total: number;
  segments: Array<{
    key: string;
    label: string;
    value: number;
    color: string;
    fraction: number;
    dash: string;
    offset: number;
  }>;
};
export function shortModuleName(name: string): string;
export function topModules(
  agents: Array<{ id: string; name: string; total_runs: number; completed: number; failed: number }>,
  count?: number,
): Array<{ id: string; name: string; completed: number; failed: number; total: number; share: number }>;
