import { describe, expect, it } from 'vitest';
import { ApiError, parseApiError } from '../api/client';
import { driverErrorMessage, rideFieldErrors } from './errors';

const conflict = (detail: string) => new ApiError(409, detail, {}, detail);

describe('driver error copy', () => {
  it('maps each known respond conflict to specific copy', () => {
    expect(driverErrorMessage(conflict('Ride has already departed'), 'accept')).toMatch(/already departed/);
    expect(driverErrorMessage(conflict('No seats available'), 'accept')).toMatch(/No seats are left/);
    expect(driverErrorMessage(conflict('Booking already responded to'), 'accept')).toBe('This request was already answered. The list has been refreshed with its current status.');
    expect(driverErrorMessage(conflict('Booking already responded to'), 'reject')).toMatch(/already answered/);
    expect(driverErrorMessage(conflict('Ride is not active'), 'accept')).toMatch(/no longer active/);
    expect(driverErrorMessage(conflict('Ride is not active'), 'cancel-ride')).toMatch(/already cancelled or completed/);
  });
  it('D7: a request withdrawn by the passenger gets its own copy on accept and reject', () => {
    for (const action of ['accept', 'reject'] as const) {
      const message = driverErrorMessage(conflict('Booking was cancelled by the passenger'), action);
      expect(message).toBe('The passenger withdrew this request, so it can’t be answered. The list has been refreshed.');
      expect(driverErrorMessage(conflict('Booking already responded to'), action)).not.toMatch(/withdrew/);
    }
    // Not a ride-level conflict: cancel/complete keep the generic copy.
    expect(driverErrorMessage(conflict('Booking was cancelled by the passenger'), 'cancel-ride')).toMatch(/changed at the same time/);
  });
  it('AST-016: early completion has specific copy; it is not mapped for other actions', () => {
    expect(driverErrorMessage(conflict('Ride cannot be completed before its departure time'), 'complete-ride')).toBe('This ride can be marked completed only after its departure time.');
    expect(driverErrorMessage(conflict('Ride cannot be completed before its departure time'), 'accept')).toMatch(/changed at the same time/);
  });
  it('near-miss server strings are not matched (exact keys only)', () => {
    for (const detail of ['booking was cancelled by the passenger', 'Booking was cancelled by the passenger.', ' Booking was cancelled by the passenger']) {
      expect(driverErrorMessage(conflict(detail), 'accept')).toMatch(/changed at the same time/);
    }
  });
  it('treats unknown conflicts as a concurrent change and never echoes the server text', () => {
    const message = driverErrorMessage(conflict('could not serialize access due to concurrent update'), 'accept');
    expect(message).toMatch(/changed at the same time/);
    expect(message).not.toMatch(/serialize/);
  });
  it('handles 401, 403, 404, 422, 429, 5xx and network failures safely', () => {
    expect(driverErrorMessage(new ApiError(401, 'x'), 'accept')).toMatch(/session has ended/);
    expect(driverErrorMessage(new ApiError(403, 'This is not your ride', {}, 'This is not your ride'), 'accept')).toMatch(/another driver/);
    expect(driverErrorMessage(new ApiError(403, 'zzz', {}, 'zzz'), 'create')).toBe('Your account can’t do that here.');
    expect(driverErrorMessage(new ApiError(404, 'x'), 'reject')).toMatch(/couldn’t find that request/);
    expect(driverErrorMessage(new ApiError(404, 'x'), 'load-ride')).toMatch(/couldn’t find this ride/);
    expect(driverErrorMessage(new ApiError(422, 'x'), 'create')).toMatch(/highlighted/);
    expect(driverErrorMessage(new ApiError(429, 'x', {}, undefined, 12), 'accept')).toMatch(/wait 12 seconds/);
    expect(driverErrorMessage(parseApiError(500, { detail: 'asyncpg exploded' }), 'accept')).toBe('The service is temporarily unavailable. Please try again later.');
    expect(driverErrorMessage(new ApiError(0, 'x'), 'accept')).toMatch(/connection/);
    expect(driverErrorMessage(new Error('boom'), 'accept')).toBe('Something went wrong. Please try again.');
  });
  it('maps server 422s to form fields with our own copy', () => {
    expect(rideFieldErrors(parseApiError(422, { detail: 'Departure time must be in the future' }))).toEqual({ departure: expect.stringMatching(/future/) });
    const pydantic = parseApiError(422, { detail: [
      { loc: ['body', 'available_seats'], msg: 'Input should be less than or equal to 8' },
      { loc: ['body', 'fare_per_seat'], msg: 'Input should be <script>' },
      { loc: ['body'], msg: 'Value error, Origin and destination must be different' },
    ] });
    const fields = rideFieldErrors(pydantic);
    expect(Object.keys(fields).sort()).toEqual(['destination', 'fare', 'seats']);
    expect(Object.values(fields).join(' ')).not.toMatch(/script|Input should/);
    expect(rideFieldErrors(new ApiError(409, 'x'))).toEqual({});
  });
});
