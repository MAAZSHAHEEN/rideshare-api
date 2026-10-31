import type { AccountMode } from '../api/types';
import { sessionClock } from './clock';
import { tokenTiming } from './validation';

// Tab-scoped session persistence (see docs/frontend/SESSION_PERSISTENCE.md).
// Only the bearer token, the device time it was received and the
// presentation-only travel mode are stored, in sessionStorage: it survives a
// reload of this tab, is never shared with other tabs or windows, and is gone
// when the tab closes. The profile is never stored; it is re-verified with
// GET /me on every restore.
export const SESSION_KEY = 'rideshare.session.v1';

export interface StoredSession {
  token: string;
  /** Device wall-clock ms when the token was received from the server (D-RC-1). */
  receivedAt: number;
  mode: AccountMode | null;
}

export interface LoadedSession extends StoredSession {
  /**
   * Hint of the token's remaining lifetime in ms, or null when unknown.
   * Zero or negative means the hint says it has expired; the server still decides
   * (the restore asks GET /me either way).
   */
  remainingMs: number | null;
}

function storage(): Storage | null {
  try { return typeof window === 'undefined' ? null : window.sessionStorage; } catch { return null; }
}

export function saveSession(session: StoredSession): void {
  const { token, receivedAt, mode } = session;
  try { storage()?.setItem(SESSION_KEY, JSON.stringify({ token, receivedAt, mode })); } catch { /* Storage full or blocked: session stays in memory only. */ }
}

export function clearSession(): void {
  try { storage()?.removeItem(SESSION_KEY); } catch { /* Nothing to clear. */ }
}

/**
 * Remaining lifetime hint for a token received at `receivedAt` (device clock).
 * Uses the server-issued lifetime `exp - iat`, so a device clock that is
 * consistently fast or slow does not matter; only elapsed device time does.
 * Without `iat` it falls back to `exp - now` (device clock, best effort).
 */
export function remainingLifetime(token: string, receivedAt: number | null, now = sessionClock.wallNow()): number | null {
  const { expMs, lifetimeMs } = tokenTiming(token);
  if (lifetimeMs === null) return expMs - now;
  if (receivedAt === null) return lifetimeMs;
  // A clock set back since receipt gives a negative elapsed time: count it as 0.
  return lifetimeMs - Math.max(0, now - receivedAt);
}

/** Returns a stored session with its expiry hint, removing anything malformed. */
export function loadSession(now = sessionClock.wallNow()): LoadedSession | null {
  let raw: string | null = null;
  try { raw = storage()?.getItem(SESSION_KEY) ?? null; } catch { return null; }
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('malformed');
    const { token, mode, receivedAt } = value as Record<string, unknown>;
    if (typeof token !== 'string' || token.length > 4096) throw new Error('malformed');
    const received = typeof receivedAt === 'number' && Number.isFinite(receivedAt) && receivedAt > 0 ? receivedAt : null;
    // The lifetime comes from the token itself, never from storage. Editing
    // `receivedAt` can at most stretch the UI hint to one token lifetime; the
    // server's `exp` (401) is still enforced.
    const remainingMs = remainingLifetime(token, received, now);
    const safeMode = mode === 'passenger' || mode === 'driver' ? mode : null;
    return { token, receivedAt: received ?? now, mode: safeMode, remainingMs };
  } catch {
    clearSession();
    return null;
  }
}
