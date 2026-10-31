import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DriverBooking, Ride } from '../api/types';
import { booking, futureIso, json, renderPassenger, ride } from '../test/passengerHarness';
import { legacyPassengerPath } from './passenger/PassengerArea';
import { liveBookingFor, searchBackPath } from './passenger/RideDetails';
import { resultSummary } from './passenger/SearchRides';
import { canCancel, pageSummary } from './passenger/MyBookings';

// Frontend adoption of PR #17 (API gaps) + passenger path routes + tolerance for #18/#20.
const upcoming = (overrides: Partial<Ride> = {}) => ride({ departure_time: futureIso(), ...overrides });
const total = (n: number) => ({ 'X-Total-Count': String(n) });

describe('passenger path routes and legacy redirects', () => {
  it.each([
    ['?ride=7', '/passenger/rides/7'],
    ['?ride=abc', '/passenger/rides/0'],
    ['?ride=07', '/passenger/rides/0'],
    ['?view=search', '/passenger/search'],
    ['?view=search&from=Lahore&to=Islamabad&date=2030-06-12&page=2&evil=1', '/passenger/search?from=Lahore&to=Islamabad&date=2030-06-12&page=2'],
    ['?view=bookings&status=pending&page=1&x=y', '/passenger/bookings?status=pending&page=1'],
    ['?view=unknown', '/passenger'],
    ['?view=search&from=%2F%2Fevil.example.com', '/passenger/search?from=%2F%2Fevil.example.com'],
  ])('maps the old link /passenger%s to %s', (search, expected) => {
    expect(legacyPassengerPath(new URLSearchParams(search))).toBe(expected);
  });
  it('leaves the plain overview alone', () => expect(legacyPassengerPath(new URLSearchParams(''))).toBeNull());

  it.each([
    ['/passenger?view=search&from=Lahore', '/passenger/search?from=Lahore'],
    ['/passenger?ride=7', '/passenger/rides/7'],
    ['/passenger?view=bookings&status=accepted', '/passenger/bookings?status=accepted'],
  ])('an old link %s redirects (replace) to %s in the app', async (from, to) => {
    renderPassenger(from, {
      'GET /rides/': () => json([], 200, total(0)), 'GET /rides/7': () => json(upcoming()),
      'GET /bookings/me': () => json([], 200, total(0)),
    });
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(to));
  });

  it('sidebar and cards link to the path routes', async () => {
    renderPassenger('/passenger/search', { 'GET /rides/': () => json([upcoming()], 200, total(1)) });
    const nav = await screen.findByRole('navigation', { name: 'Dashboard navigation' });
    expect(within(nav).getByRole('link', { name: 'Find a ride' })).toHaveAttribute('href', '/passenger/search');
    expect(within(nav).getByRole('link', { name: 'My bookings' })).toHaveAttribute('href', '/passenger/bookings');
    const card = await screen.findByRole('article', { name: 'Lahore to Islamabad' });
    expect(within(card).getByRole('link', { name: /^View details/ })).toHaveAttribute('href', '/passenger/rides/7');
  });

  it('a malformed path id shows not found without an API call', async () => {
    const harness = renderPassenger('/passenger/rides/0', {});
    expect(await screen.findByRole('heading', { name: 'Ride not found' })).toBeInTheDocument();
    expect(harness.calls.some(c => c.url.pathname.startsWith('/rides') || c.url.pathname.startsWith('/bookings'))).toBe(false);
  });
  it('unknown nested passenger paths are the app’s not-found page', async () => {
    renderPassenger('/passenger/rides/7/x', {});
    expect(await screen.findByRole('heading', { name: 'This road ends here.' })).toBeInTheDocument();
  });
  it('signed-out deep link to a passenger ride goes to sign-in with the return path', async () => {
    renderPassenger('/passenger/rides/7', {}, { signedIn: false });
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/login'));
  });
  it('back-to-search only accepts a query string from navigation state', () => {
    expect(searchBackPath('?from=Lahore&page=2')).toBe('/passenger/search?from=Lahore&page=2');
    expect(searchBackPath('')).toBe('/passenger/search');
    for (const bad of ['//evil.example.com', 'https://evil.example.com', '/account', '?x#frag', 5, undefined, `?${'a'.repeat(700)}`]) {
      expect(searchBackPath(bad)).toBe('/passenger/search');
    }
  });
});

describe('search totals (G7) and case-insensitive search (G3)', () => {
  it('shows the server total and pages with it', async () => {
    const rides = Array.from({ length: 25 }, (_, i) => upcoming({ id: 100 + i, departure_time: futureIso(10 + i) }));
    const harness = renderPassenger('/passenger/search?from=LAHORE%20', {
      'GET /rides/': url => {
        const offset = Number(url.searchParams.get('offset'));
        return json(rides.slice(offset, offset + 20), 200, total(25));
      },
    });
    expect(await screen.findByText('25 rides found. Showing 1–20.')).toBeInTheDocument();
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument();
    // The URL value is trimmed but its case is sent as typed: the server folds case (G3).
    expect(harness.calls.find(c => c.url.pathname === '/rides/')!.url.searchParams.get('origin')).toBe('LAHORE');
    await userEvent.click(screen.getByRole('button', { name: 'Next →' }));
    expect(await screen.findByText('25 rides found. Showing 21–25.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next →' })).toBeDisabled();
    expect(screen.getByTestId('location').textContent).toMatch(/^\/passenger\/search\?from=LAHORE(\+|%20)?&page=1$/);
  });
  it('no longer tells passengers that names must match exactly', async () => {
    renderPassenger('/passenger/search', { 'GET /rides/': () => json([], 200, total(0)) });
    expect(await screen.findByText(/Capital letters don’t matter/)).toBeInTheDocument();
    expect(screen.queryByText(/must match the ride exactly/)).toBeNull();
  });
  it('summary copy for each case, and the old heuristic without a total', () => {
    expect(resultSummary(false, false, 1, 0, 1)).toBe('1 ride found.');
    expect(resultSummary(false, false, 20, 0, 20)).toBe('20 rides found.');
    expect(resultSummary(false, false, 0, 0, 0)).toBe('No rides found.');
    expect(resultSummary(false, false, 0, 3, 41)).toBe('No more rides on this page. 41 rides found in total.');
    expect(resultSummary(false, false, 1, 2, 41)).toBe('41 rides found. Showing 41–41.');
    expect(resultSummary(false, false, 20, 0, null)).toBe('20 rides found.');
    expect(resultSummary(false, false, 3, 1, null)).toBe('3 rides found on page 2.');
  });
  it('without X-Total-Count (older backend) Next falls back to the page-size heuristic', async () => {
    const twenty = Array.from({ length: 20 }, (_, i) => upcoming({ id: 300 + i }));
    renderPassenger('/passenger/search', { 'GET /rides/': () => json(twenty) });
    expect(await screen.findByText('20 rides found.')).toBeInTheDocument();
    expect(screen.getByText('Page 1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next →' })).toBeEnabled();
  });
});

describe('my booking for this ride (G2)', () => {
  it('looks up the caller’s bookings for this ride only (ride_id, limit 5) and shows the live one', async () => {
    const harness = renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()),
      'GET /bookings/me': () => json([booking({ id: 90, status: 'pending' }), booking({ id: 80, status: 'rejected' })], 200, total(2)),
    });
    expect(await screen.findByText(/already requested a seat on this ride/)).toBeInTheDocument();
    const lookup = harness.calls.find(c => c.url.pathname === '/bookings/me')!.url;
    expect(Object.fromEntries(lookup.searchParams)).toEqual({ limit: '5', offset: '0', ride_id: '7' });
    expect(screen.queryByRole('button', { name: /Request a seat/ })).toBeNull();
    expect(screen.queryByText(/newest 100/)).toBeNull();
  });
  it('only past (rejected/cancelled/expired) bookings still allow a new request', async () => {
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()),
      'GET /bookings/me': () => json([booking({ status: 'expired' }), booking({ id: 71, status: 'cancelled' })], 200, total(2)),
    });
    expect(await screen.findByRole('button', { name: /Request a seat/ })).toBeEnabled();
  });
  it('liveBookingFor ignores other rides and non-live statuses', () => {
    expect(liveBookingFor(undefined, 7)).toBeUndefined();
    expect(liveBookingFor([booking({ ride_id: 8, status: 'pending' })], 7)).toBeUndefined();
    expect(liveBookingFor([booking({ status: 'expired' }), booking({ id: 2, status: 'accepted' })], 7)?.id).toBe(2);
  });
});

describe('my bookings totals, cut-off and expired', () => {
  const rows = () => [
    booking({ id: 1, status: 'pending', ride: upcoming({ id: 1, origin: 'Lahore', destination: 'Islamabad' }) }),
    booking({ id: 2, status: 'expired', ride: upcoming({ id: 2, origin: 'Multan', destination: 'Lahore' }) }),
  ];
  const server = (url: URL) => {
    const status = url.searchParams.get('status');
    if (status === 'expired' && !expiredKnown) return json({ detail: [{ loc: ['query', 'status'], msg: 'bad' }] }, 422);
    const all = rows().filter(b => !status || b.status === status);
    return json(all.slice(0, Number(url.searchParams.get('limit'))), 200, total(all.length));
  };
  let expiredKnown = true;
  afterEach(() => { expiredKnown = true; });

  it('renders an Expired group and tab counts from X-Total-Count', async () => {
    renderPassenger('/passenger/bookings', { 'GET /bookings/me': server });
    expect(await screen.findByRole('heading', { name: 'Expired (1)' })).toBeInTheDocument();
    const card = screen.getByRole('article', { name: 'Multan to Lahore' });
    expect(within(card).getByText('expired')).toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /Cancel/ })).toBeNull();
    expect(await screen.findByRole('button', { name: 'All (2)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pending (1)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Expired (1)' })).toBeInTheDocument();
    expect(screen.getByText(/2 bookings\./)).toBeInTheDocument();
  });
  it('hides the Expired filter tab when the server rejects the status (before #20), without breaking other counts', async () => {
    expiredKnown = false;
    renderPassenger('/passenger/bookings', { 'GET /bookings/me': server });
    expect(await screen.findByRole('button', { name: 'Pending (1)' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Expired/ })).toBeNull();
  });
  it('without X-Total-Count (older backend) the core tabs stay, uncounted, and Expired stays hidden', async () => {
    renderPassenger('/passenger/bookings', { 'GET /bookings/me': () => json(rows()) });
    expect(await screen.findByRole('heading', { name: 'Expired (1)' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole('button', { pressed: false }).map(b => b.textContent)).toEqual(['Pending', 'Accepted', 'Rejected', 'Cancelled']));
    expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true');
  });
  it('an accepted seat can’t be cancelled at or after departure; a pending request still can', () => {
    const now = Date.parse('2030-06-12T04:30:00Z');
    const at = ride({ departure_time: '2030-06-12T04:30:00Z' });
    expect(canCancel(booking({ status: 'accepted', ride: at }), now)).toBe(false);
    expect(canCancel(booking({ status: 'accepted', ride: at }), now - 1)).toBe(true);
    expect(canCancel(booking({ status: 'pending', ride: at }), now + 1)).toBe(true);
    expect(canCancel(booking({ status: 'expired', ride: at }), now - 1)).toBe(false);
  });
  it('page summary copy', () => {
    expect(pageSummary(0, 3, 3)).toBe('3 bookings.');
    expect(pageSummary(20, 13, 33)).toBe('Showing 21–33 of 33 bookings.');
    expect(pageSummary(40, 0, 33)).toBe('33 bookings in total.');
    expect(pageSummary(0, 1, 1)).toBe('1 booking.');
  });
  it('maps the after-departure cancel 409 to fixed copy in the dialog', async () => {
    renderPassenger('/passenger/bookings', {
      'GET /bookings/me': server,
      'PATCH /bookings/1/cancel': () => json({ detail: 'Ride has already departed' }, 409),
    });
    const card = await screen.findByRole('article', { name: 'Lahore to Islamabad' });
    await userEvent.click(within(card).getByRole('button', { name: /^Cancel booking/ }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel booking' }));
    expect(await within(screen.getByRole('alertdialog')).findByRole('alert')).toHaveTextContent('This ride has already departed, so an accepted seat can no longer be cancelled.');
  });
});

describe('driver ride: counts, totals, expired, D7', () => {
  const mine = () => ride({ id: 7, driver_id: 42, departure_time: futureIso(30), available_seats: 2 });
  const req = (id: number, name: string, status: DriverBooking['status'] = 'pending'): DriverBooking => ({ id, ride_id: 7, passenger_id: 100 + id, status, passenger: { id: 100 + id, name } });

  it('summary uses booking-counts (exact beyond 100 requests) and the footnote uses the total', async () => {
    const list = Array.from({ length: 100 }, (_, i) => req(1000 + i, `Passenger ${i}`));
    renderPassenger('/driver/rides/7', {
      'GET /rides/7': () => json(mine()),
      'GET /rides/7/bookings': () => json(list, 200, total(130)),
      'GET /rides/me/booking-counts': () => json([{ ride_id: 7, pending: 120, accepted: 1, rejected: 5, cancelled: 4 }]),
    });
    expect(await screen.findByText('120 pending · 1 accepted')).toBeInTheDocument();
    expect(screen.getByText('Showing the newest 100 of 130 requests.')).toBeInTheDocument();
  });
  it('renders expired requests read-only in their own group', async () => {
    renderPassenger('/driver/rides/7', {
      'GET /rides/7': () => json(mine()),
      'GET /rides/7/bookings': () => json([req(1, 'Late Passenger', 'expired'), req(2, 'Asha Test')], 200, total(2)),
      'GET /rides/me/booking-counts': () => json([{ ride_id: 7, pending: 1, accepted: 0, rejected: 0, cancelled: 0 }]),
    });
    const expired = await screen.findByRole('region', { name: /^Expired/ });
    expect(within(expired).getByText('expired')).toBeInTheDocument();
    expect(within(expired).queryByRole('button')).toBeNull();
    expect(screen.getAllByRole('heading', { level: 3 }).map(h => h.textContent)).toEqual(['Pending (1)', 'Expired (1)']);
  });
  it.each([
    ['Booking was cancelled by the passenger', 'The passenger withdrew this request, so it can’t be answered. The list has been refreshed.'],
    ['Booking request has expired', 'This request expired before it was answered, so it can’t be accepted. The list has been refreshed.'],
  ])('accept 409 "%s" shows fixed copy and refetches', async (detail, copy) => {
    let reads = 0;
    const harness = renderPassenger('/driver/rides/7', {
      'GET /rides/7': () => json(mine()),
      'GET /rides/7/bookings': () => { reads += 1; return json([req(1, 'Asha Test')], 200, total(1)); },
      'GET /rides/me/booking-counts': () => json([{ ride_id: 7, pending: 1, accepted: 0, rejected: 0, cancelled: 0 }]),
      'PATCH /bookings/1/respond': () => json({ detail }, 409),
    });
    await userEvent.click(await screen.findByRole('button', { name: 'Accept request from Asha Test' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(copy);
    await waitFor(() => expect(reads).toBeGreaterThan(1));
    expect(harness.calls.filter(c => c.url.pathname === '/rides/me/booking-counts').length).toBeGreaterThan(1);
  });
  it('early completion 409 shows fixed copy in the dialog', async () => {
    const departed = ride({ id: 7, driver_id: 42, departure_time: futureIso(-1), available_seats: 2 });
    renderPassenger('/driver/rides/7', {
      'GET /rides/7': () => json(departed),
      'GET /rides/7/bookings': () => json([], 200, total(0)),
      'GET /rides/me/booking-counts': () => json([]),
      'PATCH /rides/7/complete': () => json({ detail: 'Ride cannot be completed before its departure time' }, 409),
    });
    await userEvent.click(await screen.findByRole('button', { name: 'Mark ride completed' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Mark completed' }));
    expect(await within(screen.getByRole('alertdialog')).findByRole('alert')).toHaveTextContent('This ride can be marked completed only after its departure time.');
  });
});

describe('driver overview pending badges (D2)', () => {
  it('one counts call for the page; badge only where requests are pending', async () => {
    const rides = [ride({ id: 1, driver_id: 42, departure_time: futureIso(5) }), ride({ id: 2, driver_id: 42, origin: 'Islamabad', destination: 'Lahore', departure_time: futureIso(6) })];
    const harness = renderPassenger('/driver', {
      'GET /rides/me': () => json(rides, 200, total(2)),
      'GET /rides/me/booking-counts': () => json([{ ride_id: 1, pending: 3, accepted: 0, rejected: 0, cancelled: 0 }, { ride_id: 2, pending: 0, accepted: 1, rejected: 0, cancelled: 0 }]),
    });
    expect(await screen.findByText('3 pending')).toBeInTheDocument();
    expect(screen.queryByText('No pending')).toBeNull();
    expect(screen.getByText(/2 rides shown of 2/)).toBeInTheDocument();
    expect(harness.calls.filter(c => c.url.pathname === '/rides/me/booking-counts')).toHaveLength(1);
  });
});

describe('Idempotency-Key on request-seat and accept/reject (PR #18 tolerance)', () => {
  afterEach(() => vi.unstubAllEnvs());
  const keyOf = (init: RequestInit | undefined) => (init?.headers as Record<string, string> | undefined)?.['Idempotency-Key'];

  it('request-seat: same key when retried after a network failure, a new key for a new action', async () => {
    vi.stubEnv('VITE_SEND_IDEMPOTENCY_KEY', 'true');
    const keys: (string | undefined)[] = [];
    let attempt = 0;
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()),
      'GET /bookings/me': () => json([], 200, total(0)),
      'POST /bookings/7': (_url, init) => {
        keys.push(keyOf(init)); attempt += 1;
        if (attempt === 1) return Promise.reject(new TypeError('Failed to fetch'));
        if (attempt === 2) return json({ detail: 'No seats available' }, 409);
        return json({ id: 70, ride_id: 7, passenger_id: 42, status: 'pending' }, 201);
      },
    });
    const button = await screen.findByRole('button', { name: /Request a seat/ });
    await userEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent('connection');
    await userEvent.click(screen.getByRole('button', { name: /Request a seat/ })); // retry → 409 settles the action
    expect(await screen.findByRole('alert')).toHaveTextContent('This ride is full');
    await waitFor(() => expect(screen.getByRole('button', { name: /Request a seat/ })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: /Request a seat/ })); // new action
    expect(await screen.findByText('Seat requested.')).toBeInTheDocument();
    expect(keys).toHaveLength(3);
    expect(keys[0]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).not.toBe(keys[0]);
  });

  it('accept and reject on the same request use different keys; a retry after 503 reuses the accept key', async () => {
    vi.stubEnv('VITE_SEND_IDEMPOTENCY_KEY', 'true');
    const calls: { accept: string | null; key?: string }[] = [];
    let n = 0;
    renderPassenger('/driver/rides/7', {
      'GET /rides/7': () => json(ride({ id: 7, driver_id: 42, departure_time: futureIso(30), available_seats: 2 })),
      'GET /rides/7/bookings': () => json([{ id: 1, ride_id: 7, passenger_id: 101, status: 'pending', passenger: { id: 101, name: 'Asha Test' } }], 200, total(1)),
      'GET /rides/me/booking-counts': () => json([]),
      'PATCH /bookings/1/respond': (url, init) => {
        calls.push({ accept: url.searchParams.get('accept'), key: keyOf(init) }); n += 1;
        return n <= 2 ? json({ detail: 'busy' }, 503) : json({ detail: 'Booking already responded to' }, 409);
      },
    });
    await userEvent.click(await screen.findByRole('button', { name: 'Accept request from Asha Test' }));
    await screen.findByRole('alert');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept request from Asha Test' })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: 'Accept request from Asha Test' }));
    await waitFor(() => expect(calls).toHaveLength(2));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reject request from Asha Test' })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: 'Reject request from Asha Test' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Reject request' }));
    await waitFor(() => expect(calls).toHaveLength(3));
    expect(calls[0].key).toBeTruthy();
    expect(calls[1].key).toBe(calls[0].key);
    expect(calls[2].accept).toBe('false');
    expect(calls[2].key).toBeTruthy();
    expect(calls[2].key).not.toBe(calls[0].key);
  });

  it('a split-origin dev build does not send the header unless the flag is set (CORS allow_headers on this stack)', async () => {
    const keys: (string | undefined)[] = [];
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()),
      'GET /bookings/me': () => json([], 200, total(0)),
      'POST /bookings/7': (_url, init) => { keys.push(keyOf(init)); return json({ id: 70, ride_id: 7, passenger_id: 42, status: 'pending' }, 201); },
    });
    await userEvent.click(await screen.findByRole('button', { name: /Request a seat/ }));
    expect(await screen.findByText('Seat requested.')).toBeInTheDocument();
    expect(keys).toEqual([undefined]);
  });
});
