/** Return a stable local-calendar key for a date, independent of its time of day. */
export function localDayKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** Build a Monday-first, six-week grid so the calendar never shifts page height. */
export function createMonthGrid(anchor) {
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const mondayOffset = (first.getDay() + 6) % 7;
  const start = new Date(first.getFullYear(), first.getMonth(), first.getDate() - mondayOffset);
  return Array.from({ length: 42 }, (_unused, index) =>
    new Date(start.getFullYear(), start.getMonth(), start.getDate() + index),
  );
}

/** Group dated tasks by the browser's local day; invalid and undated items stay out of the calendar. */
export function groupCalendarItems(items) {
  const grouped = new Map();
  for (const item of items) {
    if (!item.due_at) continue;
    const due = new Date(item.due_at);
    if (Number.isNaN(due.valueOf())) continue;
    const key = localDayKey(due);
    const dayItems = grouped.get(key) ?? [];
    dayItems.push(item);
    grouped.set(key, dayItems);
  }
  return grouped;
}
