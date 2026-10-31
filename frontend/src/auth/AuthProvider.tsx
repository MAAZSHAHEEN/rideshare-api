import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ApiError, createApiClient } from '../api/client';
import type { ApiClient, Credentials } from '../api/client';
import type { AccountMode, LoginInput, User } from '../api/types';
import { tokenTiming } from './validation';
import { clearSession, loadSession, remainingLifetime, saveSession } from './sessionStore';
import { sessionClock } from './clock';

export const EXPIRED_MESSAGE = 'Your session has expired. Please sign in again.';
export const ENDED_MESSAGE = 'Your session has ended. Please sign in again.';
/** How long before the expiry hint a server 401 is reported as "expired" rather than "ended". */
const EXPIRY_SLACK_MS = 60_000;

interface AuthContextValue {
  user: User | null; api: ApiClient; notice: string;
  mode: AccountMode | null;
  /** True while a session saved in this tab is being re-verified after a reload. */
  restoring: boolean;
  /**
   * True when the restore could not reach the server (network error, timeout,
   * 5xx, 429). The saved session is kept and the restore is retried (D-RC-2).
   */
  restoreFailed: boolean;
  retryRestore: () => void;
  /** True after the visitor chose to log out: route guards then don't offer a return path. */
  loggedOut: boolean;
  selectMode: (mode: AccountMode) => void;
  login: (input: LoginInput) => Promise<User>;
  logout: () => void;
}
const AuthContext = createContext<AuthContextValue | null>(null);

function validIdentity(identity: User): boolean {
  return Number.isInteger(identity.id) && identity.id >= 1 && typeof identity.name === 'string' && typeof identity.email === 'string'
    && ['passenger', 'driver', 'admin'].includes(identity.role)
    && Array.isArray(identity.allowed_modes)
    && !identity.allowed_modes.some(value => !['passenger', 'driver'].includes(value))
    && new Set(identity.allowed_modes).size === identity.allowed_modes.length
    && !(identity.role === 'admin' && identity.allowed_modes.length !== 0);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const credentials = useRef<Credentials>({ token: null, generation: 0 });
  const loginRequest = useRef<AbortController | null>(null);
  // Read once per mount: a reload of this tab may carry a saved session.
  const [saved] = useState(() => loadSession());
  const [restoring, setRestoring] = useState(Boolean(saved));
  const [user, setUser] = useState<User | null>(null);
  // Expiry hint on the monotonic clock (performance.now), or null when unknown.
  const [deadline, setDeadline] = useState<number | null>(null);
  const deadlineRef = useRef<number | null>(null);
  const receivedAtRef = useRef<number>(0);
  const [restoreFailed, setRestoreFailed] = useState(false);
  const [restoreAttempt, setRestoreAttempt] = useState(0);
  const [notice, setNotice] = useState('');
  const [mode, setMode] = useState<AccountMode | null>(null);
  const [loggedOut, setLoggedOut] = useState(false);

  // Set once the saved session has been cleared (sign-out, 401, new sign-in): it is never restored again.
  const restoreAbandoned = useRef(false);
  const endSession = useCallback((message = '') => {
    restoreAbandoned.current = true;
    credentials.current = { token: null, generation: credentials.current.generation + 1 };
    loginRequest.current?.abort();
    clearSession();
    void queryClient.cancelQueries();
    queryClient.clear();
    setUser(null);
    setMode(null);
    deadlineRef.current = null;
    setDeadline(null);
    setRestoring(false);
    setRestoreFailed(false);
    setNotice(message);
  }, [queryClient]);

  const api = useMemo(() => createApiClient(
    () => credentials.current,
    (request) => {
      if (request.generation !== credentials.current.generation) return;
      // The server's 401 is the authority. The hint only picks the copy.
      const hint = deadlineRef.current;
      endSession(hint !== null && sessionClock.monoNow() >= hint - EXPIRY_SLACK_MS ? EXPIRED_MESSAGE : ENDED_MESSAGE);
    },
  ), [endSession]);

  const startSession = useCallback((token: string, identity: User, remainingMs: number | null, receivedAt: number, generation: number, preferred: AccountMode | null) => {
    credentials.current = { token, generation };
    const selected = preferred && identity.role !== 'admin' && identity.allowed_modes.includes(preferred) ? preferred : initialMode(identity);
    const next = remainingMs !== null && remainingMs > 0 ? sessionClock.monoNow() + remainingMs : null;
    deadlineRef.current = next;
    receivedAtRef.current = receivedAt;
    setUser(identity);
    setMode(selected);
    setDeadline(next);
    setRestoring(false);
    setRestoreFailed(false);
    setLoggedOut(false);
    saveSession({ token, receivedAt, mode: selected });
  }, []);

  // Re-verify a session saved in this tab. Identity always comes from GET /me.
  // Only the server's verdict (401/403) or an invalid profile ends the session;
  // an unreachable server keeps it and offers a retry (D-RC-2).
  useEffect(() => {
    if (!saved) return;
    const controller = new AbortController();
    const generation = credentials.current.generation;
    setRestoreFailed(false);
    api.me(saved.token, controller.signal).then(identity => {
      if (controller.signal.aborted || credentials.current.generation !== generation) return;
      if (!validIdentity(identity)) { endSession('Please sign in again.'); return; }
      const hint = saved.remainingMs;
      if (hint !== null && hint <= 0) {
        // The server accepted a token the hint called expired: the device clock
        // moved since receipt. Trust the server; restart the hint from now.
        startSession(saved.token, identity, null, sessionClock.wallNow(), generation, saved.mode);
        return;
      }
      // Re-derive the hint now: the restore may have waited on retries.
      startSession(saved.token, identity, remainingLifetime(saved.token, saved.receivedAt), saved.receivedAt, generation, saved.mode);
    }).catch((reason: unknown) => {
      if (controller.signal.aborted || credentials.current.generation !== generation) return;
      // A reload (or navigation) while /me is in flight aborts the fetch as the page
      // unloads. That is not a restore failure: clearing the saved session here
      // would sign the visitor out of the page they are reloading into.
      if (pageUnloading()) return;
      if (reason instanceof ApiError && (reason.status === 401 || reason.status === 403)) {
        endSession(reason.status === 401 && saved.remainingMs !== null && saved.remainingMs <= EXPIRY_SLACK_MS ? EXPIRED_MESSAGE : ENDED_MESSAGE);
        return;
      }
      // Network error, timeout, 5xx, 429 or anything else: the server never said
      // the session is invalid. Keep it (still stored) and show the retry state.
      setRestoreFailed(true);
    });
    return () => controller.abort();
  }, [saved, restoreAttempt, api, endSession, startSession]);

  const retryRestore = useCallback(() => {
    if (saved && !restoreAbandoned.current && credentials.current.token === null) setRestoreAttempt(value => value + 1);
  }, [saved]);

  // While the server is unreachable, retry with backoff and as soon as the
  // browser reports it is back online. No navigation happens, so there is no loop.
  const failures = useRef(0);
  useEffect(() => {
    if (!restoreFailed) return;
    const delay = Math.min(30_000, 5_000 * 2 ** failures.current);
    failures.current += 1;
    const timer = setTimeout(retryRestore, delay);
    window.addEventListener('online', retryRestore);
    return () => { clearTimeout(timer); window.removeEventListener('online', retryRestore); };
  }, [restoreFailed, retryRestore]);

  const login = useCallback(async (input: LoginInput) => {
    endSession();
    const generation = credentials.current.generation;
    const controller = new AbortController();
    loginRequest.current = controller;
    const result = await api.login(input, controller.signal);
    // Receipt time on the device clock, taken before any further round trip (D-RC-1).
    const receivedAt = sessionClock.wallNow();
    const receivedMono = sessionClock.monoNow();
    if (typeof result.access_token !== 'string' || typeof result.token_type !== 'string' || result.token_type.toLowerCase() !== 'bearer') throw new Error('The service returned an invalid sign-in response.');
    // Server-issued exp − iat; the device clock is never compared with exp here.
    const { expMs, lifetimeMs } = tokenTiming(result.access_token);
    const identity = await api.me(result.access_token, controller.signal);
    if (!validIdentity(identity)) throw new Error('The service returned an invalid profile.');
    if (controller.signal.aborted || credentials.current.generation !== generation) throw new DOMException('Sign-in cancelled', 'AbortError');
    const sinceReceipt = Math.max(0, sessionClock.monoNow() - receivedMono);
    // Without iat, fall back to exp − now only when that is positive; otherwise no hint (the server's 401 decides).
    const fallback = expMs - receivedAt;
    const remaining = lifetimeMs !== null ? lifetimeMs - sinceReceipt : fallback > 0 ? fallback - sinceReceipt : null;
    startSession(result.access_token, identity, remaining, receivedAt, generation, null);
    return identity;
  }, [api, endSession, startSession]);

  // Presentation state only: no token exchange or account-role mutation.
  const selectMode = useCallback((next: AccountMode) => {
    if (user && user.role !== 'admin' && user.allowed_modes.includes(next)) {
      setMode(next);
      const token = credentials.current.token;
      if (token) saveSession({ token, receivedAt: receivedAtRef.current, mode: next });
    }
  }, [user]);

  useEffect(() => {
    if (deadline === null) return;
    let timer: ReturnType<typeof setTimeout>;
    // Monotonic clock: setting the device clock forward or back does not move this.
    function check() {
      clearTimeout(timer);
      const remaining = deadline! - sessionClock.monoNow();
      if (remaining <= 0) endSession(EXPIRED_MESSAGE);
      else timer = setTimeout(check, Math.min(remaining, 2_147_483_647));
    }
    check();
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', check);
    return () => { clearTimeout(timer); window.removeEventListener('focus', check); document.removeEventListener('visibilitychange', check); };
  }, [deadline, endSession]);
  useEffect(() => () => { loginRequest.current?.abort(); }, []);

  return <AuthContext.Provider value={{ user, api, login, logout: () => { endSession(); setLoggedOut(true); }, notice, mode, selectMode, restoring, restoreFailed, retryRestore, loggedOut }}>{children}</AuthContext.Provider>;
}

// Set when this document starts unloading (reload, navigation); reset if it is restored from the back/forward cache.
let unloading = false;
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => { unloading = true; });
  window.addEventListener('pagehide', () => { unloading = true; });
  window.addEventListener('pageshow', () => { unloading = false; });
}
/** True once the page is unloading; exported for tests. */
export function pageUnloading() { return unloading; }
/** Test hook: reset the unloading flag. */
export function resetPageUnloadingForTests() { unloading = false; }

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error('AuthProvider is required');
  return value;
}
function initialMode(user: User): AccountMode | null {
  if (user.role === 'admin') return null;
  return user.allowed_modes.includes(user.role) ? user.role : user.allowed_modes[0] ?? null;
}

export function dashboardPath(user: User, mode?: AccountMode | null) {
  const selected = mode && user.allowed_modes.includes(mode) ? mode : initialMode(user);
  return user.role === 'admin' || !selected ? '/account' : `/${selected}`;
}

// Passenger and driver sub-routes are listed exactly; nothing else under /passenger/ or /driver/ is a return target.
// (`/passenger?view=…` / `?ride=…` stay returnable: the passenger area redirects them to the path routes.)
const RETURNABLE = /^\/(?:passenger(?:\/(?:search|bookings|rides\/[1-9]\d{0,9}))?|account|driver(?:\/rides(?:\/(?:new|[1-9]\d{0,9}))?)?)(?:$|[?#])/;
/**
 * A same-origin path to return to after signing in, or null. Only in-app
 * dashboard paths qualify; protocol-relative, absolute and backslash tricks
 * are rejected. Route guards still re-check the role after navigation.
 */
export function safeReturnPath(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 512 || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return null;
  try {
    const url = new URL(value, 'https://rideshare.invalid');
    if (url.origin !== 'https://rideshare.invalid') return null;
    const path = `${url.pathname}${url.search}`;
    return RETURNABLE.test(path) ? path : null;
  } catch { return null; }
}
