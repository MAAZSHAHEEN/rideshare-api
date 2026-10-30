import { describe, expect, it } from 'vitest';
import { formatDeparture, formatFare, hasDeparted, isCalendarDate, karachiDayWindow, karachiToday, seatsLabel } from './time';

describe('Pakistan time rendering', () => {
  it('renders API instants in Asia/Karachi whatever offset the ISO string carries', () => {
    expect(formatDeparture('2026-11-01T04:30:00Z')).toBe('Sun 1 Nov 2026, 9:30 am PKT');
    expect(formatDeparture('2026-11-01T04:30:00+00:00')).toBe('Sun 1 Nov 2026, 9:30 am PKT');
    expect(formatDeparture('2026-11-01T09:30:00+05:00')).toBe('Sun 1 Nov 2026, 9:30 am PKT');
    // 19:30 UTC is already the next day in Pakistan.
    expect(formatDeparture('2026-10-31T19:30:00Z')).toBe('Sun 1 Nov 2026, 12:30 am PKT');
    expect(formatDeparture('2026-11-01T13:05:00Z')).toBe('Sun 1 Nov 2026, 6:05 pm PKT');
  });
  it('never throws on an unusable timestamp', () => {
    expect(formatDeparture('not a date')).toBe('Departure time unavailable');
  });
  it('treats now, the past and unknown times as departed', () => {
    const now = Date.parse('2026-11-01T04:30:00Z');
    expect(hasDeparted('2026-11-01T04:30:00Z', now)).toBe(true);
    expect(hasDeparted('2026-11-01T04:29:59Z', now)).toBe(true);
    expect(hasDeparted('2026-11-01T04:30:01Z', now)).toBe(false);
    expect(hasDeparted('garbage', now)).toBe(true);
  });
});

describe('date filter window', () => {
  it('maps a PKT calendar day to a [start, next start) window with +05:00', () => {
    expect(karachiDayWindow('2026-11-01')).toEqual({ from: '2026-11-01T00:00:00+05:00', before: '2026-11-02T00:00:00+05:00' });
    expect(karachiDayWindow('2026-12-31')).toEqual({ from: '2026-12-31T00:00:00+05:00', before: '2027-01-01T00:00:00+05:00' });
    expect(karachiDayWindow('2028-02-28').before).toBe('2028-02-29T00:00:00+05:00');
  });
  it('rejects impossible dates', () => {
    for (const value of ['2026-02-30', '2026-13-01', '01-11-2026', '2026-1-1', '']) {
      expect(isCalendarDate(value)).toBe(false);
      expect(() => karachiDayWindow(value)).toThrow();
    }
  });
  it('computes "today" in Pakistan, not UTC', () => {
    expect(karachiToday(Date.parse('2026-10-31T18:59:59Z'))).toBe('2026-10-31');
    expect(karachiToday(Date.parse('2026-10-31T19:00:00Z'))).toBe('2026-11-01');
  });
});

describe('fare and seats', () => {
  it('shows whole rupees with grouping', () => {
    expect(formatFare(2500)).toBe('PKR 2,500');
    expect(formatFare(0)).toBe('PKR 0');
  });
  it('labels seats, including a full ride', () => {
    expect(seatsLabel(0)).toBe('Full');
    expect(seatsLabel(1)).toBe('1 seat left');
    expect(seatsLabel(4)).toBe('4 seats left');
  });
});
