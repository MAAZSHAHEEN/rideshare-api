import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { createApiClient } from '../api/client';
import type { ApiClient, Credentials } from '../api/client';
import type { LoginInput, User } from '../api/types';
import { tokenExpiry } from './validation';

interface AuthContextValue {
  user: User | null; api: ApiClient; notice: string;
  login: (input: LoginInput) => Promise<User>;
  logout: () => void;
}
const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const credentials = useRef<Credentials>({ token: null, generation: 0 });
  const loginRequest = useRef<AbortController | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [expiry, setExpiry] = useState<number | null>(null);
  const [notice, setNotice] = useState('');

  const endSession = useCallback((message = '') => {
    credentials.current = { token: null, generation: credentials.current.generation + 1 };
    loginRequest.current?.abort();
    void queryClient.cancelQueries();
    queryClient.clear();
    setUser(null);
    setExpiry(null);
    setNotice(message);
  }, [queryClient]);

  const api = useMemo(() => createApiClient(
    () => credentials.current,
    (request) => {
      if (request.generation === credentials.current.generation) endSession('Your session has ended. Please sign in again.');
    },
  ), [endSession]);

  const login = useCallback(async (input: LoginInput) => {
    endSession();
    const generation = credentials.current.generation;
    const controller = new AbortController();
    loginRequest.current = controller;
    const result = await api.login(input, controller.signal);
    if (typeof result.access_token !== 'string' || typeof result.token_type !== 'string' || result.token_type.toLowerCase() !== 'bearer') throw new Error('The service returned an invalid sign-in response.');
    const identity = await api.me(result.access_token, controller.signal);
    if (!Number.isInteger(identity.id) || identity.id < 1 || typeof identity.name !== 'string' || typeof identity.email !== 'string'
        || !['passenger', 'driver', 'admin'].includes(identity.role)) throw new Error('The service returned an invalid profile.');
    const expiresAt = tokenExpiry(result.access_token);
    if (controller.signal.aborted || credentials.current.generation !== generation) throw new DOMException('Sign-in cancelled', 'AbortError');
    credentials.current = { token: result.access_token, generation };
    setUser(identity);
    setExpiry(expiresAt);
    return identity;
  }, [api, endSession]);

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

  return <AuthContext.Provider value={{ user, api, login, logout: () => endSession(), notice }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error('AuthProvider is required');
  return value;
}
export function dashboardPath(user: User) {
  return user.role === 'admin' ? '/account' : `/${user.role}`;
}
