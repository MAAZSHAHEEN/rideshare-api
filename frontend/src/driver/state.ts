import type { BookingStatus, DriverBooking, Ride, RideBookingCounts } from '../api/types';
import { seatsOf } from '../api/types';
import { hasDeparted } from '../passenger/time';

// Derived ride states for the driver UI. "Departed" is not stored by the API
// (M4: no worker yet), so it is derived from departure_time <= now.
export type RideFilter = 'upcoming' | 'departed' | 'completed' | 'cancelled';
export const RIDE_FILTERS: RideFilter[] = ['upcoming', 'departed', 'completed', 'cancelled'];
export const FILTER_LABELS: Record<RideFilter, string> = { upcoming: 'Upcoming', departed: 'Departed', completed: 'Completed', cancelled: 'Cancelled' };

export function rideFilterOf(ride: Ride, now = Date.now()): RideFilter {
  if (ride.status === 'cancelled') return 'cancelled';
  if (ride.status === 'completed') return 'completed';
  return hasDeparted(ride.departure_time, now) ? 'departed' : 'upcoming';
}

/** Why the driver can't accept a pending request on this ride right now, or null. Mirrors respond_to_booking + PR #2. */
export function acceptBlockedReason(ride: Ride, now = Date.now()): string | null {
  if (ride.status !== 'active') return 'This ride is no longer active, so requests can’t be accepted.';
  if (hasDeparted(ride.departure_time, now)) return 'This ride has departed, so requests can no longer be accepted.';
  if (ride.available_seats < 1) return 'No seats left. Reject requests or cancel the ride; accepting would overbook it.';
  return null;
}

/**
 * Why this one request can't be accepted although the ride takes requests:
 * it needs more seats than are left (c1a7e0b00003; the server answers 409
 * "Not enough seats available"). Null when it fits, or when the ride-level
 * reason (full, departed, inactive) already applies.
 */
export function acceptSeatShortfall(booking: Pick<DriverBooking, 'seats_requested'>, ride: Ride): string | null {
  const needed = seatsOf(booking);
  const left = ride.available_seats;
  if (left < 1 || needed <= left) return null;
  return `Needs ${needed} seats, but only ${left} ${left === 1 ? 'is' : 'are'} left. Reject it, or wait until seats free up.`;
}

/** Rejecting stays allowed after departure (AST-001); only an inactive ride blocks it. */
export const canReject = (ride: Ride) => ride.status === 'active';
/** services/rides.cancel_ride: any active ride, no departure cut-off. */
export const canCancelRide = (ride: Ride) => ride.status === 'active';
/**
 * The API completes any active ride, but completing before departure would
 * cancel every pending request on a trip that hasn't happened. The UI only
 * offers it once the ride has departed (M4 proposal AST-016).
 */
export const canCompleteRide = (ride: Ride, now = Date.now()) => ride.status === 'active' && hasDeparted(ride.departure_time, now);

export const BOOKING_ORDER: BookingStatus[] = ['pending', 'accepted', 'rejected', 'cancelled', 'expired'];
export const BOOKING_LABELS: Record<BookingStatus, string> = { pending: 'Pending', accepted: 'Accepted', rejected: 'Rejected', cancelled: 'Cancelled', expired: 'Expired' };
export const BOOKING_HELP: Record<BookingStatus, string> = {
  pending: 'Waiting for your answer.',
  accepted: 'Seat confirmed. Each accepted request uses one seat.',
  rejected: 'You declined these requests.',
  cancelled: 'Withdrawn by the passenger, or cancelled with the ride.',
  expired: 'Not answered in time, or still pending at departure. Read-only.',
};

/** "2 pending" for a ride card; null while counts are unknown (never a guess). */
export function pendingLabel(counts: RideBookingCounts | undefined): string | null {
  if (!counts) return null;
  return counts.pending === 0 ? 'No pending' : `${counts.pending} pending`;
}
