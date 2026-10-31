// RideShare serves Lahore ↔ Islamabad/Rawalpindi. Every time shown to a
// passenger is Pakistan Standard Time, whatever the device's own zone is.
// Pakistan has observed no daylight saving since 2009, so the offset is fixed.
export const RIDE_TIME_ZONE = 'Asia/Karachi';
export const RIDE_UTC_OFFSET = '+05:00';
const OFFSET_MS = 5 * 60 * 60 * 1000;

const departureFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: RIDE_TIME_ZONE, weekday: 'short', day: 'numeric', month: 'short', year: 'numeric',
  hour: 'numeric', minute: '2-digit', hour12: true,
});

/** Parses an API timestamp (ISO-8601). Returns null for anything unusable. */
export function parseInstant(iso: string): Date | null {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "Sat 1 Nov 2026, 9:30 am PKT" — always in Asia/Karachi, never the device zone. */
export function formatDeparture(iso: string): string {
  const date = parseInstant(iso);
  if (!date) return 'Departure time unavailable';
  // Built from parts so the output is identical across ICU versions (no narrow no-break spaces).
  const part = Object.fromEntries(departureFormat.formatToParts(date).map(p => [p.type, p.value]));
  return `${part.weekday} ${part.day} ${part.month} ${part.year}, ${part.hour}:${part.minute} ${String(part.dayPeriod).toLowerCase()} PKT`;
}

/** True when the ride's departure is at or before now; unknown times count as departed (never bookable). */
export function hasDeparted(iso: string, now = Date.now()): boolean {
  const date = parseInstant(iso);
  return !date || date.getTime() <= now;
}

/** Today's calendar date in Pakistan as YYYY-MM-DD. */
export function karachiToday(now = Date.now()): string {
  return new Date(now + OFFSET_MS).toISOString().slice(0, 10);
}

/** True for a real calendar date written as YYYY-MM-DD. */
export function isCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

/** The [start, end) window of one Pakistan calendar day, as offset ISO strings for the API. */
export function karachiDayWindow(day: string): { from: string; before: string } {
  if (!isCalendarDate(day)) throw new RangeError('Invalid date');
  const next = new Date(Date.parse(`${day}T00:00:00Z`) + 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
  return { from: `${day}T00:00:00${RIDE_UTC_OFFSET}`, before: `${next}T00:00:00${RIDE_UTC_OFFSET}` };
}

export function formatFare(pkr: number): string {
  return `PKR ${new Intl.NumberFormat('en-PK', { maximumFractionDigits: 0 }).format(pkr)}`;
}

export function seatsLabel(seats: number): string {
  if (seats <= 0) return 'Full';
  return `${seats} seat${seats === 1 ? '' : 's'} left`;
}
