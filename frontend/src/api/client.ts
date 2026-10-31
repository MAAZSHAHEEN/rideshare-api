import type { Booking, BookingStatus, DriverBooking, LoginInput, Page, PassengerBooking, RegisterInput, Ride, RideBookingCounts, RideCreateInput, RideSearch, RideStatus, RideWhen, SortOrder, Token, User } from './types';
import { MAX_SEATS_PER_BOOKING } from './types';

export class ApiError extends Error {
  /**
   * `detail` keeps the server's string detail for 4xx responses only, so
   * screens can map known conflicts to their own copy. It is never set for
   * 5xx responses and must not be rendered verbatim for unknown values.
   * `retryAfter` is in whole seconds when a 429/503 carried a readable header.
   */
  constructor(public status: number, message: string, public fields: Record<string, string> = {},
    public detail?: string, public retryAfter?: number) {
    super(message);
    this.name = 'ApiError';
  }
}

/** Retry-After as delta-seconds or an HTTP date; anything else is ignored. */
export function parseRetryAfter(value: string | null, now = Date.now()): number | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if (/^\d{1,6}$/.test(trimmed)) return Math.max(1, Number(trimmed));
  const at = Date.parse(trimmed);
  if (Number.isNaN(at)) return undefined;
  const seconds = Math.ceil((at - now) / 1000);
  return seconds > 0 && seconds < 1_000_000 ? seconds : undefined;
}

/**
 * X-Total-Count from a list response (API gap G7). Returns null when the header
 * is missing or not a plain non-negative integer, so the UI falls back to the
 * page-size heuristic against an older backend instead of trusting junk.
 */
export function parseTotalCount(value: string | null): number | null {
  if (value === null) return null;
  const trimmed = value.trim();
  if (!/^\d{1,15}$/.test(trimmed)) return null;
  const total = Number(trimmed);
  return Number.isSafeInteger(total) ? total : null;
}

/** True when another page exists after `offset + count` rows. Uses the server total when known. */
export function hasNextPage(offset: number, count: number, limit: number, total: number | null): boolean {
  if (total !== null) return offset + count < total;
  return count >= limit;
}

export function parseApiError(status: number, body: unknown): ApiError {
  if (status >= 500) return new ApiError(status, 'The service is temporarily unavailable. Please try again later.');
  const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : undefined;
  if (typeof detail === 'string') return new ApiError(status, detail, {}, detail);
  const fields: Record<string, string> = {};
  if (Array.isArray(detail)) for (const item of detail) {
    if (Array.isArray(item?.loc) && typeof item?.msg === 'string') {
      fields[String(item.loc.at(-1))] = item.msg;
    }
  }
  return new ApiError(status, Object.keys(fields).length ? 'Please check the highlighted fields.' : 'The request could not be completed.', fields);
}

export interface Credentials { token: string | null; generation: number }

/** PR #18 accepts 1–64 visible ASCII characters; anything else would be a 422, so it is never sent. */
export const IDEMPOTENCY_KEY = /^[\x21-\x7e]{1,64}$/;

/**
 * Whether to send `Idempotency-Key` (PR #18). On by default: same-origin
 * requests (every production bundle served by FastAPI) involve no CORS, and
 * from feat/frontend-multi-seat on, main.py's CORS allow_headers includes
 * Idempotency-Key, so a split-origin dev build passes the preflight too.
 * VITE_SEND_IDEMPOTENCY_KEY=false opts a split-origin build out, for pointing
 * it at an older backend whose CORS still allows only Authorization and
 * Content-Type (that preflight would fail every write). Same-origin always sends.
 */
export function idempotencyHeaderEnabled(base: string, flag: string | undefined = import.meta.env.VITE_SEND_IDEMPOTENCY_KEY): boolean {
  return base === '' || flag !== 'false';
}

export function createApiClient(
  getCredentials: () => Credentials,
  onUnauthorized: (credentials: Credentials) => void,
  baseUrl = import.meta.env.PROD ? '' : (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'),
  options: { idempotencyKeys?: boolean } = {},
) {
  const base = (import.meta.env.PROD ? '' : baseUrl).replace(/\/+$/, '');
  const sendIdempotencyKeys = options.idempotencyKeys ?? idempotencyHeaderEnabled(base);
  if (base) {
    const parsed = new URL(base);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error('VITE_API_BASE_URL must be empty (same origin) or a public HTTP(S) API URL.');
    }
  }
  type RequestOptions = { method?: 'GET' | 'POST' | 'PATCH'; body?: unknown; signal?: AbortSignal; public?: boolean; token?: string; idempotencyKey?: string };
  async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    return (await send<T>(path, options)).body;
  }
  /** A list endpoint: the JSON array plus the server's X-Total-Count (null when absent or invalid). */
  async function requestPage<T>(path: string, options: RequestOptions = {}): Promise<Page<T>> {
    const { body, response } = await send<T[]>(path, options);
    if (!Array.isArray(body)) throw new ApiError(0, 'The service returned an unexpected response.');
    return { items: body, total: parseTotalCount(response.headers.get('X-Total-Count')) };
  }
  async function send<T>(path: string, options: RequestOptions): Promise<{ body: T; response: Response }> {
    const credentials = getCredentials();
    const token = options.token ?? (options.public ? null : credentials.token);
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (options.body) headers['Content-Type'] = 'application/json';
    if (sendIdempotencyKeys && options.idempotencyKey && IDEMPOTENCY_KEY.test(options.idempotencyKey)) headers['Idempotency-Key'] = options.idempotencyKey;
    let response: Response;
    try {
      response = await fetch(`${base}${path}`, {
        method: options.method ?? 'GET', headers, credentials: 'omit', cache: 'no-store',
        body: options.body ? JSON.stringify(options.body) : undefined,
        signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(20_000)]) : AbortSignal.timeout(20_000),
      });
    } catch (error) {
      if (options.signal?.aborted) throw error;
      throw new ApiError(0, 'We could not reach RideShare. Check your connection and try again.');
    }
    if (response.status === 401 && token && !options.public && !options.token) onUnauthorized(credentials);
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const error = parseApiError(response.status, body);
      if (response.status === 429 || response.status === 503) error.retryAfter = parseRetryAfter(response.headers.get('Retry-After'));
      throw error;
    }
    if (body === null) throw new ApiError(0, 'The service returned an unexpected response.');
    return { body: body as T, response };
  }
  const myRidesPage = async (offset: number, signal?: AbortSignal, options: MyRidesOptions = {}) =>
    requestPage<Ride>(`/rides/me?${myRidesQuery(offset, options)}`, { signal });
  const myBookingsPage = async (offset: number, signal?: AbortSignal, options: MyBookingsOptions = {}) =>
    requestPage<PassengerBooking>(`/bookings/me?${myBookingsQuery(offset, options)}`, { signal });
  const rideBookingsPage = async (rideId: number, signal?: AbortSignal, options: { status?: BookingStatus; limit?: number; offset?: number } = {}) =>
    requestPage<DriverBooking>(`/rides/${positiveId(rideId)}/bookings?limit=${options.limit ?? 100}&offset=${options.offset ?? 0}${options.status ? `&status=${options.status}` : ''}`, { signal });
  const searchRidesPage = async (search: RideSearch, signal?: AbortSignal) => requestPage<Ride>(`/rides/?${rideSearchQuery(search)}`, { signal });
  return {
    register: (input: RegisterInput, signal?: AbortSignal) => request<User>('/auth/register', { method: 'POST', body: input, public: true, signal }),
    login: (input: LoginInput, signal?: AbortSignal) => request<Token>('/auth/login', { method: 'POST', body: input, public: true, signal }),
    me: (token: string, signal?: AbortSignal) => request<User>('/me', { token, signal }),
    // List reads. The *Page variants also return X-Total-Count (G7); the plain ones keep the old array signature.
    myRides: async (offset: number, signal?: AbortSignal, options: MyRidesOptions = {}) => (await myRidesPage(offset, signal, options)).items,
    myRidesPage,
    myBookings: async (offset: number, signal?: AbortSignal, options: MyBookingsOptions = {}) => (await myBookingsPage(offset, signal, options)).items,
    myBookingsPage,
    // Passenger journey (AST-023).
    searchRides: async (search: RideSearch, signal?: AbortSignal) => (await searchRidesPage(search, signal)).items,
    searchRidesPage,
    getRide: async (rideId: number, signal?: AbortSignal) => request<Ride>(`/rides/${positiveId(rideId)}`, { signal }),
    /**
     * `idempotencyKey`: one per user action, reused when that action is retried (PR #18 replays the first result).
     * `seats` (c1a7e0b00003): 1 sends no body, exactly the pre-multi-seat request; 2–8 send `{"seats": n}`.
     */
    bookRide: async (rideId: number, signal?: AbortSignal, idempotencyKey?: string, seats = 1) =>
      request<Booking>(`/bookings/${positiveId(rideId)}`, { method: 'POST', signal, idempotencyKey, body: seats === 1 ? undefined : { seats: seatCount(seats) } }),
    cancelBooking: async (bookingId: number, signal?: AbortSignal) => request<Booking>(`/bookings/${positiveId(bookingId)}/cancel`, { method: 'PATCH', signal }),
    // Driver journey (M3). The server enforces driver capability and ownership on every call.
    createRide: (input: RideCreateInput, signal?: AbortSignal) => request<Ride>('/rides/', { method: 'POST', body: input, signal }),
    rideBookings: async (rideId: number, signal?: AbortSignal, options: { status?: BookingStatus; limit?: number; offset?: number } = {}) =>
      (await rideBookingsPage(rideId, signal, options)).items,
    rideBookingsPage,
    /**
     * Request counts per ride for the signed-in driver (D2): one GET per 100 ids.
     * The server omits rides that aren't the caller's, so a missing id means "no counts", never an error.
     */
    rideBookingCounts: async (rideIds: number[], signal?: AbortSignal) => {
      const ids = [...new Set(rideIds.map(positiveId))];
      const chunks: number[][] = [];
      for (let i = 0; i < ids.length; i += BOOKING_COUNTS_MAX) chunks.push(ids.slice(i, i + BOOKING_COUNTS_MAX));
      const pages = await Promise.all(chunks.map(chunk => request<RideBookingCounts[]>(
        `/rides/me/booking-counts?${chunk.map(id => `ride_id=${id}`).join('&')}`, { signal })));
      return pages.flat();
    },
    respondToBooking: async (bookingId: number, accept: boolean, signal?: AbortSignal, idempotencyKey?: string) =>
      request<Booking>(`/bookings/${positiveId(bookingId)}/respond?accept=${accept ? 'true' : 'false'}`, { method: 'PATCH', signal, idempotencyKey }),
    cancelRide: async (rideId: number, signal?: AbortSignal) => request<Ride>(`/rides/${positiveId(rideId)}/cancel`, { method: 'PATCH', signal }),
    completeRide: async (rideId: number, signal?: AbortSignal) => request<Ride>(`/rides/${positiveId(rideId)}/complete`, { method: 'PATCH', signal }),
  };
}
export type ApiClient = ReturnType<typeof createApiClient>;

/** A seat count the server accepts (1–MAX_SEATS_PER_BOOKING); anything else is a programming error, never sent. */
function seatCount(seats: number): number {
  if (!Number.isSafeInteger(seats) || seats < 1 || seats > MAX_SEATS_PER_BOOKING) throw new Error('Invalid seat count');
  return seats;
}

/** The server accepts 1–100 ride ids per booking-counts request. */
export const BOOKING_COUNTS_MAX = 100;

export interface MyRidesOptions {
  status?: RideStatus; limit?: number;
  /** D1: `upcoming` = departure > now, `departed` = departure <= now (server clock). */
  when?: RideWhen;
  /** D1: `asc` = soonest first; the server default is `desc`. */
  order?: SortOrder;
  /** Aware ISO-8601; inclusive lower bound. */
  departureFrom?: string;
  /** Aware ISO-8601; exclusive upper bound. */
  departureBefore?: string;
}
export interface MyBookingsOptions { status?: BookingStatus; limit?: number; /** G2: only this ride's bookings (always the caller's). */ rideId?: number }

export function myRidesQuery(offset: number, options: MyRidesOptions = {}): string {
  const params = new URLSearchParams({ limit: String(options.limit ?? 20), offset: String(offset) });
  if (options.status) params.set('status', options.status);
  if (options.when) params.set('when', options.when);
  if (options.order) params.set('order', options.order);
  if (options.departureFrom) params.set('departure_from', options.departureFrom);
  if (options.departureBefore) params.set('departure_before', options.departureBefore);
  return params.toString();
}
export function myBookingsQuery(offset: number, options: MyBookingsOptions = {}): string {
  const params = new URLSearchParams({ limit: String(options.limit ?? 20), offset: String(offset) });
  if (options.status) params.set('status', options.status);
  if (options.rideId !== undefined) params.set('ride_id', String(positiveId(options.rideId)));
  return params.toString();
}

function positiveId(id: number): number {
  if (!Number.isSafeInteger(id) || id < 1) throw new ApiError(404, 'Not found.');
  return id;
}

/** Builds the GET /rides/ query with the server's parameter names. Empty text filters are omitted. */
export function rideSearchQuery(search: RideSearch): string {
  const params = new URLSearchParams();
  const origin = search.origin?.trim();
  const destination = search.destination?.trim();
  if (origin) params.set('origin', origin);
  if (destination) params.set('destination', destination);
  if (search.departureFrom) params.set('departure_from', search.departureFrom);
  if (search.departureBefore) params.set('departure_before', search.departureBefore);
  if (search.minSeats && search.minSeats > 1) params.set('min_seats', String(search.minSeats));
  params.set('limit', String(search.limit ?? 20));
  params.set('offset', String(search.offset ?? 0));
  return params.toString();
}
