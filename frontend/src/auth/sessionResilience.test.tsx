// D-RC-1 (expiry must not depend on the device clock) and D-RC-2 (an unreachable
// server during restore must not sign the visitor out). See RC_ACCEPTANCE.md §2.
import { act, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { AuthProvider, resetPageUnloadingForTests, useAuth } from './AuthProvider';
import { SESSION_KEY } from './sessionStore';
import { SessionRestoring } from '../components/SessionRestoring';
import type { User } from '../api/types';

const HOUR = 3_600_000;
const user: User = { id: 1, name: 'Ayesha', email: 'a@example.com', role: 'passenger', allowed_modes: ['passenger'] };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
/** A token as the server issues it: iat/exp on the SERVER clock (`serverNow`), 60-minute lifetime. */
const serverToken = (serverNow: number, lifetimeSeconds = 3600) => {
  const iat = Math.floor(serverNow / 1000);
  return `h.${btoa(JSON.stringify({ sub: '1', role: 'passenger', iat, exp: iat + lifetimeSeconds }))}.s`;
};

function Probe() {
  const auth = useAuth();
  return <><p>{auth.user ? `Signed in as ${auth.user.name}` : 'Signed out'}</p><p data-testid="notice">{auth.notice}</p>
    <button onClick={() => void auth.login({ email: user.email, password: 'password' }).catch((error: Error) => { document.title = error.message; })}>Login</button>
    <button onClick={() => void auth.api.myBookings(0).catch(() => {})}>Fetch</button>
    {auth.restoring && <SessionRestoring />}</>;
}
function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><AuthProvider><Probe /></AuthProvider></QueryClientProvider>);
}
/** Fake Date, timers AND performance.now (the monotonic clock), with the device wall clock skewed by `skewMs`. */
function fakeClocks(skewMs: number) {
  const serverNow = Date.now();
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance'], shouldAdvanceTime: true });
  vi.setSystemTime(serverNow + skewMs);
  return serverNow;
}
async function advance(ms: number) { await act(async () => { vi.advanceTimersByTime(ms); }); }
async function signIn(serverNow: number) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ access_token: serverToken(serverNow), token_type: 'bearer' })).mockResolvedValueOnce(json(user)));
  mount();
  fireEvent.click(screen.getByText('Login'));
  expect(await screen.findByText('Signed in as Ayesha')).toBeInTheDocument();
}

describe('D-RC-1: token expiry does not depend on the device clock', () => {
  it.each([['2 h fast', 2 * HOUR], ['2 h slow', -2 * HOUR], ['in sync', 0]])('device clock %s: signed in for exactly the server lifetime', async (_label, skew) => {
    const serverNow = fakeClocks(skew);
    await signIn(serverNow);
    await advance(HOUR - 1_000);
    expect(screen.getByText('Signed in as Ayesha')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).not.toBeNull();
    await advance(1_000);
    expect(screen.getByText('Signed out')).toBeInTheDocument();
    expect(screen.getByTestId('notice')).toHaveTextContent('Your session has expired. Please sign in again.');
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it('exp boundary: just before the lifetime ends still signed in, at the boundary expired', async () => {
    const serverNow = fakeClocks(0);
    await signIn(serverNow);
    // shouldAdvanceTime lets a few real ms pass during the sign-in round trip, hence the 250 ms margin.
    await advance(HOUR - 250);
    expect(screen.getByText('Signed in as Ayesha')).toBeInTheDocument();
    await advance(250);
    expect(screen.getByText('Signed out')).toBeInTheDocument();
  });

  it('the device clock jumping forward 2 h (then focus) does not sign out; jumping back 2 h does not extend the session', async () => {
    const serverNow = fakeClocks(0);
    await signIn(serverNow);
    act(() => { vi.setSystemTime(Date.now() + 2 * HOUR); });
    fireEvent.focus(window);
    document.dispatchEvent(new Event('visibilitychange'));
    expect(screen.getByText('Signed in as Ayesha')).toBeInTheDocument();
    act(() => { vi.setSystemTime(Date.now() - 4 * HOUR); });
    await advance(HOUR);
    fireEvent.focus(window);
    expect(screen.getByText('Signed out')).toBeInTheDocument();
    expect(screen.getByTestId('notice')).toHaveTextContent('Your session has expired.');
  });

  it('a reload with a consistently fast device clock restores and keeps the remaining lifetime', async () => {
    const serverNow = fakeClocks(2 * HOUR);
    const deviceNow = Date.now();
    // Received 10 minutes ago (device clock); the token's exp is already "past" by the device clock.
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token: serverToken(serverNow - 10 * 60_000), receivedAt: deviceNow - 10 * 60_000, mode: 'passenger' }));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json(user)));
    mount();
    expect(await screen.findByText('Signed in as Ayesha')).toBeInTheDocument();
    await advance(49 * 60_000);
    expect(screen.getByText('Signed in as Ayesha')).toBeInTheDocument();
    await advance(2 * 60_000);
    expect(screen.getByText('Signed out')).toBeInTheDocument();
  });

  it('sign-in with a token lacking iat and the device clock past exp still succeeds (server decides via 401)', async () => {
    fakeClocks(2 * HOUR);
    const noIat = `h.${btoa(JSON.stringify({ exp: Math.floor((Date.now() - 2 * HOUR) / 1000) + 3600 }))}.s`;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ access_token: noIat, token_type: 'bearer' })).mockResolvedValueOnce(json(user))
      .mockResolvedValueOnce(json({ detail: 'Token expired' }, 401)));
    mount();
    fireEvent.click(screen.getByText('Login'));
    expect(await screen.findByText('Signed in as Ayesha')).toBeInTheDocument();
    await advance(10 * HOUR);
    expect(screen.getByText('Signed in as Ayesha')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Fetch'));
    expect(await screen.findByText('Signed out')).toBeInTheDocument();
  });

  it('a server 401 is the authority: near the expiry hint it reads "expired", otherwise "ended"', async () => {
    const serverNow = fakeClocks(0);
    await signIn(serverNow);
    vi.mocked(fetch).mockResolvedValueOnce(json({ detail: 'Invalid token' }, 401));
    await advance(HOUR - 30_000);
    fireEvent.click(screen.getByText('Fetch'));
    expect(await screen.findByText('Signed out')).toBeInTheDocument();
    expect(screen.getByTestId('notice')).toHaveTextContent('Your session has expired. Please sign in again.');
  });
});

describe('D-RC-2: restore signs out only on the server’s verdict', () => {
  const store = () => {
    const saved = JSON.stringify({ token: serverToken(Date.now()), receivedAt: Date.now(), mode: 'passenger' });
    window.sessionStorage.setItem(SESSION_KEY, saved);
    return saved;
  };
  it.each([[401, 'Your session has ended. Please sign in again.'], [403, 'Your session has ended. Please sign in again.']])('%i clears the session', async (status, message) => {
    store();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ detail: 'Not allowed' }, status)));
    mount();
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });
  it('an invalid profile clears the session', async () => {
    store();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ ...user, id: 0 })));
    mount();
    expect(await screen.findByText('Please sign in again.')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });
  it.each([
    ['503', () => Promise.resolve(json({ detail: 'down' }, 503))],
    ['500', () => Promise.resolve(json({ detail: 'secret stack' }, 500))],
    ['429', () => Promise.resolve(json({ detail: 'slow down' }, 429))],
    ['network error', () => Promise.reject(new TypeError('Failed to fetch'))],
    ['timeout', () => Promise.reject(new DOMException('The operation timed out.', 'TimeoutError'))],
  ])('%s keeps the session, shows "Can’t reach RideShare" with Retry, and recovers', async (_label, failure) => {
    const saved = store();
    vi.stubGlobal('fetch', vi.fn().mockImplementationOnce(failure).mockResolvedValueOnce(json(user)));
    mount();
    expect(await screen.findByRole('heading', { name: 'Can’t reach RideShare' })).toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBe(saved);
    expect(screen.getByText('Signed out')).toBeInTheDocument();
    expect(screen.queryByText(/secret stack/)).toBeNull();
    expect(screen.getByTestId('notice')).toBeEmptyDOMElement();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Signed in as Ayesha')).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it('retries automatically when the browser comes back online, and on a backoff timer', async () => {
    store();
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'], shouldAdvanceTime: true });
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch')).mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(json(user)));
    mount();
    expect(await screen.findByRole('heading', { name: 'Can’t reach RideShare' })).toBeInTheDocument();
    await act(async () => {}); // flush passive effects (the listener) under fake timers
    act(() => { window.dispatchEvent(new Event('online')); });
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2), { timeout: 5_000 });
    expect(await screen.findByRole('heading', { name: 'Can’t reach RideShare' }, { timeout: 5_000 })).toBeInTheDocument();
    await act(async () => {}); // the backoff timer is armed by an effect
    expect(fetch).toHaveBeenCalledTimes(2);
    await advance(30_000); // backoff (5 s, then 10 s) fires without any event
    expect(await screen.findByText('Signed in as Ayesha', {}, { timeout: 5_000 })).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(3);
  });
  it('an abort caused by the page unloading is not a failure (keeps the earlier unload fix)', async () => {
    const saved = store();
    let fail!: (reason: unknown) => void;
    vi.stubGlobal('fetch', vi.fn().mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; })));
    mount();
    try {
      window.dispatchEvent(new Event('pagehide'));
      await act(async () => { fail(new DOMException('aborted', 'AbortError')); });
      expect(window.sessionStorage.getItem(SESSION_KEY)).toBe(saved);
      expect(screen.queryByRole('heading', { name: 'Can’t reach RideShare' })).toBeNull();
    } finally { resetPageUnloadingForTests(); }
  });
  it('Sign out from the retry state clears the session and stops retrying', async () => {
    store();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
    act(() => { window.dispatchEvent(new Event('online')); });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
