import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ApiError, createApiClient } from '../api/client';
import type { ApiClient, Credentials } from '../api/client';
import type { AccountMode, LoginInput, User } from '../api/types';
import { tokenExpiry } from './validation';
import { clearSession, loadSession, saveSession } from './sessionStore';

interface AuthContextValue {
  user: User | null; api: ApiClient; notice: string;
  mode: AccountMode | null;
  /** True while a session saved in this tab is being re-verified after a reload. */
  restoring: boolean;
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
  const [expiry, setExpiry] = useState<number | null>(null);
  const [notice, setNotice] = useState('');
  const [mode, setMode] = useState<AccountMode | null>(null);
  const [loggedOut, setLoggedOut] = useState(false);

  const endSession = useCallback((message = '') => {
    credentials.current = { token: null, generation: credentials.current.generation + 1 };
    loginRequest.current?.abort();
    clearSession();
    void queryClient.cancelQueries();
    queryClient.clear();
    setUser(null);
    setMode(null);
    setExpiry(null);
    setRestoring(false);
    setNotice(message);
  }, [queryClient]);

  const api = useMemo(() => createApiClient(
    () => credentials.current,
    (request) => {
      if (request.generation === credentials.current.generation) endSession('Your session has ended. Please sign in again.');
    },
  ), [endSession]);

  const startSession = useCallback((token: string, identity: User, expiresAt: number, generation: number, preferred: AccountMode | null) => {
    credentials.current = { token, generation };
    const selected = preferred && identity.role !== 'admin' && identity.allowed_modes.includes(preferred) ? preferred : initialMode(identity);
    setUser(identity);
    setMode(selected);
    setExpiry(expiresAt);
    setRestoring(false);
    setLoggedOut(false);
    saveSession({ token, expiresAt, mode: selected });
  }, []);

  // Re-verify a session saved in this tab. Identity always comes from GET /me.
  useEffect(() => {
    if (!saved) return;
    const controller = new AbortController();
    const generation = credentials.current.generation;
    api.me(saved.token, controller.signal).then(identity => {
      if (controller.signal.aborted || credentials.current.generation !== generation) return;
      if (!validIdentity(identity)) { endSession('Please sign in again.'); return; }
      startSession(saved.token, identity, saved.expiresAt, generation, saved.mode);
    }).catch((reason: unknown) => {
      if (controller.signal.aborted || credentials.current.generation !== generation) return;
      endSession(reason instanceof ApiError && reason.status === 401
        ? 'Your session has ended. Please sign in again.'
        : 'We couldn’t restore your session. Please sign in again.');
    });
    return () => controller.abort();
  }, [saved, api, endSession, startSession]);

  const login = useCallback(async (input: LoginInput) => {
    endSession();
    const generation = credentials.current.generation;
    const controller = new AbortController();
    loginRequest.current = controller;
    const result = await api.login(input, controller.signal);
    if (typeof result.access_token !== 'string' || typeof result.token_type !== 'string' || result.token_type.toLowerCase() !== 'bearer') throw new Error('The service returned an invalid sign-in response.');
    const identity = await api.me(result.access_token, controller.signal);
    if (!validIdentity(identity)) throw new Error('The service returned an invalid profile.');
    const expiresAt = tokenExpiry(result.access_token);
    if (controller.signal.aborted || credentials.current.generation !== generation) throw new DOMException('Sign-in cancelled', 'AbortError');
    startSession(result.access_token, identity, expiresAt, generation, null);
    return identity;
  }, [api, endSession, startSession]);

  // Presentation state only: no token exchange or account-role mutation.
  const selectMode = useCallback((next: AccountMode) => {
    if (user && user.role !== 'admin' && user.allowed_modes.includes(next)) {
      setMode(next);
      const token = credentials.current.token;
      if (token && expiry) saveSession({ token, expiresAt: expiry, mode: next });
    }
  }, [user, expiry]);

  useEffect(() => {
    if (!expiry) return;
    let timer: ReturnType<typeof setTimeout>;
    function check() {
      clearTimeout(timer);
      const remaining = expiry! - Date.now();
      if (remaining <= 0) endSession('Your session has expired. Please sign in again.');
      else timer = setTimeout(check, Math.min(remaining, 2_147_483_647));
    }
    check();
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', check);
    return () => { clearTimeout(timer); window.removeEventListener('focus', check); document.removeEventListener('visibilitychange', check); };
  }, [expiry, endSession]);
  useEffect(() => () => { loginRequest.current?.abort(); }, []);

  return <AuthContext.Provider value={{ user, api, login, logout: () => { endSession(); setLoggedOut(true); }, notice, mode, selectMode, restoring, loggedOut }}>{children}</AuthContext.Provider>;
}

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

// Driver sub-routes are listed exactly (M3); nothing else under /driver/ is a return target.
const RETURNABLE = /^\/(?:passenger|account|driver(?:\/rides(?:\/(?:new|[1-9]\d{0,9}))?)?)(?:$|[?#])/;
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
