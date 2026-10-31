export function passwordError(value: string, registering: boolean): string | undefined {
  // Python str.strip() whitespace, matching the backend (not JavaScript trim()).
  if (/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/.test(value)) return 'Enter a password that is not blank.';
  if (registering && Array.from(value).length < 12) return 'Use at least 12 characters.';
  if (new TextEncoder().encode(value).length > 72) return 'Use no more than 72 UTF-8 bytes (some characters use more than one byte).';
  return undefined;
}

export const INVALID_SESSION_MESSAGE = 'The sign-in session is invalid or expired. Please sign in again.';

export interface TokenTiming {
  /** Server-issued `exp`, in ms since the epoch (server clock). */
  expMs: number;
  /** Server-issued lifetime `exp - iat` in ms, or null when the token has no usable `iat`. */
  lifetimeMs: number | null;
}

/**
 * Reads `exp` and `iat` from the token. Never compares them with the device
 * clock: the claims are on the server's clock, so only their difference (the
 * lifetime) is meaningful here (D-RC-1). Expiry is only a UI hint; identity and
 * authorization come from GET /me and the server's 401.
 */
export function tokenTiming(token: string): TokenTiming {
  try {
    const segment = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    const claims: unknown = JSON.parse(atob(segment.padEnd(Math.ceil(segment.length / 4) * 4, '=')));
    if (claims && typeof claims === 'object') {
      const { exp, iat } = claims as Record<string, unknown>;
      if (typeof exp === 'number' && Number.isSafeInteger(exp) && exp > 0) {
        if (iat === undefined) return { expMs: exp * 1000, lifetimeMs: null };
        if (typeof iat === 'number' && Number.isSafeInteger(iat) && iat > 0 && exp > iat) return { expMs: exp * 1000, lifetimeMs: (exp - iat) * 1000 };
      }
    }
  } catch { /* Invalid server response is handled below. */ }
  throw new Error(INVALID_SESSION_MESSAGE);
}
