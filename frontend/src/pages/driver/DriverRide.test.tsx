import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { DriverBooking, Ride } from '../../api/types';
import { futureIso, json, renderPassenger, ride } from '../../test/passengerHarness';

const mine = (overrides: Partial<Ride> = {}) => ride({ id: 7, driver_id: 42, departure_time: futureIso(30), available_seats: 2, ...overrides });
const request = (id: number, name: string, status: DriverBooking['status'] = 'pending'): DriverBooking =>
  ({ id, ride_id: 7, passenger_id: 100 + id, status, passenger: { id: 100 + id, name } });

/** Stateful mock of the ride + its requests with the server's respond rules. */
function server(initial: Ride, requests: DriverBooking[]) {
  const state = { ride: { ...initial }, requests: requests.map(r => ({ ...r })) };
  return {
    state,
    handlers: {
      'GET /rides/7': () => json(state.ride),
      'GET /rides/7/bookings': () => json([...state.requests].reverse()),
      ...Object.fromEntries(state.requests.map(r => [`PATCH /bookings/${r.id}/respond`, (url: URL) => {
        const booking = state.requests.find(b => b.id === r.id)!;
        if (booking.status !== 'pending') return json({ detail: 'Booking already responded to' }, 409);
        if (url.searchParams.get('accept') === 'true') {
          if (state.ride.available_seats < 1) return json({ detail: 'No seats available' }, 409);
          booking.status = 'accepted'; state.ride.available_seats -= 1;
        } else booking.status = 'rejected';
        return json({ id: booking.id, ride_id: 7, passenger_id: booking.passenger_id, status: booking.status });
      }])),
    } as Record<string, (url: URL, init: RequestInit) => Response>,
  };
}

const group = (name: RegExp) => screen.getByRole('region', { name });

describe('driver ride: incoming requests', () => {
  it('shows loading, then requests grouped by passenger booking status', async () => {
    const api = server(mine(), [request(1, 'Pending Passenger'), request(2, 'Accepted Passenger', 'accepted'), request(3, 'Rejected Passenger', 'rejected'), request(4, 'Gone Passenger', 'cancelled')]);
    renderPassenger('/driver/rides/7', api.handlers);
    expect(await screen.findByText('Loading ride…')).toBeInTheDocument();
    expect(await screen.findByText('1 pending · 1 accepted')).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { level: 3 }).map(h => h.textContent)).toEqual(['Pending (1)', 'Accepted (1)', 'Rejected (1)', 'Cancelled (1)']);
    expect(within(group(/^Pending/)).getByRole('button', { name: 'Accept request from Pending Passenger' })).toBeEnabled();
    expect(within(group(/^Accepted/)).queryByRole('button')).toBeNull();
    expect(within(group(/^Cancelled/)).queryByRole('button')).toBeNull();
    expect(screen.getByText('2 seats left')).toBeInTheDocument();
  });

  it('has an empty state and an error state with retry', async () => {
    renderPassenger('/driver/rides/7', { 'GET /rides/7': () => json(mine()), 'GET /rides/7/bookings': () => json([]) });
    expect(await screen.findByRole('heading', { name: 'No requests yet.' })).toBeInTheDocument();
  });
  it('shows a requests load error without leaking server text', async () => {
    renderPassenger('/driver/rides/7', { 'GET /rides/7': () => json(mine()), 'GET /rides/7/bookings': () => json({ detail: 'pool timeout' }, 503) });
    expect(await screen.findByRole('heading', { name: 'We couldn’t load the requests.' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('temporarily unavailable');
    expect(screen.queryByText(/pool timeout/)).toBeNull();
  });

  it('accepts a request: the seat count drops and the request moves to accepted', async () => {
    const api = server(mine(), [request(1, 'Asha Test'), request(2, 'Bilal Test')]);
    const harness = renderPassenger('/driver/rides/7', api.handlers);
    await userEvent.click(await screen.findByRole('button', { name: 'Accept request from Asha Test' }));
    expect(await screen.findByText('Accepted Asha Test’s request. One seat is now taken.')).toBeInTheDocument();
    expect(await screen.findByText('1 seat left')).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole('heading', { level: 3 }).map(h => h.textContent)).toEqual(['Pending (1)', 'Accepted (1)']));
    expect(harness.calls.find(c => c.method === 'PATCH')!.url.search).toBe('?accept=true');
  });

  it('rejects only after confirming', async () => {
    const api = server(mine(), [request(1, 'Asha Test')]);
    const harness = renderPassenger('/driver/rides/7', api.handlers);
    const trigger = await screen.findByRole('button', { name: 'Reject request from Asha Test' });
    await userEvent.click(trigger);
    const dialog = screen.getByRole('alertdialog', { name: 'Reject this request?' });
    expect(within(dialog).getByRole('button', { name: 'Keep request' })).toHaveFocus();
    await userEvent.keyboard('{Escape}');
    expect(harness.calls.some(c => c.method === 'PATCH')).toBe(false);
    await userEvent.click(trigger);
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Reject request' }));
    expect(await screen.findByText('Rejected Asha Test’s request.')).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole('heading', { level: 3 }).map(h => h.textContent)).toEqual(['Rejected (1)']));
    expect(harness.calls.find(c => c.method === 'PATCH')!.url.search).toBe('?accept=false');
  });

  const conflicts: [string, string, RegExp][] = [
    ['already responded', 'Booking already responded to', /^This request was already answered\./],
    ['withdrawn by the passenger (D7) — neutral R-1 copy', 'Booking was cancelled by the passenger', /^This booking is no longer available\.$/],
    ['insufficient seats', 'No seats available', /No seats are left/],
    ['departed', 'Ride has already departed', /already departed/],
    ['concurrent (unknown detail)', 'deadlock detected', /changed at the same time/],
    ['inactive', 'Ride is not active', /no longer active/],
  ];
  for (const [name, detail, message] of conflicts) {
    it(`409 ${name} on accept shows a specific message and re-fetches the ride and requests`, async () => {
      const api = server(mine(), [request(1, 'Asha Test')]);
      api.handlers['PATCH /bookings/1/respond'] = () => json({ detail }, 409);
      const harness = renderPassenger('/driver/rides/7', api.handlers);
      await userEvent.click(await screen.findByRole('button', { name: 'Accept request from Asha Test' }));
      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent(message);
      await waitFor(() => expect(alert).toHaveFocus());
      expect(screen.queryByText(/deadlock/)).toBeNull();
      const reads = (path: string) => harness.calls.filter(c => c.method === 'GET' && c.url.pathname === path).length;
      await waitFor(() => { expect(reads('/rides/7')).toBe(2); expect(reads('/rides/7/bookings')).toBe(2); });
    });
  }

  it('a real race: the passenger withdrew first, so the refreshed list shows cancelled', async () => {
    const api = server(mine(), [request(1, 'Asha Test')]);
    renderPassenger('/driver/rides/7', api.handlers);
    const accept = await screen.findByRole('button', { name: 'Accept request from Asha Test' });
    api.state.requests[0].status = 'cancelled';
    await userEvent.click(accept);
    expect(await screen.findByRole('alert')).toHaveTextContent(/already answered/);
    expect(await screen.findByRole('heading', { name: 'Cancelled (1)' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Accept request/ })).toBeNull();
  });

  it('never offers accept after departure; reject stays available', async () => {
    const api = server(mine({ departure_time: futureIso(-1) }), [request(1, 'Asha Test')]);
    const harness = renderPassenger('/driver/rides/7', api.handlers);
    expect(await screen.findByText('Accepting is paused.')).toBeInTheDocument();
    expect(screen.getByText(/has departed, so requests can no longer be accepted/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Accept request/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Reject request from Asha Test' })).toBeEnabled();
    expect(screen.getByText('Departed')).toBeInTheDocument();
    expect(harness.calls.some(c => c.method === 'PATCH')).toBe(false);
  });

  it('re-checks departure at click time (page left open past departure)', async () => {
    const api = server(mine({ departure_time: new Date(Date.now() + 1500).toISOString() }), [request(1, 'Asha Test')]);
    const harness = renderPassenger('/driver/rides/7', api.handlers);
    const accept = await screen.findByRole('button', { name: 'Accept request from Asha Test' });
    await new Promise(resolve => setTimeout(resolve, 1600));
    await userEvent.click(accept);
    expect(await screen.findByRole('alert')).toHaveTextContent(/departed/);
    expect(harness.calls.some(c => c.method === 'PATCH')).toBe(false);
  });

  it('a full ride offers no accept and explains why', async () => {
    const api = server(mine({ available_seats: 0 }), [request(1, 'Asha Test')]);
    renderPassenger('/driver/rides/7', api.handlers);
    expect(await screen.findByText(/No seats left/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Accept request/ })).toBeNull();
  });

  it('cancels the ride after a confirm dialog that counts affected passengers', async () => {
    const api = server(mine(), [request(1, 'Asha Test'), request(2, 'Bilal Test', 'accepted')]);
    api.handlers['PATCH /rides/7/cancel'] = () => { api.state.ride.status = 'cancelled'; api.state.requests.forEach(r => { if (r.status === 'pending' || r.status === 'accepted') r.status = 'cancelled'; }); return json(api.state.ride); };
    renderPassenger('/driver/rides/7', api.handlers);
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel ride' }));
    const dialog = screen.getByRole('alertdialog', { name: 'Cancel this ride?' });
    expect(dialog).toHaveTextContent('1 accepted passenger and 1 pending request will be cancelled.');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel ride' }));
    expect(await screen.findByText(/Ride cancelled\./)).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Cancelled (2)' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel ride' })).toBeNull();
  });

  it('a 409 on cancel keeps the dialog open with a specific message', async () => {
    const api = server(mine(), []);
    api.handlers['PATCH /rides/7/cancel'] = () => json({ detail: 'Ride is not active' }, 409);
    renderPassenger('/driver/rides/7', api.handlers);
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel ride' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel ride' }));
    expect(await within(screen.getByRole('alertdialog')).findByRole('alert')).toHaveTextContent('already cancelled or completed');
  });

  it('offers "Mark ride completed" only after departure', async () => {
    const api = server(mine({ departure_time: futureIso(-2) }), [request(1, 'Asha Test')]);
    api.handlers['PATCH /rides/7/complete'] = () => { api.state.ride.status = 'completed'; api.state.requests[0].status = 'cancelled'; return json(api.state.ride); };
    renderPassenger('/driver/rides/7', api.handlers);
    await userEvent.click(await screen.findByRole('button', { name: 'Mark ride completed' }));
    const dialog = screen.getByRole('alertdialog', { name: 'Mark this ride completed?' });
    expect(dialog).toHaveTextContent('1 pending request will be cancelled.');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Mark completed' }));
    expect(await screen.findByText(/Ride marked as completed/)).toBeInTheDocument();
    expect(await screen.findByText('Completed')).toBeInTheDocument();
  });
  it('hides completion for an upcoming ride', async () => {
    renderPassenger('/driver/rides/7', server(mine(), []).handlers);
    expect(await screen.findByRole('button', { name: 'Cancel ride' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Mark ride completed' })).toBeNull();
  });

  it('another driver’s ride shows no requests and makes no bookings call', async () => {
    const harness = renderPassenger('/driver/rides/7', { 'GET /rides/7': () => json(mine({ driver_id: 9 })) });
    expect(await screen.findByRole('heading', { name: 'Not your ride' })).toBeInTheDocument();
    expect(harness.calls.some(c => c.url.pathname === '/rides/7/bookings')).toBe(false);
  });

  it('a missing or malformed ride shows not found', async () => {
    renderPassenger('/driver/rides/7', { 'GET /rides/7': () => json({ detail: 'Ride not found' }, 404) });
    expect(await screen.findByRole('heading', { name: 'Ride not found' })).toBeInTheDocument();
  });
  it('a malformed id makes no request', async () => {
    const harness = renderPassenger('/driver/rides/abc', {});
    expect(await screen.findByRole('heading', { name: 'Ride not found' })).toBeInTheDocument();
    expect(harness.calls.filter(c => c.url.pathname.startsWith('/rides'))).toHaveLength(0);
  });

  it('401 while accepting ends the session and goes to sign-in', async () => {
    const api = server(mine(), [request(1, 'Asha Test')]);
    api.handlers['PATCH /bookings/1/respond'] = () => json({ detail: 'Invalid token' }, 401);
    renderPassenger('/driver/rides/7', api.handlers);
    await userEvent.click(await screen.findByRole('button', { name: 'Accept request from Asha Test' }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/login'));
  });

  it.each([
    [403, { detail: 'This is not your ride' }, {}, /another driver/],
    [404, { detail: 'Booking not found' }, {}, /couldn’t find that request/],
    [422, { detail: [{ loc: ['query', 'accept'], msg: 'bad' }] }, {}, /weren’t accepted/],
    [429, { detail: 'slow down' }, { 'Retry-After': '9' }, /wait 9 seconds/],
    [502, { detail: 'upstream' }, {}, /temporarily unavailable/],
  ])('accept %s shows a safe message', async (status, body, headers, message) => {
    const api = server(mine(), [request(1, 'Asha Test')]);
    api.handlers['PATCH /bookings/1/respond'] = () => json(body, status, headers);
    renderPassenger('/driver/rides/7', api.handlers);
    await userEvent.click(await screen.findByRole('button', { name: 'Accept request from Asha Test' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
  });
});
