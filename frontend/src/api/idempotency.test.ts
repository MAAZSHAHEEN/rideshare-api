import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, IDEMPOTENCY_KEY, createApiClient, idempotencyHeaderEnabled } from './client';
import { isRetryableFailure, newIdempotencyKey } from './idempotency';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe('idempotency keys (PR #18 tolerance)', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.restoreAllMocks(); });

  it('mints distinct UUID v4 keys, with a getRandomValues fallback', () => {
    const a = newIdempotencyKey(); const b = newIdempotencyKey();
    expect(a).toMatch(UUID); expect(b).not.toBe(a);
    const real = globalThis.crypto;
    vi.stubGlobal('crypto', { getRandomValues: real.getRandomValues.bind(real) });
    const fallback = newIdempotencyKey();
    expect(fallback).toMatch(UUID);
    expect(fallback).not.toBe(newIdempotencyKey());
  });
  it('only network, 429 and 5xx failures keep the key for a retry', () => {
    expect(isRetryableFailure(new ApiError(0, 'x'))).toBe(true);
    expect(isRetryableFailure(new ApiError(429, 'x'))).toBe(true);
    expect(isRetryableFailure(new ApiError(503, 'x'))).toBe(true);
    for (const status of [400, 401, 403, 404, 409, 422]) expect(isRetryableFailure(new ApiError(status, 'x'))).toBe(false);
    expect(isRetryableFailure(new TypeError('boom'))).toBe(true);
  });
  it('the header is enabled same-origin, or cross-origin only with the flag', () => {
    expect(idempotencyHeaderEnabled('', undefined)).toBe(true);
    expect(idempotencyHeaderEnabled('http://localhost:8000', undefined)).toBe(false);
    expect(idempotencyHeaderEnabled('http://localhost:8000', 'false')).toBe(false);
    expect(idempotencyHeaderEnabled('http://localhost:8000', '1')).toBe(false);
    expect(idempotencyHeaderEnabled('http://localhost:8000', 'true')).toBe(true);
  });
  it('sends the key only on the two mutations, and never an invalid key', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('{}'))));
    const api = createApiClient(() => ({ token: 't', generation: 1 }), vi.fn(), 'http://localhost:8000', { idempotencyKeys: true });
    await api.bookRide(7, undefined, 'k-1');
    await api.respondToBooking(70, true, undefined, 'k-2');
    await api.bookRide(7);
    await api.bookRide(7, undefined, 'has space');
    await api.bookRide(7, undefined, 'x'.repeat(65));
    await api.cancelBooking(70);
    const headers = (fetch as ReturnType<typeof vi.fn>).mock.calls.map(([, init]) => init.headers['Idempotency-Key']);
    expect(headers).toEqual(['k-1', 'k-2', undefined, undefined, undefined, undefined]);
    expect(IDEMPOTENCY_KEY.test('x'.repeat(64))).toBe(true);
  });
  it('a client without the option (split-origin dev default) never sends it', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('{}'))));
    await createApiClient(() => ({ token: 't', generation: 1 }), vi.fn(), 'http://localhost:8000').bookRide(7, undefined, 'k-1');
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls[0][1].headers).not.toHaveProperty('Idempotency-Key');
  });
});
