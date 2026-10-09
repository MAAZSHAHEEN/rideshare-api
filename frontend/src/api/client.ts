import type { LoginInput, PassengerBooking, RegisterInput, Ride, Token, User } from './types';

export class ApiError extends Error {
  constructor(public status: number, message: string, public fields: Record<string, string> = {}) {
    super(message);
    this.name = 'ApiError';
  }
}

export function parseApiError(status: number, body: unknown): ApiError {
  if (status >= 500) return new ApiError(status, 'The service is temporarily unavailable. Please try again later.');
  const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : undefined;
  if (typeof detail === 'string') return new ApiError(status, detail);
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
  baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
) {
  const base = baseUrl.replace(/\/+$/, '');
  const parsed = new URL(base);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('VITE_API_BASE_URL must be a public HTTP(S) API URL.');
  }
  async function request<T>(path: string, options: {
    method?: 'GET' | 'POST'; body?: unknown; signal?: AbortSignal; public?: boolean; token?: string;
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
    if (!response.ok) throw parseApiError(response.status, body);
    if (body === null) throw new ApiError(0, 'The service returned an unexpected response.');
    return body as T;
  }
  return {
    register: (input: RegisterInput, signal?: AbortSignal) => request<User>('/auth/register', { method: 'POST', body: input, public: true, signal }),
    login: (input: LoginInput, signal?: AbortSignal) => request<Token>('/auth/login', { method: 'POST', body: input, public: true, signal }),
    me: (token: string, signal?: AbortSignal) => request<User>('/me', { token, signal }),
    myRides: (offset: number, signal?: AbortSignal) => request<Ride[]>(`/rides/me?limit=20&offset=${offset}`, { signal }),
    myBookings: (offset: number, signal?: AbortSignal) => request<PassengerBooking[]>(`/bookings/me?limit=20&offset=${offset}`, { signal }),
  };
}
export type ApiClient = ReturnType<typeof createApiClient>;
