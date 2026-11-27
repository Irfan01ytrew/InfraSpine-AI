export interface DatedCalendarItem {
  due_at: string | null;
}

export function localDayKey(date: Date): string;
export function createMonthGrid(anchor: Date): Date[];
export function groupCalendarItems<T extends DatedCalendarItem>(items: readonly T[]): Map<string, T[]>;
