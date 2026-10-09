export function passwordError(value: string, registering: boolean): string | undefined {
  // Python str.strip() whitespace, matching the backend (not JavaScript trim()).
  if (/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/.test(value)) return 'Enter a password that is not blank.';
  if (registering && Array.from(value).length < 12) return 'Use at least 12 characters.';
  if (new TextEncoder().encode(value).length > 72) return 'Use no more than 72 UTF-8 bytes (some characters use more than one byte).';
  return undefined;
}

// Expiry is only a UI timer. Identity and authorization come from GET /me.
export function tokenExpiry(token: string): number {
  try {
    const segment = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    const claims: unknown = JSON.parse(atob(segment.padEnd(Math.ceil(segment.length / 4) * 4, '=')));
    const exp = claims && typeof claims === 'object' && 'exp' in claims ? claims.exp : null;
    if (typeof exp === 'number' && Number.isSafeInteger(exp) && exp > Date.now() / 1000) return exp * 1000;
  } catch { /* Invalid server response is handled below. */ }
  throw new Error('The sign-in session is invalid or expired. Please sign in again.');
}
