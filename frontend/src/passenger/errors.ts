import { ApiError } from '../api/client';

export type PassengerAction = 'search' | 'load-ride' | 'book' | 'cancel' | 'load-bookings';

// Known server details (services/bookings.py, services/rides.py, and AST-001's
// departure guard). Anything else gets generic copy: server text is never shown raw.
const BOOK_CONFLICTS: Record<string, string> = {
  'No seats available': 'This ride is full. There are no seats left to request.',
  'Ride is not active': 'This ride is no longer taking requests. It may have been cancelled or completed.',
  'You already booked this ride': 'You have already requested a seat on this ride. Check My bookings for its status.',
  'Ride has already departed': 'This ride has already departed, so it can’t be booked.',
};
const CANCEL_CONFLICTS: Record<string, string> = {
  'Booking cannot be cancelled': 'This booking can no longer be cancelled. It may already be rejected or cancelled.',
  'Ride is not active': 'The ride is no longer active, so this booking can’t be cancelled.',
};
const FORBIDDEN: Record<string, string> = {
  'You cannot book your own ride': 'You can’t request a seat on a ride you are driving.',
  'This is not your booking': 'You can only cancel your own bookings.',
};

export function retryMessage(seconds?: number): string {
  if (!seconds) return 'Too many requests right now. Please wait a moment and try again.';
  if (seconds < 90) return `Too many requests right now. Please wait ${seconds} second${seconds === 1 ? '' : 's'} and try again.`;
  const minutes = Math.ceil(seconds / 60);
  return `Too many requests right now. Please wait about ${minutes} minutes and try again.`;
}

/** Friendly copy for a failed passenger action. Never echoes unknown server text. */
export function passengerErrorMessage(error: unknown, action: PassengerAction): string {
  if (!(error instanceof ApiError)) return 'Something went wrong. Please try again.';
  const { status, detail } = error;
  if (status === 0) return 'We couldn’t reach RideShare. Check your connection and try again.';
  if (status === 401) return 'Your session has ended. Please sign in again.';
  if (status === 429) return retryMessage(error.retryAfter);
  if (status >= 500) return 'The service is temporarily unavailable. Please try again later.';
  if (status === 403) {
    if (detail && FORBIDDEN[detail]) return FORBIDDEN[detail];
    return action === 'cancel' ? 'You can’t cancel this booking.' : 'Your account can’t do that here.';
  }
  if (status === 404) {
    if (action === 'cancel' || action === 'load-bookings') return 'We couldn’t find that booking. It may have been removed.';
    return 'We couldn’t find this ride. It may have been removed.';
  }
  if (status === 409) {
    if (action === 'cancel') return (detail && CANCEL_CONFLICTS[detail]) || 'This booking can’t be cancelled right now.';
    if (action === 'book') return (detail && BOOK_CONFLICTS[detail]) || 'This ride can’t be booked right now.';
    return 'That couldn’t be completed because something changed. Please refresh and try again.';
  }
  if (status === 422) {
    if (action === 'search') return 'Please check your search filters and try again.';
    return 'Some details weren’t accepted. Please refresh and try again.';
  }
  return 'The request could not be completed. Please try again.';
}
