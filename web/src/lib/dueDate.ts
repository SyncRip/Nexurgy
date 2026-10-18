// Task due dates are calendar days. The server returns them as timestamps
// (midnight or noon UTC), and formatting such a timestamp in local time shows
// the previous day west of UTC. Format the calendar day itself instead.

/** The YYYY-MM-DD calendar day of a due date, or null when it has none. */
export function dueDay(due: string | null | undefined): string | null {
  return due?.match(/^(\d{4}-\d{2}-\d{2})/)?.[1] ?? null;
}

/** A due date as a local-noon timestamp of its calendar day, for formatting (no off-by-one west of UTC). */
export function dueDayForDisplay(due: string): string {
  const day = dueDay(due);
  return day ? `${day}T12:00:00` : due;
}

/** Compact calendar day for cards: MM-DD in the current year, else YYYY-MM-DD. */
export function shortDay(due: string, now = new Date()): string {
  const day = dueDay(due);
  if (!day) return due;
  return day.startsWith(`${now.getFullYear()}-`) ? day.slice(5) : day;
}

/** dueDayForDisplay for optional calendar-day fields (due / start dates). */
export function dayForDisplay(value: string | null | undefined): string | null | undefined {
  return value ? dueDayForDisplay(value) : value;
}
