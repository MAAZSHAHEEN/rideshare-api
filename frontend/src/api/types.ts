// Matches the existing FastAPI response models; timestamps stay ISO strings.
export type Role = 'passenger' | 'driver' | 'admin';
export type AccountMode = 'passenger' | 'driver';
export interface User { id: number; name: string; email: string; role: Role; allowed_modes: AccountMode[] }
export interface Token { access_token: string; token_type: string }
export interface LoginInput { email: string; password: string }
export interface RegisterInput extends LoginInput {
  name: string; cnic: string; phone_number: string; role: 'passenger' | 'driver';
}
export type RideStatus = 'active' | 'completed' | 'cancelled';
/** 'expired' comes from PR #20 (pending expiry); older backends never send it, the UI tolerates both. */
export type BookingStatus = 'pending' | 'accepted' | 'rejected' | 'cancelled' | 'expired';
export interface Ride {
  id: number; driver_id: number; origin: string; destination: string;
  departure_time: string; available_seats: number; fare_per_seat: number; status: RideStatus;
}
export interface PassengerBooking {
  id: number; ride_id: number; passenger_id: number; status: BookingStatus; ride: Ride;
  /** Seats this booking holds once accepted (c1a7e0b00003, 1–8). Absent from pre-#22 servers: read it with seatsOf(), which treats that as 1. */
  seats_requested?: number;
}

/** GET /rides/ query. Place filters are trimmed, case-insensitive whole-value matches on the server (G3). */
export interface RideSearch {
  origin?: string; destination?: string;
  /** ISO-8601 with offset; inclusive lower bound. */
  departureFrom?: string;
  /** ISO-8601 with offset; exclusive upper bound. */
  departureBefore?: string;
  minSeats?: number; limit?: number; offset?: number;
}
export interface Booking {
  id: number; ride_id: number; passenger_id: number; status: BookingStatus;
  /** Seats this booking holds once accepted (c1a7e0b00003, 1–8). Absent from pre-#22 servers: read it with seatsOf(), which treats that as 1. */
  seats_requested?: number;
}

/** Server hard cap on seats per booking (schemas.MAX_SEATS_PER_BOOKING, DB CHECK). BOOKING_MAX_SEATS can only lower it. */
export const MAX_SEATS_PER_BOOKING = 8;

/** Seats a booking holds: `seats_requested`, or 1 for a legacy response without it (or any out-of-range value). */
export function seatsOf(booking: { seats_requested?: number }): number {
  const seats = booking.seats_requested;
  return typeof seats === 'number' && Number.isSafeInteger(seats) && seats >= 1 && seats <= MAX_SEATS_PER_BOOKING ? seats : 1;
}

/** "1 seat" / "N seats". */
export function seatCountLabel(seats: number): string {
  return `${seats} seat${seats === 1 ? '' : 's'}`;
}

/** POST /rides/ body. departure_time is ISO-8601 with an explicit offset (+05:00 from the driver UI). */
export interface RideCreateInput {
  origin: string; destination: string; departure_time: string; available_seats: number; fare_per_seat: number;
}
/** GET /rides/{id}/bookings item: the server projects only the passenger's id and name (no contact data). */
export interface DriverBooking {
  id: number; ride_id: number; passenger_id: number; status: BookingStatus; passenger: { id: number; name: string };
  /** Seats this booking holds once accepted (c1a7e0b00003, 1–8). Absent from pre-#22 servers: read it with seatsOf(), which treats that as 1. */
  seats_requested?: number;
}

/** A list page: the rows plus X-Total-Count (G7), or null against a backend that doesn't send it. */
export interface Page<T> { items: T[]; total: number | null }
/** GET /rides/me `when` (D1). */
export type RideWhen = 'upcoming' | 'departed';
export type SortOrder = 'asc' | 'desc';
/** GET /rides/me/booking-counts item (D2): request counts for one of the driver's own rides. */
export interface RideBookingCounts { ride_id: number; pending: number; accepted: number; rejected: number; cancelled: number; /** Only once PR #20 is merged, if the endpoint adds it. */ expired?: number }
