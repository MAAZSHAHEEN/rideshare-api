import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { AuthProvider, safeReturnPath, useAuth } from './AuthProvider';
import { SESSION_KEY } from './sessionStore';
import type { User } from '../api/types';

const user: User = { id: 1, name: 'Ayesha', email: 'a@example.com', role: 'passenger', allowed_modes: ['passenger', 'driver'] };
const token = () => `h.${btoa(JSON.stringify({ sub: '1', role: 'driver', exp: Math.floor(Date.now() / 1000) + 3600 }))}.s`;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function Probe() {
  const auth = useAuth();
  return <><p>{auth.user ? `${auth.user.name}:${auth.user.role}` : 'Signed out'}</p><p>{auth.notice}</p><p>Mode: {auth.mode ?? "none"}</p>
    <button onClick={() => void auth.login({ email: user.email, password: 'password' }).catch(() => {})}>Login</button>
    <button onClick={auth.logout}>Logout</button>
    <button onClick={() => auth.selectMode("passenger")}>Passenger Mode</button>
    <button onClick={() => auth.selectMode("driver")}>Driver Mode</button>
    <button onClick={() => void auth.api.myBookings(0).catch(() => {})}>Fetch</button></>;
}
function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><AuthProvider><Probe /></AuthProvider></QueryClientProvider>);
  return queryClient;
}

describe('verified sessions', () => {
  it.each(['passenger', 'driver'] as const)('switches a legacy %s account without replacing its token or private cache', async role => {
    const originalToken = token();
    const fetcher = vi.fn().mockResolvedValueOnce(json({ access_token: originalToken, token_type: 'bearer' }))
      .mockResolvedValueOnce(json({ ...user, role })).mockResolvedValueOnce(json([]));
    vi.stubGlobal('fetch', fetcher);
    const client = mount();
    await userEvent.click(screen.getByText('Login'));
    await screen.findByText(`Ayesha:${role}`);
    expect(screen.getByText(`Mode: ${role}`)).toBeInTheDocument();
    client.setQueryData(['my-bookings', user.id], ['booking history']);
    client.setQueryData(['my-rides', user.id], ['ride history']);
    for (const mode of ['driver', 'passenger'] as const) {
      await userEvent.click(screen.getByText(mode === 'driver' ? 'Driver Mode' : 'Passenger Mode'));
      expect(screen.getByText(`Mode: ${mode}`)).toBeInTheDocument();
      expect(screen.getByText(`Ayesha:${role}`)).toBeInTheDocument();
    }
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(client.getQueryData(['my-bookings', user.id])).toEqual(['booking history']);
    expect(client.getQueryData(['my-rides', user.id])).toEqual(['ride history']);
    await userEvent.click(screen.getByText('Fetch'));
    expect(fetcher.mock.calls[2][1].headers.Authorization).toBe(`Bearer ${originalToken}`);
    await userEvent.click(screen.getByText('Logout'));
    expect(screen.getByText('Mode: none')).toBeInTheDocument();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    await userEvent.click(screen.getByText('Driver Mode'));
    expect(screen.getByText('Mode: none')).toBeInTheDocument();
  });

  it('only accepts modes returned by the verified profile; admin has neither', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ access_token: token(), token_type: 'bearer' }))
      .mockResolvedValueOnce(json({ ...user, allowed_modes: ['passenger'] }))
      .mockResolvedValueOnce(json({ access_token: token(), token_type: 'bearer' }))
      .mockResolvedValueOnce(json({ ...user, role: 'admin', allowed_modes: [] }));
    vi.stubGlobal('fetch', fetcher);
    mount();
    await userEvent.click(screen.getByText('Login'));
    await screen.findByText('Ayesha:passenger');
    await userEvent.click(screen.getByText('Driver Mode'));
    expect(screen.getByText('Mode: passenger')).toBeInTheDocument();
    await userEvent.click(screen.getByText('Logout'));
    await userEvent.click(screen.getByText('Login'));
    await screen.findByText('Ayesha:admin');
    for (const label of ['Driver Mode', 'Passenger Mode']) await userEvent.click(screen.getByText(label));
    expect(screen.getByText('Mode: none')).toBeInTheDocument();
  });

  it.each([undefined, ['admin'], ['driver', 'driver']])('rejects invalid profile capabilities %j', async allowed_modes => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ access_token: token(), token_type: 'bearer' }))
      .mockResolvedValueOnce(json({ ...user, allowed_modes })));
    mount();
    await userEvent.click(screen.getByText('Login'));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(screen.getByText('Signed out')).toBeInTheDocument();
  });

  it('uses /me identity rather than JWT role, then clears cache and the tab session on logout', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ access_token: token(), token_type: 'bearer' })).mockResolvedValueOnce(json(user));
    vi.stubGlobal('fetch', fetcher);
    const storage = vi.spyOn(Storage.prototype, 'setItem');
    const client = mount();
    await userEvent.click(screen.getByText('Login'));
    expect(await screen.findByText('Ayesha:passenger')).toBeInTheDocument();
    expect(fetcher.mock.calls[1][0]).toBe('http://localhost:8000/me');
    client.setQueryData(['private'], ['private data']);
    await userEvent.click(screen.getByText('Logout'));
    expect(screen.getByText('Signed out')).toBeInTheDocument();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    // AST-023: only the tab-scoped session key is ever written, never to localStorage, never the profile.
    expect(storage.mock.contexts.every(context => context === window.sessionStorage)).toBe(true);
    expect(storage.mock.calls.map(([key]) => key)).toEqual([SESSION_KEY]);
    expect(storage.mock.calls[0][1]).not.toContain(user.email);
    expect(storage.mock.calls[0][1]).not.toContain(user.name);
    expect(window.sessionStorage.length).toBe(0);
    expect(window.localStorage.length).toBe(0);
  });
  it('never authenticates when identity verification fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ access_token: token(), token_type: 'bearer' })).mockResolvedValueOnce(json({ detail: 'Invalid token' }, 401)));
    mount();
    await userEvent.click(screen.getByText('Login'));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(screen.getByText('Signed out')).toBeInTheDocument();
  });
  it('discards a late identity response after logout', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ access_token: token(), token_type: 'bearer' })).mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; })));
    mount();
    await userEvent.click(screen.getByText('Login'));
    await waitFor(() => expect(finish).toBeDefined());
    await userEvent.click(screen.getByText('Logout'));
    await act(async () => { finish(json(user)); });
    expect(screen.getByText('Signed out')).toBeInTheDocument();
  });
  it('expires on the next protected 401 and removes private cached data', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ access_token: token(), token_type: 'bearer' })).mockResolvedValueOnce(json(user)).mockResolvedValueOnce(json({ detail: 'Invalid token' }, 401)));
    const client = mount();
    await userEvent.click(screen.getByText('Login'));
    await screen.findByText('Ayesha:passenger');
    client.setQueryData(['private'], 'secret');
    await userEvent.click(screen.getByText('Fetch'));
    expect(await screen.findByText('Signed out')).toBeInTheDocument();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    expect(screen.getByText('Your session has ended. Please sign in again.')).toBeInTheDocument();
  });
  it('ignores an old request’s 401 after a newer session starts', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ access_token: token(), token_type: 'bearer' })).mockResolvedValueOnce(json(user))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }))
      .mockResolvedValueOnce(json({ access_token: token(), token_type: 'bearer' })).mockResolvedValueOnce(json({ ...user, name: 'New user' })));
    mount();
    await userEvent.click(screen.getByText('Login'));
    await screen.findByText('Ayesha:passenger');
    await userEvent.click(screen.getByText('Fetch'));
    await userEvent.click(screen.getByText('Logout'));
    await userEvent.click(screen.getByText('Login'));
    await screen.findByText('New user:passenger');
    await act(async () => { finish(json({ detail: 'Invalid token' }, 401)); });
    expect(screen.getByText('New user:passenger')).toBeInTheDocument();
  });
  it('expires an idle session using the JWT expiry timer', async () => {
    const expiry = Math.floor(Date.now() / 1000) + 3600;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ access_token: `h.${btoa(JSON.stringify({ exp: expiry }))}.s`, token_type: 'bearer' })).mockResolvedValueOnce(json(user)));
    const client = mount();
    await userEvent.click(screen.getByText('Login'));
    await screen.findByText('Ayesha:passenger');
    client.setQueryData(['private'], 'secret');
    vi.spyOn(Date, 'now').mockReturnValue((expiry + 1) * 1000);
    act(() => window.dispatchEvent(new Event('focus')));
    expect(screen.getByText('Signed out')).toBeInTheDocument();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    expect(screen.getByText('Your session has expired. Please sign in again.')).toBeInTheDocument();
  });
});

describe('tab session persistence (AST-023)', () => {
  function mountProbe() {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<QueryClientProvider client={queryClient}><AuthProvider><Probe /></AuthProvider></QueryClientProvider>);
    return { queryClient, view };
  }
  it('restores a saved session after a reload by re-verifying it with /me', async () => {
    const saved = token();
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token: saved, expiresAt: 1, mode: 'driver' }));
    const fetcher = vi.fn().mockResolvedValueOnce(json(user)).mockResolvedValueOnce(json([]));
    vi.stubGlobal('fetch', fetcher);
    mountProbe();
    expect(await screen.findByText('Ayesha:passenger')).toBeInTheDocument();
    expect(fetcher.mock.calls[0][0]).toBe('http://localhost:8000/me');
    expect(fetcher.mock.calls[0][1].headers.Authorization).toBe(`Bearer ${saved}`);
    // The presentation-only mode survives too, when the verified profile allows it.
    expect(screen.getByText('Mode: driver')).toBeInTheDocument();
    await userEvent.click(screen.getByText('Fetch'));
    expect(fetcher.mock.calls[1][1].headers.Authorization).toBe(`Bearer ${saved}`);
  });
  it('drops an expired saved token without any network call', async () => {
    const expired = `h.${btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) - 5 }))}.s`;
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token: expired, expiresAt: Date.now() + 999_999, mode: 'passenger' }));
    vi.stubGlobal('fetch', vi.fn());
    mountProbe();
    expect(screen.getByText('Signed out')).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });
  it.each(['not json', '{"token":5}', '[]', 'null'])('drops malformed saved data %s', async raw => {
    window.sessionStorage.setItem(SESSION_KEY, raw);
    vi.stubGlobal('fetch', vi.fn());
    mountProbe();
    expect(screen.getByText('Signed out')).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });
  it('clears the saved session when the server rejects it (401)', async () => {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token: token(), expiresAt: 1, mode: null }));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ detail: 'Invalid token' }, 401)));
    mountProbe();
    expect(await screen.findByText('Your session has ended. Please sign in again.')).toBeInTheDocument();
    expect(screen.getByText('Signed out')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });
  it('does not trust a saved profile that fails validation', async () => {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token: token(), expiresAt: 1, mode: null }));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ ...user, role: 'admin' })));
    mountProbe();
    await waitFor(() => expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull());
    expect(screen.getByText('Signed out')).toBeInTheDocument();
  });
  it('a protected 401 during a restored session clears the saved token', async () => {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token: token(), expiresAt: 1, mode: null }));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(user)).mockResolvedValueOnce(json({ detail: 'Invalid token' }, 401)));
    mountProbe();
    await screen.findByText('Ayesha:passenger');
    await userEvent.click(screen.getByText('Fetch'));
    expect(await screen.findByText('Signed out')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });
});

describe('post-login return path', () => {
  it.each([
    ['/passenger?ride=7', '/passenger?ride=7'],
    ['/passenger?view=search&from=Lahore', '/passenger?view=search&from=Lahore'],
    ['/passenger/search', '/passenger/search'],
    ['/passenger/search?from=Lahore&page=2', '/passenger/search?from=Lahore&page=2'],
    ['/passenger/bookings?status=pending', '/passenger/bookings?status=pending'],
    ['/passenger/rides/7', '/passenger/rides/7'],
    ['/driver', '/driver'],
    ['/driver/rides', '/driver/rides'],
    ['/driver/rides?filter=upcoming', '/driver/rides?filter=upcoming'],
    ['/driver/rides/new', '/driver/rides/new'],
    ['/driver/rides/7', '/driver/rides/7'],
    ['/account', '/account'],
  ])('accepts in-app path %s', (value, expected) => expect(safeReturnPath(value)).toBe(expected));
  it.each([undefined, 5, '', 'passenger', '//evil.example.com/passenger', '/\\evil.example.com', 'https://evil.example.com/passenger', 'javascript:alert(1)', '/login', '/passengerx', '/%2F%2Fevil.example.com', '/driverx', '/driver/', '/driver/rides/0', '/driver/rides/7/x', '/driver/admin', '/driver/rides//evil.example.com', '/driver/rides/%2F%2Fevil',
    '/passenger/', '/passenger/rides', '/passenger/rides/0', '/passenger/rides/7/x', '/passenger/admin', '/passenger/searchx', '/passenger/rides//evil.example.com', '/passenger/rides/%2F%2Fevil', '/passenger/bookings/1'])(
    'rejects %j', value => expect(safeReturnPath(value)).toBeNull());
});
