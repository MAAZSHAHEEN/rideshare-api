import { describe, expect, it } from 'vitest';
import { passwordError, tokenTiming } from './validation';

describe('backend password policy', () => {
  it('requires 12 characters for registration but accepts legacy login passwords', () => {
    expect(passwordError('short', true)).toBeTruthy();
    expect(passwordError('short', false)).toBeUndefined();
    expect(passwordError('a'.repeat(12), true)).toBeUndefined();
  });
  it('uses UTF-8 bytes and Unicode code points, never truncating or normalizing', () => {
    expect(passwordError('é'.repeat(36), true)).toBeUndefined();
    expect(passwordError('é'.repeat(37), true)).toBeTruthy();
    expect(passwordError('😀'.repeat(11), true)).toBeTruthy();
    expect(passwordError('😀'.repeat(18), true)).toBeUndefined();
    expect(passwordError('😀'.repeat(19), true)).toBeTruthy();
    expect(passwordError('a'.repeat(73), false)).toBeTruthy();
    expect(passwordError('  password with spaces  ', true)).toBeUndefined();
  });
  it('rejects Python whitespace-only input without inventing a trim policy', () => {
    for (const value of ['', ' '.repeat(12), '\u0085'.repeat(12), '\u001c'.repeat(12)]) expect(passwordError(value, true)).toBeTruthy();
    expect(passwordError('\ufeff'.repeat(12), true)).toBeUndefined();
  });
});

describe('expiry hints (D-RC-1: server-issued lifetime, not the device clock)', () => {
  const tok = (claims: unknown) => `h.${btoa(JSON.stringify(claims))}.s`;
  it('returns exp and the server lifetime exp − iat without looking at the device clock', () => {
    // A token issued long ago on the server clock still yields its lifetime: the device clock is irrelevant.
    expect(tokenTiming(tok({ iat: 1_000, exp: 4_600 }))).toEqual({ expMs: 4_600_000, lifetimeMs: 3_600_000 });
    const exp = Math.floor(Date.now() / 1000) + 600;
    expect(tokenTiming(tok({ exp }))).toEqual({ expMs: exp * 1000, lifetimeMs: null });
  });
  it('rejects malformed claims', () => {
    for (const token of ['bad', 'h.@@@.s', tok({}), tok({ exp: '9999999999' }), tok({ exp: 1.5 }), tok({ exp: 0 }), tok({ exp: 100, iat: 100 }), tok({ exp: 100, iat: 200 }), tok({ exp: 100, iat: '1' }), tok(null), tok([1])])
      expect(() => tokenTiming(token), token).toThrow('The sign-in session is invalid or expired. Please sign in again.');
  });
});
