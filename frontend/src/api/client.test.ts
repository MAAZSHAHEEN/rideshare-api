import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApiClient, parseApiError, parseRetryAfter, rideSearchQuery } from './client';

describe('typed API boundary', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('uses relative same-origin URLs by default in production', async () => {
    vi.stubEnv('PROD', true);
    vi.stubEnv('VITE_API_BASE_URL', '');
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('{}'))));
    const api = createApiClient(() => ({ token: 'private', generation: 1 }), vi.fn());
    await api.me('private');
    await api.myRides(0);
    expect(fetch).toHaveBeenNthCalledWith(1, '/me', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer private' }), credentials: 'omit',
    }));
    expect(fetch).toHaveBeenNthCalledWith(2, '/rides/me?limit=20&offset=0', expect.anything());
  });

  it('supports an explicit same-origin base and an intentional external override', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('{}'))));
    for (const base of ['', '/', 'https://api.example.com/']) {
      await createApiClient(() => ({ token: null, generation: 0 }), vi.fn(), base).me('private');
    }
    expect(fetch).toHaveBeenNthCalledWith(1, '/me', expect.anything());
    expect(fetch).toHaveBeenNthCalledWith(2, '/me', expect.anything());
    expect(fetch).toHaveBeenNthCalledWith(3, 'https://api.example.com/me', expect.anything());
    for (const base of ['//untrusted.example.com', 'javascript:alert(1)', 'https://user:pass@example.com', 'https://example.com?secret=1']) {
      expect(() => createApiClient(() => ({ token: null, generation: 0 }), vi.fn(), base)).toThrow();
    }
  });

  it.each(['http://localhost:8000', 'https://development.example.com'])('ignores the production API override %s', async base => {
    vi.stubEnv('PROD', true);
    vi.stubEnv('VITE_API_BASE_URL', base);
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('{}'))));
    await createApiClient(() => ({ token: null, generation: 0 }), vi.fn()).me('private');
    expect(fetch).toHaveBeenCalledWith('/me', expect.anything());
  });

  it('keeps explicit client overrides on the same origin in production', async () => {
    vi.stubEnv('PROD', true);
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('{}'))));
    await createApiClient(() => ({ token: null, generation: 0 }), vi.fn(), 'https://development.example.com').me('private');
    expect(fetch).toHaveBeenCalledWith('/me', expect.anything());
  });

  it('uses exact auth payloads, no bearer on login, and preserves passwords', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ access_token: 'token', token_type: 'bearer' })));
    vi.stubGlobal('fetch', fetcher);
    const api = createApiClient(() => ({ token: 'old', generation: 1 }), vi.fn());
    await api.login({ email: 'a@example.com', password: '  exact password  ' });
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe('http://localhost:8000/auth/login');
    expect(options.headers).not.toHaveProperty('Authorization');
    expect(options.credentials).toBe('omit');
    expect(JSON.parse(options.body).password).toBe('  exact password  ');
  });
  it('passes bearer credentials and generation to the unauthorized callback', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"Invalid token"}', { status: 401 })));
    const unauthorized = vi.fn();
    const credentials = { token: 'private', generation: 7 };
    const api = createApiClient(() => credentials, unauthorized);
    await expect(api.myBookings(20)).rejects.toMatchObject({ status: 401 });
    expect(unauthorized).toHaveBeenCalledWith(credentials);
    expect(fetch).toHaveBeenCalledWith('http://localhost:8000/bookings/me?limit=20&offset=20', expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer private' }) }));
  });
  it('does not terminate an existing session on a failed public login', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"Invalid email or password"}', { status: 401 })));
    const unauthorized = vi.fn();
    await expect(createApiClient(() => ({ token: 'old', generation: 2 }), unauthorized).login({ email: 'a@example.com', password: 'wrong' })).rejects.toMatchObject({ status: 401 });
    expect(unauthorized).not.toHaveBeenCalled();
  });
  it('extracts 422 messages without echoing sensitive validation input', () => {
    const error = parseApiError(422, { detail: [{ loc: ['body', 'password'], msg: 'Too short', input: 'private' }] });
    expect(error.fields).toEqual({ password: 'Too short' });
    expect(JSON.stringify(error)).not.toContain('private');
    expect(parseApiError(409, { detail: 'Registration details already in use' }).message).toBe('Registration details already in use');
    expect(parseApiError(500, { detail: 'internal database details' }).message).not.toContain('database');
  });
  it('reports network errors and does not retry mutations', async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError('network failed'));
    vi.stubGlobal('fetch', fetcher);
    await expect(createApiClient(() => ({ token: null, generation: 0 }), vi.fn()).login({ email: 'a@example.com', password: 'password' })).rejects.toMatchObject({ status: 0 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

describe('passenger journey client (AST-023)', () => {
  const api = () => createApiClient(() => ({ token: 'private', generation: 1 }), vi.fn());
  it('builds the ride search query with server parameter names, omitting blanks', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('[]'))));
    await api().searchRides({ origin: '  Lahore ', destination: '', departureFrom: '2026-11-01T00:00:00+05:00', departureBefore: '2026-11-02T00:00:00+05:00', limit: 20, offset: 40 });
    const url = new URL((fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]);
    expect(url.pathname).toBe('/rides/');
    expect(Object.fromEntries(url.searchParams)).toEqual({ origin: 'Lahore', departure_from: '2026-11-01T00:00:00+05:00', departure_before: '2026-11-02T00:00:00+05:00', limit: '20', offset: '40' });
    // The + in the offset must survive encoding (a raw + would decode as a space server-side).
    expect(url.search).toContain('departure_from=2026-11-01T00%3A00%3A00%2B05%3A00');
  });
  it('encodes free-text places safely', () => {
    expect(rideSearchQuery({ origin: 'Lahore&admin=1', destination: 'Isl#amabad' })).toBe('origin=Lahore%26admin%3D1&destination=Isl%23amabad&limit=20&offset=0');
  });
  it('requests, cancels and reads with the existing endpoints and methods', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('{}'))));
    const client = api();
    await client.getRide(7);
    await client.bookRide(7);
    await client.cancelBooking(70);
    await client.myBookings(0, undefined, { status: 'pending', limit: 100 });
    const calls = (fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.map(([url, init]) => `${init.method} ${url}`)).toEqual([
      'GET http://localhost:8000/rides/7', 'POST http://localhost:8000/bookings/7', 'PATCH http://localhost:8000/bookings/70/cancel',
      'GET http://localhost:8000/bookings/me?limit=100&offset=0&status=pending',
    ]);
    expect(calls[1][1].body).toBeUndefined();
    expect(calls[1][1].headers.Authorization).toBe('Bearer private');
  });
  it.each([0, -1, 1.5, Number.NaN])('rejects invalid ids (%s) without a request', async id => {
    vi.stubGlobal('fetch', vi.fn());
    await expect(api().getRide(id)).rejects.toMatchObject({ status: 404 });
    await expect(api().bookRide(id)).rejects.toMatchObject({ status: 404 });
    expect(fetch).not.toHaveBeenCalled();
  });
  it('exposes Retry-After on 429 and keeps 4xx detail for mapping, never for 5xx', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('{"detail":"Too many requests"}', { status: 429, headers: { 'Retry-After': '17' } }))
      .mockResolvedValueOnce(new Response('{"detail":"No seats available"}', { status: 409 }))
      .mockResolvedValueOnce(new Response('{"detail":"psycopg internal"}', { status: 500 })));
    await expect(api().bookRide(7)).rejects.toMatchObject({ status: 429, retryAfter: 17 });
    await expect(api().bookRide(7)).rejects.toMatchObject({ status: 409, detail: 'No seats available' });
    const failure = await api().bookRide(7).catch(error => error);
    expect(failure.detail).toBeUndefined();
    expect(failure.message).not.toContain('psycopg');
  });
  it('driver journey uses the existing endpoints, methods and query names', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('{}'))));
    const client = api();
    const input = { origin: 'Lahore', destination: 'Islamabad', departure_time: '2030-06-12T09:30:00+05:00', available_seats: 3, fare_per_seat: 2500 };
    await client.createRide(input);
    await client.myRides(20, undefined, { status: 'active', limit: 100 });
    await client.rideBookings(7);
    await client.rideBookings(7, undefined, { status: 'pending', limit: 20, offset: 20 });
    await client.respondToBooking(70, true);
    await client.respondToBooking(71, false);
    await client.cancelRide(7);
    await client.completeRide(7);
    const calls = (fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.map(([url, init]) => `${init.method} ${url}`)).toEqual([
      'POST http://localhost:8000/rides/',
      'GET http://localhost:8000/rides/me?limit=100&offset=20&status=active',
      'GET http://localhost:8000/rides/7/bookings?limit=100&offset=0',
      'GET http://localhost:8000/rides/7/bookings?limit=20&offset=20&status=pending',
      'PATCH http://localhost:8000/bookings/70/respond?accept=true',
      'PATCH http://localhost:8000/bookings/71/respond?accept=false',
      'PATCH http://localhost:8000/rides/7/cancel',
      'PATCH http://localhost:8000/rides/7/complete',
    ]);
    expect(JSON.parse(calls[0][1].body)).toEqual(input);
    expect(calls[0][1].headers['Content-Type']).toBe('application/json');
    expect(calls[4][1].body).toBeUndefined();
  });
  it.each([0, -1, 2.5])('driver calls reject invalid ids (%s) without a request', async id => {
    vi.stubGlobal('fetch', vi.fn());
    await expect(api().rideBookings(id)).rejects.toMatchObject({ status: 404 });
    await expect(api().respondToBooking(id, true)).rejects.toMatchObject({ status: 404 });
    await expect(api().cancelRide(id)).rejects.toMatchObject({ status: 404 });
    await expect(api().completeRide(id)).rejects.toMatchObject({ status: 404 });
    expect(fetch).not.toHaveBeenCalled();
  });
  it('parses Retry-After seconds and HTTP dates, ignoring junk', () => {
    const now = Date.parse('2026-11-01T00:00:00Z');
    expect(parseRetryAfter('5', now)).toBe(5);
    expect(parseRetryAfter('0', now)).toBe(1);
    expect(parseRetryAfter('Sun, 01 Nov 2026 00:01:00 GMT', now)).toBe(60);
    expect(parseRetryAfter('Sat, 31 Oct 2026 00:00:00 GMT', now)).toBeUndefined();
    expect(parseRetryAfter('soon', now)).toBeUndefined();
    expect(parseRetryAfter(null, now)).toBeUndefined();
  });
});
