// R-6: the "Can't reach RideShare" retry screen must not show signed-out navigation
// (the visitor is still signed in) or role navigation (identity not verified yet).
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { AuthProvider } from '../auth/AuthProvider';
import { SESSION_KEY } from '../auth/sessionStore';
import type { User } from '../api/types';

const user: User = { id: 1, name: 'Ayesha Test', email: 'ayesha@example.test', role: 'passenger', allowed_modes: ['passenger', 'driver'] };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'X-Total-Count': '0' } });
const token = () => {
  const iat = Math.floor(Date.now() / 1000);
  return `h.${btoa(JSON.stringify({ sub: '1', role: 'passenger', iat, exp: iat + 3600 }))}.s`;
};

function setup(path = '/account', down: boolean | 'pending' = true) {
  window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token: token(), receivedAt: Date.now(), mode: 'passenger' }));
  const state = { down, meCalls: 0 };
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input instanceof Request ? input.url : input), 'http://api.test');
    if (url.pathname.endsWith('/me')) {
      state.meCalls += 1;
      if (state.down === 'pending') return new Promise<Response>(() => {});
      if (state.down) return Promise.reject(new TypeError('Failed to fetch'));
      return Promise.resolve(json(user));
    }
    return Promise.resolve(json([]));
  }));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter></QueryClientProvider>);
  return state;
}
const header = () => document.querySelector('header.site-header') as HTMLElement;
const footer = () => document.querySelector('footer.site-footer') as HTMLElement;
function expectNeutralChrome() {
  // Brand only: no sign-in / register links, no role or travel-mode navigation.
  expect(within(header()).getByRole('link', { name: 'RideShare home' })).toBeInTheDocument();
  expect(within(header()).getAllByRole('link')).toHaveLength(1);
  expect(within(header()).queryByRole('navigation')).toBeNull();
  expect(within(header()).queryByRole('button')).toBeNull();
  expect(screen.queryByRole('link', { name: /log in|sign in/i })).toBeNull();
  expect(screen.queryByRole('link', { name: /get started|register|join rideshare/i })).toBeNull();
  expect(screen.queryByRole('link', { name: /my dashboard/i })).toBeNull();
  expect(screen.queryByRole('navigation', { name: 'Travel mode' })).toBeNull();
  expect(document.querySelector('a[href="/login"], a[href="/register"]')).toBeNull();
  expect(within(footer()).queryAllByRole('link').map(a => a.getAttribute('href'))).toEqual(['/']);
}

describe('R-6: neutral header while the session is restoring or the API is unreachable', () => {
  it('restoring: brand only, with a polite live status', async () => {
    setup('/account', 'pending');
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('Restoring your session…');
    expect(status).toHaveAttribute('aria-live', 'polite');
    expectNeutralChrome();
  });

  it('unreachable: brand only, Retry + Sign out kept, heading focused, alert announced; Retry recovers to the role nav', async () => {
    const state = setup();
    const heading = await screen.findByRole('heading', { level: 1, name: 'Can’t reach RideShare' });
    expectNeutralChrome();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('You’re still signed in on this tab.');
    await waitFor(() => expect(heading).toHaveFocus());

    // A failed Retry keeps the neutral state and keeps focus on the Retry button.
    const retry = screen.getByRole('button', { name: 'Retry' });
    retry.focus();
    fireEvent.click(retry);
    await waitFor(() => expect(state.meCalls).toBeGreaterThanOrEqual(2));
    await screen.findByRole('heading', { level: 1, name: 'Can’t reach RideShare' });
    expectNeutralChrome();

    state.down = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    const nav = await within(header()).findByRole('navigation', { name: 'Main navigation' });
    expect(within(nav).getByRole('link', { name: 'My dashboard' })).toBeInTheDocument();
    expect(within(nav).getByRole('button', { name: 'Log out' })).toBeInTheDocument();
    expect(within(nav).queryByRole('link', { name: /log in/i })).toBeNull();
    expect(screen.getByRole('navigation', { name: 'Travel mode' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Can’t reach RideShare' })).toBeNull();
    // Focus moves to the new content, not <body>.
    await waitFor(() => expect(document.getElementById('main-content')).toHaveFocus());
  });

  it('Sign out from the unreachable state shows the signed-out navigation (that is now true)', async () => {
    setup();
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    const nav = await within(header()).findByRole('navigation', { name: 'Main navigation' });
    expect(within(nav).getByRole('link', { name: 'Log in' })).toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it('a visitor with no saved session gets the normal signed-out header (no neutral state)', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json([]))));
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={['/']}><AuthProvider><App /></AuthProvider></MemoryRouter></QueryClientProvider>);
    const nav = within(header()).getByRole('navigation', { name: 'Main navigation' });
    expect(within(nav).getByRole('link', { name: 'Log in' })).toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: /get started/i })).toBeInTheDocument();
  });
});
