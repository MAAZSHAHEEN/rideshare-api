import { describe, expect, it } from 'vitest';
import { ride } from '../test/passengerHarness';
import { acceptBlockedReason, canCancelRide, canCompleteRide, canReject, rideFilterOf } from './state';

const NOW = Date.parse('2026-11-01T04:00:00Z');
const at = (iso: string, overrides = {}) => ride({ departure_time: iso, ...overrides });

describe('driver ride state', () => {
  it('derives upcoming/departed from departure time with an inclusive boundary', () => {
    expect(rideFilterOf(at('2026-11-01T04:00:01Z'), NOW)).toBe('upcoming');
    expect(rideFilterOf(at('2026-11-01T04:00:00Z'), NOW)).toBe('departed');
    expect(rideFilterOf(at('2026-11-01T04:00:01Z', { status: 'cancelled' }), NOW)).toBe('cancelled');
    expect(rideFilterOf(at('2026-10-01T04:00:00Z', { status: 'completed' }), NOW)).toBe('completed');
  });
  it('blocks accepting after departure, when full, or on an inactive ride', () => {
    expect(acceptBlockedReason(at('2026-11-01T05:00:00Z'), NOW)).toBeNull();
    expect(acceptBlockedReason(at('2026-11-01T04:00:00Z'), NOW)).toMatch(/departed/);
    expect(acceptBlockedReason(at('2026-11-01T05:00:00Z', { available_seats: 0 }), NOW)).toMatch(/No seats left/);
    expect(acceptBlockedReason(at('2026-11-01T05:00:00Z', { status: 'cancelled' }), NOW)).toMatch(/no longer active/);
  });
  it('allows reject and cancel on any active ride, complete only after departure', () => {
    const departed = at('2026-11-01T03:00:00Z');
    expect(canReject(departed)).toBe(true);
    expect(canCancelRide(departed)).toBe(true);
    expect(canCompleteRide(departed, NOW)).toBe(true);
    expect(canCompleteRide(at('2026-11-01T05:00:00Z'), NOW)).toBe(false);
    expect(canReject(at('2026-11-01T05:00:00Z', { status: 'completed' }))).toBe(false);
    expect(canCancelRide(at('2026-11-01T05:00:00Z', { status: 'cancelled' }))).toBe(false);
  });
});
