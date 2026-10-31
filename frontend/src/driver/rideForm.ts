import type { RideCreateInput } from '../api/types';
import { RIDE_UTC_OFFSET } from '../passenger/time';

// Client-side mirror of the server's ride rules (schemas.RideCreate +
// services/rides.create_ride, including AST-007's bounds in PR #7). The server
// stays authoritative: these checks only save a round trip and explain why.
export const PLACE_MAX = 100;
export const SEATS_MIN = 1; // The API accepts 0, but a ride with no seats can never be booked.
export const SEATS_MAX = 8;
export const FARE_MIN = 0;
export const FARE_MAX = 100_000; // PKR, AST-007 FARE_PER_SEAT_MAX.
export const SUGGESTED_CITIES = ['Lahore', 'Islamabad', 'Rawalpindi'];
const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f]/;
const LOCAL_DATETIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/;
const OFFSET_MS = 5 * 60 * 60 * 1000;

export interface RideFormValues { origin: string; destination: string; departure: string; seats: string; fare: string }
export type RideFormErrors = Partial<Record<keyof RideFormValues, string>>;

/** A wall-clock "YYYY-MM-DDTHH:MM" in Pakistan time (from <input type="datetime-local">) → ISO with +05:00, or null. */
export function karachiLocalToIso(value: string): string | null {
  const match = LOCAL_DATETIME.exec(value);
  if (!match) return null;
  const [, y, mo, d, h, mi] = match.map(Number);
  const probe = new Date(Date.UTC(y, mo - 1, d, h, mi));
  if (probe.getUTCFullYear() !== y || probe.getUTCMonth() !== mo - 1 || probe.getUTCDate() !== d || probe.getUTCHours() !== h || probe.getUTCMinutes() !== mi) return null;
  return `${value}:00${RIDE_UTC_OFFSET}`;
}

/** The current Pakistan wall-clock time as "YYYY-MM-DDTHH:MM" (for the input's min). */
export function karachiNowLocal(now = Date.now()): string {
  return new Date(now + OFFSET_MS).toISOString().slice(0, 16);
}

function placeError(value: string, label: string): string | undefined {
  if (!value) return `Enter where the ride ${label === 'origin' ? 'leaves from' : 'goes to'}.`;
  if (value.length > PLACE_MAX) return `Use ${PLACE_MAX} characters or fewer.`;
  if (CONTROL_CHARS.test(value)) return 'Remove line breaks and other control characters.';
  return undefined;
}

function wholeNumber(value: string): number | null {
  return /^\d{1,7}$/.test(value) ? Number(value) : null;
}

/** Validates the form; the departure must be strictly after `now` (server rule: departure_time > now). */
export function validateRide(values: RideFormValues, now = Date.now()): RideFormErrors {
  const errors: RideFormErrors = {};
  const origin = values.origin.trim();
  const destination = values.destination.trim();
  errors.origin = placeError(origin, 'origin');
  errors.destination = placeError(destination, 'destination');
  if (!errors.origin && !errors.destination && origin.toLocaleLowerCase() === destination.toLocaleLowerCase()) {
    errors.destination = 'Choose a destination different from the starting point.';
  }
  if (!values.departure) errors.departure = 'Choose the departure date and time.';
  else {
    const iso = karachiLocalToIso(values.departure);
    if (!iso) errors.departure = 'Enter a valid date and time.';
    else if (Date.parse(iso) <= now) errors.departure = 'Choose a departure time in the future (Pakistan time).';
  }
  const seats = wholeNumber(values.seats.trim());
  if (seats === null || seats < SEATS_MIN || seats > SEATS_MAX) errors.seats = `Enter a whole number of seats from ${SEATS_MIN} to ${SEATS_MAX}.`;
  const fare = wholeNumber(values.fare.trim());
  if (fare === null || fare < FARE_MIN || fare > FARE_MAX) errors.fare = `Enter a whole number of rupees from ${FARE_MIN} to ${FARE_MAX.toLocaleString('en-PK')}.`;
  for (const key of Object.keys(errors) as (keyof RideFormValues)[]) if (!errors[key]) delete errors[key];
  return errors;
}

/** The POST /rides/ body for valid values (call validateRide first). */
export function toRideInput(values: RideFormValues): RideCreateInput {
  const iso = karachiLocalToIso(values.departure);
  if (!iso) throw new RangeError('Invalid departure');
  return {
    origin: values.origin.trim(), destination: values.destination.trim(), departure_time: iso,
    available_seats: Number(values.seats.trim()), fare_per_seat: Number(values.fare.trim()),
  };
}
