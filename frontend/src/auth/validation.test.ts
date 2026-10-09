import { describe, expect, it } from 'vitest';
import { passwordError, tokenExpiry } from './validation';

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

describe('expiry hints', () => {
  it('accepts future integer expiry, rejecting malformed and expired tokens', () => {
    const exp = Math.floor(Date.now() / 1000) + 600;
    expect(tokenExpiry(`h.${btoa(JSON.stringify({ exp }))}.s`)).toBe(exp * 1000);
    for (const token of ['bad', 'h.@@@.s', `h.${btoa('{"exp":1}')}.s`, `h.${btoa('{"exp":"9999999999"}')}.s`]) expect(() => tokenExpiry(token)).toThrow();
  });
});
