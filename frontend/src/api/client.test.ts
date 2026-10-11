import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApiClient, parseApiError } from './client';

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
