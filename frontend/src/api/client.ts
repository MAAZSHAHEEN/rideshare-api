import type { Booking, BookingStatus, DriverBooking, LoginInput, PassengerBooking, RegisterInput, Ride, RideCreateInput, RideSearch, RideStatus, Token, User } from './types';

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
export function createApiClient(
  getCredentials: () => Credentials,
  onUnauthorized: (credentials: Credentials) => void,
  baseUrl = import.meta.env.PROD ? '' : (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'),
) {
  const base = (import.meta.env.PROD ? '' : baseUrl).replace(/\/+$/, '');
  if (base) {
    const parsed = new URL(base);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error('VITE_API_BASE_URL must be empty (same origin) or a public HTTP(S) API URL.');
    }
  }
  async function request<T>(path: string, options: {
    method?: 'GET' | 'POST' | 'PATCH'; body?: unknown; signal?: AbortSignal; public?: boolean; token?: string;
  } = {}): Promise<T> {
    const credentials = getCredentials();
    const token = options.token ?? (options.public ? null : credentials.token);
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (options.body) headers['Content-Type'] = 'application/json';
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
    return body as T;
  }
  return {
    register: (input: RegisterInput, signal?: AbortSignal) => request<User>('/auth/register', { method: 'POST', body: input, public: true, signal }),
    login: (input: LoginInput, signal?: AbortSignal) => request<Token>('/auth/login', { method: 'POST', body: input, public: true, signal }),
    me: (token: string, signal?: AbortSignal) => request<User>('/me', { token, signal }),
    myRides: (offset: number, signal?: AbortSignal, options: { status?: RideStatus; limit?: number } = {}) =>
      request<Ride[]>(`/rides/me?limit=${options.limit ?? 20}&offset=${offset}${options.status ? `&status=${options.status}` : ''}`, { signal }),
    myBookings: (offset: number, signal?: AbortSignal, options: { status?: BookingStatus; limit?: number } = {}) =>
      request<PassengerBooking[]>(`/bookings/me?limit=${options.limit ?? 20}&offset=${offset}${options.status ? `&status=${options.status}` : ''}`, { signal }),
    // Passenger journey (AST-023). Driver endpoints are added beside these later.
    searchRides: (search: RideSearch, signal?: AbortSignal) => request<Ride[]>(`/rides/?${rideSearchQuery(search)}`, { signal }),
    getRide: async (rideId: number, signal?: AbortSignal) => request<Ride>(`/rides/${positiveId(rideId)}`, { signal }),
    bookRide: async (rideId: number, signal?: AbortSignal) => request<Booking>(`/bookings/${positiveId(rideId)}`, { method: 'POST', signal }),
    cancelBooking: async (bookingId: number, signal?: AbortSignal) => request<Booking>(`/bookings/${positiveId(bookingId)}/cancel`, { method: 'PATCH', signal }),
    // Driver journey (M3). The server enforces driver capability and ownership on every call.
    createRide: (input: RideCreateInput, signal?: AbortSignal) => request<Ride>('/rides/', { method: 'POST', body: input, signal }),
    rideBookings: async (rideId: number, signal?: AbortSignal, options: { status?: BookingStatus; limit?: number; offset?: number } = {}) =>
      request<DriverBooking[]>(`/rides/${positiveId(rideId)}/bookings?limit=${options.limit ?? 100}&offset=${options.offset ?? 0}${options.status ? `&status=${options.status}` : ''}`, { signal }),
    respondToBooking: async (bookingId: number, accept: boolean, signal?: AbortSignal) =>
      request<Booking>(`/bookings/${positiveId(bookingId)}/respond?accept=${accept ? 'true' : 'false'}`, { method: 'PATCH', signal }),
    cancelRide: async (rideId: number, signal?: AbortSignal) => request<Ride>(`/rides/${positiveId(rideId)}/cancel`, { method: 'PATCH', signal }),
    completeRide: async (rideId: number, signal?: AbortSignal) => request<Ride>(`/rides/${positiveId(rideId)}/complete`, { method: 'PATCH', signal }),
  };
}
export type ApiClient = ReturnType<typeof createApiClient>;

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
