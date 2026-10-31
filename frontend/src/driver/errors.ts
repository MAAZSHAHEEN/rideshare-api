import { ApiError } from '../api/client';
import { retryMessage } from '../passenger/errors';
import type { RideFormErrors } from './rideForm';

export type DriverAction = 'create' | 'load-rides' | 'load-ride' | 'load-requests' | 'accept' | 'reject' | 'cancel-ride' | 'complete-ride';

// Known server details (services/bookings.py, services/rides.py, lifecycle.py and
// PR #2's departure guard). Anything else gets generic copy: server text is never shown raw.
const ACCEPT_CONFLICTS: Record<string, string> = {
  'No seats available': 'No seats are left on this ride, so this request can’t be accepted. Requests and seats have been refreshed.',
  'Ride has already departed': 'This ride has already departed, so requests can no longer be accepted.',
  // The server still sends this for accepted/rejected requests and for requests cancelled with their ride.
  'Booking already responded to': 'This request was already answered. The list has been refreshed with its current status.',
  // D7 (PR #17): a pending request the passenger withdrew while the ride is still active.
  'Booking was cancelled by the passenger': 'The passenger withdrew this request, so it can’t be answered. The list has been refreshed.',
  'Ride is not active': 'This ride is no longer active (it was cancelled or completed), so requests can’t be answered.',
  // PR #20 (pending expiry): the request timed out before it was answered.
  'Booking request has expired': 'This request expired before it was answered, so it can’t be accepted. The list has been refreshed.',
};
const REJECT_CONFLICTS: Record<string, string> = {
  'Booking already responded to': ACCEPT_CONFLICTS['Booking already responded to'],
  'Booking was cancelled by the passenger': ACCEPT_CONFLICTS['Booking was cancelled by the passenger'],
  'Booking request has expired': 'This request has expired. The list has been refreshed with its current status.',
  'Ride is not active': ACCEPT_CONFLICTS['Ride is not active'],
};
const RIDE_CONFLICTS: Record<string, string> = {
  'Ride is not active': 'This ride is already cancelled or completed. Its status has been refreshed.',
  // AST-016 (PR #12): completion is refused before the scheduled departure.
  'Ride cannot be completed before its departure time': 'This ride can be marked completed only after its departure time.',
};
const CONCURRENT = 'Something changed at the same time (another action on this ride). The latest status has been loaded. Check it and try again.';
const FORBIDDEN: Record<string, string> = {
  'Only drivers can create rides': 'Your account can’t publish rides.',
  'Only drivers can list their rides': 'Your account can’t see driver rides.',
  'Only drivers can list ride bookings': 'Your account can’t see ride requests.',
  'Only drivers can respond to bookings': 'Your account can’t answer ride requests.',
  'Only drivers can cancel rides': 'Your account can’t cancel rides.',
  'Only drivers can complete rides': 'Your account can’t complete rides.',
  'This is not your ride': 'This ride belongs to another driver. Only its driver can manage it.',
};

/** Friendly copy for a failed driver action. Never echoes unknown server text. */
export function driverErrorMessage(error: unknown, action: DriverAction): string {
  if (!(error instanceof ApiError)) return 'Something went wrong. Please try again.';
  const { status, detail } = error;
  if (status === 0) return 'We couldn’t reach RideShare. Check your connection and try again.';
  if (status === 401) return 'Your session has ended. Please sign in again.';
  if (status === 429) return retryMessage(error.retryAfter);
  if (status >= 500) return 'The service is temporarily unavailable. Please try again later.';
  if (status === 403) return (detail && FORBIDDEN[detail]) || 'Your account can’t do that here.';
  if (status === 404) {
    if (action === 'accept' || action === 'reject') return 'We couldn’t find that request. It may have been removed. The list has been refreshed.';
    if (action === 'load-rides') return 'We couldn’t find your rides.';
    return 'We couldn’t find this ride. It may have been removed.';
  }
  if (status === 409) {
    if (action === 'accept') return (detail && ACCEPT_CONFLICTS[detail]) || CONCURRENT;
    if (action === 'reject') return (detail && REJECT_CONFLICTS[detail]) || CONCURRENT;
    if (action === 'cancel-ride' || action === 'complete-ride') return (detail && RIDE_CONFLICTS[detail]) || CONCURRENT;
    return CONCURRENT;
  }
  if (status === 422) {
    if (action === 'create') return 'Some ride details weren’t accepted. Check the highlighted fields.';
    return 'Some details weren’t accepted. Please refresh and try again.';
  }
  return 'The request could not be completed. Please try again.';
}

const SERVER_FIELD: Record<string, keyof RideFormErrors> = {
  origin: 'origin', destination: 'destination', departure_time: 'departure', available_seats: 'seats', fare_per_seat: 'fare',
};
const FIELD_COPY: Record<keyof RideFormErrors, string> = {
  origin: 'The starting point wasn’t accepted. Use a place name of up to 100 characters.',
  destination: 'The destination wasn’t accepted. Use a different place name of up to 100 characters.',
  departure: 'The departure time wasn’t accepted. Choose a time in the future (Pakistan time).',
  seats: 'The number of seats wasn’t accepted. Use 1 to 8.',
  fare: 'The fare wasn’t accepted. Use a whole number of rupees up to 100,000.',
};

/** Maps a 422 from POST /rides/ onto form fields with our own copy (never the server's text). */
export function rideFieldErrors(error: unknown): RideFormErrors {
  if (!(error instanceof ApiError) || error.status !== 422) return {};
  const errors: RideFormErrors = {};
  if (error.detail === 'Departure time must be in the future') errors.departure = FIELD_COPY.departure;
  for (const [field, message] of Object.entries(error.fields)) {
    const key = SERVER_FIELD[field];
    if (key) errors[key] = FIELD_COPY[key];
    // AST-007's model validator reports origin == destination at the body level.
    else if (/origin and destination must be different/i.test(message)) errors.destination = 'Choose a destination different from the starting point.';
  }
  return errors;
}
