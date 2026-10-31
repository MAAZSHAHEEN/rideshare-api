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
/** Mirrors GET /rides/me with D1 (status, when, order) and G7 (X-Total-Count). */
const server = (url: URL) => {
  const q = url.searchParams;
  const now = Date.now();
  let rows = rides().filter(r => (!q.get('status') || r.status === q.get('status'))
    && (q.get('when') !== 'upcoming' || Date.parse(r.departure_time) > now)
    && (q.get('when') !== 'departed' || Date.parse(r.departure_time) <= now));
  rows = rows.sort((a, b) => (Date.parse(b.departure_time) - Date.parse(a.departure_time)) * (q.get('order') === 'asc' ? -1 : 1));
  const offset = Number(q.get('offset') ?? 0);
  return json(rows.slice(offset, offset + Number(q.get('limit') ?? 20)), 200, { 'X-Total-Count': String(rows.length) });
};
const counts = (url: URL) => json(url.searchParams.getAll('ride_id').map(Number).map(id => ({ ride_id: id, pending: id === 1 ? 2 : 0, accepted: 0, rejected: 0, cancelled: 0 })));

describe('my rides', () => {
  it('lists every ride with a derived state label and a manage link', async () => {
    renderPassenger('/driver/rides', { 'GET /rides/me': server, 'GET /rides/me/booking-counts': counts });
    await screen.findAllByRole('article');
    const card = screen.getByRole('article', { name: 'Lahore to Rawalpindi' });
    expect(within(card).getByText('Departed')).toBeInTheDocument();
    expect(within(screen.getByRole('article', { name: 'Multan to Lahore' })).getByText('Cancelled')).toBeInTheDocument();
    expect(within(screen.getByRole('article', { name: 'Islamabad to Lahore' })).getByRole('link', { name: /^Manage requests: Islamabad to Lahore/ })).toHaveAttribute('href', '/driver/rides/2');
  });

  it('Upcoming/Departed use the server filters (D1): when + order, no client split over 100 rows', async () => {
    const harness = renderPassenger('/driver/rides?filter=upcoming', { 'GET /rides/me': server, 'GET /rides/me/booking-counts': counts });
    await waitFor(() => expect(screen.getAllByRole('article').map(a => a.getAttribute('aria-labelledby'))).toEqual(['driver-ride-2-title', 'driver-ride-1-title']));
    const last = harness.calls.filter(c => c.url.pathname === '/rides/me').at(-1)!.url;
    expect(Object.fromEntries(last.searchParams)).toEqual({ limit: '20', offset: '0', status: 'active', when: 'upcoming', order: 'asc' });
    await userEvent.click(screen.getByRole('button', { name: 'Departed' }));
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(1));
    expect(screen.getByRole('article', { name: 'Lahore to Rawalpindi' })).toBeInTheDocument();
    const departed = harness.calls.filter(c => c.url.pathname === '/rides/me').at(-1)!.url;
    expect(Object.fromEntries(departed.searchParams)).toEqual({ limit: '20', offset: '0', status: 'active', when: 'departed' });
    expect(screen.getByTestId('location')).toHaveTextContent('/driver/rides?filter=departed');
  });

  it('shows pending counts from one booking-counts call per page (D2)', async () => {
    const harness = renderPassenger('/driver/rides', { 'GET /rides/me': server, 'GET /rides/me/booking-counts': counts });
    const card = await screen.findByRole('article', { name: 'Lahore to Islamabad' });
    expect(await within(card).findByText('2 pending')).toBeInTheDocument();
    expect(within(screen.getByRole('article', { name: 'Islamabad to Lahore' })).getByText('No pending')).toBeInTheDocument();
    const countCalls = harness.calls.filter(c => c.url.pathname === '/rides/me/booking-counts');
    expect(countCalls).toHaveLength(1);
    expect(countCalls[0].url.searchParams.getAll('ride_id').map(Number).sort()).toEqual([1, 2, 3, 4, 5]);
  });

  it('a failed counts call hides the counts but keeps the list', async () => {
    renderPassenger('/driver/rides', { 'GET /rides/me': server, 'GET /rides/me/booking-counts': () => json({ detail: 'x' }, 500) });
    expect(await screen.findByRole('article', { name: 'Lahore to Islamabad' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/pending$/)).toBeNull());
  });

  it('paginates with the server total (G7): page X of Y, Next disabled on the last page', async () => {
    const many = Array.from({ length: 21 }, (_, i) => ride({ id: 100 + i, driver_id: 42, departure_time: futureIso(10 + i) }));
    const page = (url: URL) => {
      const offset = Number(url.searchParams.get('offset'));
      return json(many.slice(offset, offset + 20), 200, { 'X-Total-Count': '21' });
    };
    renderPassenger('/driver/rides', { 'GET /rides/me': page, 'GET /rides/me/booking-counts': () => json([]) });
    expect(await screen.findByText('Page 1 of 2')).toBeInTheDocument();
    expect(screen.getByText(/Showing 1–20 of 21 rides\./)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Next →' }));
    expect(await screen.findByText('Page 2 of 2')).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(1));
    expect(screen.getByRole('button', { name: 'Next →' })).toBeDisabled();
  });

  it('a full page of exactly the total has no Next (heuristic would have offered an empty page)', async () => {
    const twenty = Array.from({ length: 20 }, (_, i) => ride({ id: 200 + i, driver_id: 42, departure_time: futureIso(10 + i) }));
    renderPassenger('/driver/rides', { 'GET /rides/me': () => json(twenty, 200, { 'X-Total-Count': '20' }), 'GET /rides/me/booking-counts': () => json([]) });
    expect(await screen.findByText('Page 1 of 1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next →' })).toBeDisabled();
  });

  it('uses the server status filter for completed and cancelled', async () => {
    const harness = renderPassenger('/driver/rides?filter=completed', { 'GET /rides/me': server, 'GET /rides/me/booking-counts': counts });
    expect(await screen.findByRole('article', { name: 'Rawalpindi to Lahore' })).toBeInTheDocument();
    expect(harness.calls.filter(c => c.url.pathname === '/rides/me').at(-1)!.url.searchParams.get('status')).toBe('completed');
    expect(harness.calls.filter(c => c.url.pathname === '/rides/me').at(-1)!.url.searchParams.get('limit')).toBe('20');
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
