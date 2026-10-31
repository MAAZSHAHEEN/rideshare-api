import { describe, expect, it } from 'vitest';
import { ApiError, parseApiError } from '../api/client';
import { passengerErrorMessage, retryMessage } from './errors';

const err = (status: number, detail?: string, retryAfter?: number) => {
  const error = parseApiError(status, detail === undefined ? null : { detail });
  error.retryAfter = retryAfter;
  return error;
};

describe('booking error copy', () => {
  it.each([
    ['No seats available', 'This ride is full'],
    ['Ride is not active', 'no longer taking requests'],
    ['You already booked this ride', 'already requested a seat'],
    ['Ride has already departed', 'already departed'],
  ])('maps 409 %s to a specific message', (detail, expected) => {
    expect(passengerErrorMessage(err(409, detail), 'book')).toContain(expected);
  });
  it('maps cancellation conflicts separately from booking conflicts', () => {
    expect(passengerErrorMessage(err(409, 'Booking cannot be cancelled'), 'cancel')).toContain('can no longer be cancelled');
    expect(passengerErrorMessage(err(409, 'Ride is not active'), 'cancel')).toContain('no longer active');
  });
  it('PR #12: cancelling an accepted seat after departure has specific copy (cancel only)', () => {
    expect(passengerErrorMessage(err(409, 'Ride has already departed'), 'cancel')).toBe('This ride has already departed, so an accepted seat can no longer be cancelled.');
    expect(passengerErrorMessage(err(409, 'Ride has already departed'), 'book')).toBe('This ride has already departed, so it can’t be booked.');
    expect(passengerErrorMessage(err(409, 'ride has already departed'), 'cancel')).toBe('This booking can’t be cancelled right now.');
  });
  it('never shows an unknown server detail verbatim', () => {
    const secret = 'duplicate key value violates unique constraint "uq_bookings_active_passenger_ride"';
    for (const status of [400, 403, 404, 409, 422, 418]) {
      for (const action of ['book', 'cancel', 'search', 'load-ride', 'load-bookings'] as const) {
        expect(passengerErrorMessage(err(status, secret), action)).not.toContain('constraint');
      }
    }
    expect(passengerErrorMessage(err(500, secret), 'book')).toBe('The service is temporarily unavailable. Please try again later.');
  });
  it('covers 401, 403, 404, 422, 5xx and network', () => {
    expect(passengerErrorMessage(err(401, 'Invalid token'), 'book')).toBe('Your session has ended. Please sign in again.');
    expect(passengerErrorMessage(err(403, 'You cannot book your own ride'), 'book')).toBe('You can’t request a seat on a ride you are driving.');
    expect(passengerErrorMessage(err(403, 'Only passengers can book rides'), 'book')).toBe('Your account can’t do that here.');
    expect(passengerErrorMessage(err(404, 'Ride not found'), 'book')).toContain('couldn’t find this ride');
    expect(passengerErrorMessage(err(404, 'Booking not found'), 'cancel')).toContain('couldn’t find that booking');
    expect(passengerErrorMessage(err(422), 'search')).toContain('search filters');
    expect(passengerErrorMessage(err(503), 'search')).toContain('temporarily unavailable');
    expect(passengerErrorMessage(new ApiError(0, 'x'), 'book')).toContain('connection');
    expect(passengerErrorMessage(new Error('boom'), 'book')).toBe('Something went wrong. Please try again.');
  });
  it('turns 429 Retry-After into a wait message', () => {
    expect(passengerErrorMessage(err(429, 'Too many authentication attempts', 30), 'book')).toBe('Too many requests right now. Please wait 30 seconds and try again.');
    expect(passengerErrorMessage(err(429), 'book')).toBe('Too many requests right now. Please wait a moment and try again.');
    expect(retryMessage(1)).toContain('1 second and');
    expect(retryMessage(600)).toContain('about 10 minutes');
  });
});
