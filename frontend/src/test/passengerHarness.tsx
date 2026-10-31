import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { vi } from 'vitest';
import { App } from '../App';
import { AuthProvider } from '../auth/AuthProvider';
import { SESSION_KEY } from '../auth/sessionStore';
import type { PassengerBooking, Ride, User } from '../api/types';

// Obviously fake fixtures: no real people, CNICs or phone numbers.
export const passenger: User = { id: 42, name: 'Test Passenger', email: 'test.passenger@example.com', role: 'passenger', allowed_modes: ['passenger', 'driver'] };
const HOUR = 3_600_000;
export const futureIso = (hours = 48) => new Date(Date.now() + hours * HOUR).toISOString();
export const ride = (overrides: Partial<Ride> = {}): Ride => ({
  id: 7, driver_id: 9, origin: 'Lahore', destination: 'Islamabad', departure_time: '2030-06-12T04:30:00Z',
  available_seats: 3, fare_per_seat: 2500, status: 'active', ...overrides,
});
export const booking = (overrides: Partial<PassengerBooking> = {}): PassengerBooking => ({
  id: 70, ride_id: 7, passenger_id: 42, status: 'pending', ride: ride(), ...overrides,
});
export const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });

type Handler = (url: URL, init: RequestInit) => Response | Promise<Response>;
export interface Harness { calls: { method: string; url: URL }[] }

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

/** Renders the real app at `path` with a restored (sessionStorage) passenger session and routed fetch mocks. */
export function renderPassenger(path: string, handlers: Record<string, Handler>, options: { signedIn?: boolean } = {}): Harness {
  const harness: Harness = { calls: [] };
  if (options.signedIn !== false) {
    const tokenValue = `h.${btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }))}.s`;
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ token: tokenValue, expiresAt: 0, mode: 'passenger' }));
  }
  vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = new URL(input);
    const method = init.method ?? 'GET';
    harness.calls.push({ method, url });
    const handler = handlers[`${method} ${url.pathname}`] ?? (url.pathname === '/me' ? () => json(passenger) : undefined);
    if (!handler) throw new Error(`Unexpected API request: ${method} ${url.pathname}`);
    return handler(url, init);
  }));
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><AuthProvider><MemoryRouter initialEntries={[path]}><App /><LocationProbe /></MemoryRouter></AuthProvider></QueryClientProvider>);
  return harness;
}
