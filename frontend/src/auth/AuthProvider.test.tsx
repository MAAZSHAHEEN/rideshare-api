import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { AuthProvider, useAuth } from './AuthProvider';
import type { User } from '../api/types';

const user: User = { id: 1, name: 'Ayesha', email: 'a@example.com', role: 'passenger' };
const token = () => `h.${btoa(JSON.stringify({ sub: '1', role: 'driver', exp: Math.floor(Date.now() / 1000) + 3600 }))}.s`;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function Probe() {
  const auth = useAuth();
  return <><p>{auth.user ? `${auth.user.name}:${auth.user.role}` : 'Signed out'}</p><p>{auth.notice}</p>
    <button onClick={() => void auth.login({ email: user.email, password: 'password' }).catch(() => {})}>Login</button>
    <button onClick={auth.logout}>Logout</button>
    <button onClick={() => void auth.api.myBookings(0).catch(() => {})}>Fetch</button></>;
}
function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><AuthProvider><Probe /></AuthProvider></QueryClientProvider>);
  return queryClient;
}

describe('memory-only verified sessions', () => {
  it('uses /me identity rather than JWT role, then clears cache on logout', async () => {
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
    expect(storage).not.toHaveBeenCalled();
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
