import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { futureIso, json, renderPassenger, ride } from '../../test/passengerHarness';

const rides = () => [
  ride({ id: 1, driver_id: 42, origin: 'Lahore', destination: 'Islamabad', departure_time: futureIso(72) }),
  ride({ id: 2, driver_id: 42, origin: 'Islamabad', destination: 'Lahore', departure_time: futureIso(24) }),
  ride({ id: 3, driver_id: 42, origin: 'Lahore', destination: 'Rawalpindi', departure_time: futureIso(-3) }),
  ride({ id: 4, driver_id: 42, origin: 'Rawalpindi', destination: 'Lahore', departure_time: futureIso(-48), status: 'completed' }),
  ride({ id: 5, driver_id: 42, origin: 'Multan', destination: 'Lahore', departure_time: futureIso(5), status: 'cancelled' }),
];
const server = (url: URL) => {
  const status = url.searchParams.get('status');
  return json(rides().filter(r => !status || r.status === status).sort((a, b) => Date.parse(b.departure_time) - Date.parse(a.departure_time)));
};

describe('my rides', () => {
  it('lists every ride with a derived state label and a manage link', async () => {
    renderPassenger('/driver/rides', { 'GET /rides/me': server });
    await screen.findAllByRole('article');
    const card = screen.getByRole('article', { name: 'Lahore to Rawalpindi' });
    expect(within(card).getByText('Departed')).toBeInTheDocument();
    expect(within(screen.getByRole('article', { name: 'Multan to Lahore' })).getByText('Cancelled')).toBeInTheDocument();
    expect(within(screen.getByRole('article', { name: 'Islamabad to Lahore' })).getByRole('link', { name: /^Manage requests: Islamabad to Lahore/ })).toHaveAttribute('href', '/driver/rides/2');
  });

  it('splits active rides into upcoming (soonest first) and departed', async () => {
    const harness = renderPassenger('/driver/rides?filter=upcoming', { 'GET /rides/me': server });
    await waitFor(() => expect(screen.getAllByRole('article').map(a => a.getAttribute('aria-labelledby'))).toEqual(['driver-ride-2-title', 'driver-ride-1-title']));
    const last = harness.calls.at(-1)!.url;
    expect(last.searchParams.get('status')).toBe('active');
    expect(last.searchParams.get('limit')).toBe('100');
    await userEvent.click(screen.getByRole('button', { name: 'Departed' }));
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(1));
    expect(screen.getByRole('article', { name: 'Lahore to Rawalpindi' })).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('/driver/rides?filter=departed');
  });

  it('uses the server status filter for completed and cancelled', async () => {
    const harness = renderPassenger('/driver/rides?filter=completed', { 'GET /rides/me': server });
    expect(await screen.findByRole('article', { name: 'Rawalpindi to Lahore' })).toBeInTheDocument();
    expect(harness.calls.at(-1)!.url.searchParams.get('status')).toBe('completed');
    expect(harness.calls.at(-1)!.url.searchParams.get('limit')).toBe('20');
  });

  it('has empty and error states', async () => {
    renderPassenger('/driver/rides?filter=cancelled', { 'GET /rides/me': () => json([]) });
    expect(await screen.findByRole('heading', { name: 'No cancelled rides.' })).toBeInTheDocument();
  });
  it('shows a load error with retry', async () => {
    renderPassenger('/driver/rides', { 'GET /rides/me': () => json({ detail: 'x' }, 500) });
    expect(await screen.findByText('We couldn’t load your rides.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('driver navigation offers publish and my rides, and labels documents as not verified', async () => {
    renderPassenger('/driver/rides', { 'GET /rides/me': () => json([]) });
    const nav = await screen.findByRole('navigation', { name: 'Dashboard navigation' });
    expect(within(nav).getByRole('link', { name: 'Publish a ride' })).toHaveAttribute('href', '/driver/rides/new');
    expect(within(nav).getByRole('link', { name: 'My rides' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText('Driver documents not verified')).toBeInTheDocument();
    expect(screen.queryByText(/verified driver/i)).toBeNull();
  });

  it('signed-out deep link to a driver ride signs in and returns there', async () => {
    renderPassenger('/driver/rides/7', {}, { signedIn: false });
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/login'));
  });
});
