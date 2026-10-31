import type { AccountMode } from '../api/types';
import { tokenExpiry } from './validation';

// Tab-scoped session persistence (see docs/frontend/SESSION_PERSISTENCE.md).
// Only the bearer token and the presentation-only travel mode are stored, in
// sessionStorage: it survives a reload of this tab, is never shared with other
// tabs or windows, and is gone when the tab closes. The profile is never stored;
// it is re-verified with GET /me on every restore.
export const SESSION_KEY = 'rideshare.session.v1';

export interface StoredSession { token: string; expiresAt: number; mode: AccountMode | null }

function storage(): Storage | null {
  try { return typeof window === 'undefined' ? null : window.sessionStorage; } catch { return null; }
}

export function saveSession(session: StoredSession): void {
  try { storage()?.setItem(SESSION_KEY, JSON.stringify(session)); } catch { /* Storage full or blocked: session stays in memory only. */ }
}

export function clearSession(): void {
  try { storage()?.removeItem(SESSION_KEY); } catch { /* Nothing to clear. */ }
}

/** Returns a stored, unexpired session, removing anything malformed or expired. */
export function loadSession(now = Date.now()): StoredSession | null {
  let raw: string | null = null;
  try { raw = storage()?.getItem(SESSION_KEY) ?? null; } catch { return null; }
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object') throw new Error('malformed');
    const { token, mode } = value as Record<string, unknown>;
    if (typeof token !== 'string' || token.length > 4096) throw new Error('malformed');
    // Expiry comes from the token itself, not from the stored copy, so editing storage can't extend it.
    const expiresAt = tokenExpiry(token);
    if (expiresAt <= now) throw new Error('expired');
    const safeMode = mode === 'passenger' || mode === 'driver' ? mode : null;
    return { token, expiresAt, mode: safeMode };
  } catch {
    clearSession();
    return null;
  }
}
