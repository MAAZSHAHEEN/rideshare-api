import { describe, expect, it, vi } from 'vitest';
import { createApiClient, parseApiError } from './client';

describe('typed API boundary', () => {
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
