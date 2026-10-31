import { describe, expect, it } from 'vitest';
import { karachiLocalToIso, karachiNowLocal, toRideInput, validateRide } from './rideForm';
import type { RideFormValues } from './rideForm';

// 2026-11-01T04:00:00Z = 09:00 PKT on 1 Nov 2026.
const NOW = Date.parse('2026-11-01T04:00:00Z');
const valid: RideFormValues = { origin: 'Lahore', destination: 'Islamabad', departure: '2026-11-02T09:30', seats: '3', fare: '2500' };

describe('ride form: Pakistan time', () => {
  it('sends the wall-clock time with an explicit +05:00 offset', () => {
    expect(karachiLocalToIso('2026-11-02T09:30')).toBe('2026-11-02T09:30:00+05:00');
    expect(Date.parse(karachiLocalToIso('2026-11-02T09:30')!)).toBe(Date.parse('2026-11-02T04:30:00Z'));
  });
  it.each(['', '2026-11-02', '2026-02-30T09:30', '2026-11-02T24:00', '2026-11-02T09:60', '2026-11-02 09:30', '2026-11-02T09:30:00', '2026-11-02T09:30Z'])('rejects %j', value => {
    expect(karachiLocalToIso(value)).toBeNull();
  });
  it('computes the input minimum in PKT, not the device zone', () => {
    expect(karachiNowLocal(NOW)).toBe('2026-11-01T09:00');
    expect(karachiNowLocal(Date.parse('2026-10-31T20:30:00Z'))).toBe('2026-11-01T01:30');
  });
});

describe('ride form: validation mirrors the server', () => {
  it('accepts a valid future ride and builds the exact API body', () => {
    expect(validateRide(valid, NOW)).toEqual({});
    expect(toRideInput({ ...valid, origin: '  Lahore ', fare: ' 2500 ' })).toEqual({
      origin: 'Lahore', destination: 'Islamabad', departure_time: '2026-11-02T09:30:00+05:00', available_seats: 3, fare_per_seat: 2500,
    });
  });
  it('requires a strictly future departure (the exact current minute is rejected)', () => {
    expect(validateRide({ ...valid, departure: '2026-11-01T09:00' }, NOW).departure).toMatch(/future/);
    expect(validateRide({ ...valid, departure: '2026-11-01T08:59' }, NOW).departure).toMatch(/future/);
    expect(validateRide({ ...valid, departure: '2026-11-01T09:01' }, NOW).departure).toBeUndefined();
    expect(validateRide({ ...valid, departure: '' }, NOW).departure).toMatch(/Choose/);
  });
  it('rejects identical places case-insensitively, blanks, long and control-character names', () => {
    expect(validateRide({ ...valid, destination: ' LAHORE ' }, NOW).destination).toMatch(/different/);
    expect(validateRide({ ...valid, origin: '   ' }, NOW).origin).toMatch(/leaves from/);
    expect(validateRide({ ...valid, destination: 'x'.repeat(101) }, NOW).destination).toMatch(/100 characters/);
    expect(validateRide({ ...valid, destination: 'x'.repeat(100) }, NOW).destination).toBeUndefined();
    expect(validateRide({ ...valid, origin: 'Lahore\nCantt' }, NOW).origin).toMatch(/control/);
  });
  it.each([['0', true], ['1', false], ['8', false], ['9', true], ['2.5', true], ['-1', true], ['', true], ['three', true]])('seats %j invalid=%s', (seats, invalid) => {
    expect(Boolean(validateRide({ ...valid, seats }, NOW).seats)).toBe(invalid);
  });
  it.each([['0', false], ['100000', false], ['100001', true], ['-5', true], ['99.5', true], ['', true], ['1e3', true]])('fare %j invalid=%s', (fare, invalid) => {
    expect(Boolean(validateRide({ ...valid, fare }, NOW).fare)).toBe(invalid);
  });
});
