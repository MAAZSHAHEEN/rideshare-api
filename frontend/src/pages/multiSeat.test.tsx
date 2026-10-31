import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { createApiClient } from '../api/client';
import { MAX_SEATS_PER_BOOKING, seatCountLabel, seatsOf } from '../api/types';
import type { DriverBooking, Ride } from '../api/types';
import { driverErrorMessage } from '../driver/errors';
import { acceptSeatShortfall } from '../driver/state';
import { ApiError } from '../api/client';
import { passengerErrorMessage } from '../passenger/errors';
import { booking, futureIso, json, renderPassenger, ride } from '../test/passengerHarness';
import { bookScope, maxSelectableSeats } from './passenger/RideDetails';

// Multi-seat bookings (#22, docs/architecture/BOOKING_SCHEMA_CHANGES.md §3). Fake fixtures only.
const upcoming = (overrides: Partial<Ride> = {}) => ride({ departure_time: futureIso(), ...overrides });
const keyOf = (init: RequestInit) => (init.headers as Record<string, string>)['Idempotency-Key'];
const bodyOf = (init: RequestInit) => (init.body === undefined ? undefined : JSON.parse(init.body as string));
const created = (seats: number) => json({ id: 70, ride_id: 7, passenger_id: 42, status: 'pending', seats_requested: seats }, 201);
const seatSelect = () => screen.getByRole('combobox', { name: 'Seats' });
const optionValues = () => within(seatSelect()).getAllByRole('option').map(o => (o as HTMLOptionElement).value);

describe('multi-seat helpers', () => {
  it('seatsOf reads seats_requested and treats missing or invalid values as 1', () => {
    expect(seatsOf({ seats_requested: 3 })).toBe(3);
    expect(seatsOf({})).toBe(1);
    for (const bad of [0, 9, -1, 2.5, Number.NaN]) expect(seatsOf({ seats_requested: bad })).toBe(1);
    expect(seatCountLabel(1)).toBe('1 seat');
    expect(seatCountLabel(2)).toBe('2 seats');
  });
  it('the selector bound is min(available, 8), 0 when full', () => {
    expect(MAX_SEATS_PER_BOOKING).toBe(8);
    expect([0, -2, 1, 3, 8, 9, 40].map(n => maxSelectableSeats({ available_seats: n }))).toEqual([0, 0, 1, 3, 8, 8, 8]);
  });
  it('the idempotency scope includes the seat count', () => {
    expect(bookScope(7, 1)).toBe('book:7:1');
    expect(bookScope(7, 2)).not.toBe(bookScope(7, 3));
  });
  it('acceptSeatShortfall explains only a request that needs more seats than are left', () => {
    const r = upcoming({ available_seats: 2 });
    expect(acceptSeatShortfall({ seats_requested: 2 }, r)).toBeNull();
    expect(acceptSeatShortfall({}, r)).toBeNull();
    expect(acceptSeatShortfall({ seats_requested: 3 }, r)).toBe('Needs 3 seats, but only 2 are left. Reject it, or wait until seats free up.');
    expect(acceptSeatShortfall({ seats_requested: 2 }, upcoming({ available_seats: 1 }))).toContain('only 1 is left');
    expect(acceptSeatShortfall({ seats_requested: 3 }, upcoming({ available_seats: 0 }))).toBeNull(); // the ride-level "full" reason covers it
  });
  it('maps the new 409 and the lowered-cap 422 without echoing other text', () => {
    expect(passengerErrorMessage(new ApiError(409, '', {}, 'Not enough seats available'), 'book')).toBe('Not enough seats are left for that many. Choose fewer seats and try again.');
    expect(passengerErrorMessage(new ApiError(422, '', {}, 'At most 4 seats per booking'), 'book')).toBe('You can request at most 4 seats at a time. Choose fewer seats.');
    expect(passengerErrorMessage(new ApiError(422, '', {}, 'At most 1 seat per booking'), 'book')).toBe('You can request at most 1 seat at a time. Choose fewer seats.');
    expect(passengerErrorMessage(new ApiError(422, '', {}, 'At most 99 seats per booking <b>'), 'book')).toBe('Some details weren’t accepted. Please refresh and try again.');
    expect(driverErrorMessage(new ApiError(409, '', {}, 'Not enough seats available'), 'accept')).toContain('needs more seats than are left');
  });
});

describe('api client bookRide seats', () => {
  it('sends no body for 1 seat and {"seats": n} otherwise; never an invalid count', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response('{}'))));
    const api = createApiClient(() => ({ token: 't', generation: 1 }), vi.fn(), 'http://localhost:8000');
    await api.bookRide(7);
    await api.bookRide(7, undefined, 'k-1', 1);
    await api.bookRide(7, undefined, 'k-2', 2);
    await api.bookRide(7, undefined, undefined, 8);
    for (const bad of [0, 9, 1.5, Number.NaN]) await expect(api.bookRide(7, undefined, 'k', bad)).rejects.toThrow('Invalid seat count');
    const calls = (fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls).toHaveLength(4);
    expect(calls.map(c => c[1].body)).toEqual([undefined, undefined, '{"seats":2}', '{"seats":8}']);
    expect(calls[0][1].headers).not.toHaveProperty('Content-Type');
    expect(calls[2][1].headers['Content-Type']).toBe('application/json');
  });
});

describe('ride details: seat selector', () => {
  it.each([[3, ['1', '2', '3']], [1, ['1']], [12, ['1', '2', '3', '4', '5', '6', '7', '8']]])('with %s seats left offers %j', async (left, values) => {
    renderPassenger('/passenger/rides/7', { 'GET /rides/7': () => json(upcoming({ available_seats: left as number })), 'GET /bookings/me': () => json([]) });
    await screen.findByRole('heading', { level: 1, name: 'Lahore to Islamabad' });
    expect(optionValues()).toEqual(values);
    expect((seatSelect() as HTMLSelectElement).value).toBe('1');
  });

  it('is hidden when the ride is full', async () => {
    renderPassenger('/passenger/rides/7', { 'GET /rides/7': () => json(upcoming({ available_seats: 0 })), 'GET /bookings/me': () => json([]) });
    expect(await screen.findByText(/There are no seats left to request/)).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Seats' })).toBeNull();
    expect(screen.queryByRole('button', { name: /Request/ })).toBeNull();
  });

  it('1 seat keeps the old no-body request; 2 seats sends {"seats": 2} with the total shown', async () => {
    const bodies: unknown[] = [];
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([]),
      'POST /bookings/7': (_url, init) => { bodies.push(bodyOf(init)); return created(bodyOf(init)?.seats ?? 1); },
    });
    await screen.findByRole('heading', { level: 1, name: 'Lahore to Islamabad' });
    expect(screen.getByText('PKR 2,500', { selector: 'strong' })).toBeInTheDocument();
    await userEvent.selectOptions(seatSelect(), '2');
    expect(document.getElementById('seat-total')).toHaveTextContent('Total PKR 5,000 for 2 seats (PKR 2,500 each)');
    await userEvent.click(screen.getByRole('button', { name: /Request 2 seats/ }));
    expect(await screen.findByText('2 seats requested.')).toBeInTheDocument();
    expect(bodies).toEqual([{ seats: 2 }]);
  });

  it('sends exactly one 1-seat request with no body', async () => {
    const bodies: unknown[] = [];
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([]),
      'POST /bookings/7': (_url, init) => { bodies.push(bodyOf(init)); return created(1); },
    });
    await userEvent.click(await screen.findByRole('button', { name: /Request a seat/ }));
    expect(await screen.findByText('Seat requested.')).toBeInTheDocument();
    expect(bodies).toEqual([undefined]);
  });

  it('a retry reuses the key for the same seat count; a different count gets a new key', async () => {
    const sent: { seats: number; key: string }[] = [];
    let attempt = 0;
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([]),
      'POST /bookings/7': (_url, init) => {
        sent.push({ seats: bodyOf(init)?.seats ?? 1, key: keyOf(init) }); attempt += 1;
        return attempt < 3 ? Promise.reject(new TypeError('Failed to fetch')) : created(3);
      },
    });
    await screen.findByRole('heading', { level: 1, name: 'Lahore to Islamabad' });
    await userEvent.selectOptions(seatSelect(), '2');
    await userEvent.click(screen.getByRole('button', { name: /Request 2 seats/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Check your connection');
    await userEvent.click(screen.getByRole('button', { name: /Request 2 seats/ }));
    await waitFor(() => expect(sent).toHaveLength(2));
    await userEvent.selectOptions(seatSelect(), '3');
    expect(screen.queryByRole('alert')).toBeNull(); // changing the count clears the stale error
    await userEvent.click(screen.getByRole('button', { name: /Request 3 seats/ }));
    expect(await screen.findByText('3 seats requested.')).toBeInTheDocument();
    expect(sent.map(s => s.seats)).toEqual([2, 2, 3]);
    expect(sent[1].key).toBe(sent[0].key);
    expect(sent[2].key).not.toBe(sent[0].key);
  });

  it('409 "Not enough seats available" shows fixed copy, refetches the ride and shrinks the selector', async () => {
    let left = 3;
    const harness = renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming({ available_seats: left })), 'GET /bookings/me': () => json([]),
      'POST /bookings/7': () => { left = 1; return json({ detail: 'Not enough seats available' }, 409); },
    });
    await screen.findByRole('heading', { level: 1, name: 'Lahore to Islamabad' });
    await userEvent.selectOptions(seatSelect(), '3');
    await userEvent.click(screen.getByRole('button', { name: /Request 3 seats/ }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Not enough seats are left for that many. Choose fewer seats and try again.');
    expect(alert).toHaveFocus();
    await waitFor(() => expect(optionValues()).toEqual(['1']));
    expect(harness.calls.filter(c => c.method === 'GET' && c.url.pathname === '/rides/7').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('button', { name: /Request a seat/ })).toBeEnabled();
  });

  it('422 lowered cap shows the server cap as a validated number', async () => {
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([]),
      'POST /bookings/7': () => json({ detail: 'At most 2 seats per booking' }, 422),
    });
    await screen.findByRole('heading', { level: 1, name: 'Lahore to Islamabad' });
    await userEvent.selectOptions(seatSelect(), '3');
    await userEvent.click(screen.getByRole('button', { name: /Request 3 seats/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('You can request at most 2 seats at a time.');
  });

  it('an existing multi-seat request is shown with its seat count', async () => {
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([booking({ seats_requested: 2, status: 'accepted' })]),
    });
    expect(await screen.findByText(/already requested 2 seats on this ride/)).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Seats' })).toBeNull();
  });
});

describe('my bookings: seat counts', () => {
  it('shows "N seats" and the total fare on every card; cancel copy names the seats', async () => {
    renderPassenger('/passenger/bookings', {
      'GET /bookings/me': () => json([
        booking({ id: 1, ride_id: 1, status: 'accepted', seats_requested: 2, ride: ride({ id: 1, departure_time: futureIso(30) }) }),
        booking({ id: 2, ride_id: 2, status: 'pending', ride: ride({ id: 2, departure_time: futureIso(40) }) }),
      ]),
    });
    const cards = await screen.findAllByRole('article');
    cards.reverse(); // grouped Pending first, then Accepted
    expect(within(cards[0]).getByText('2 seats')).toBeInTheDocument();
    expect(cards[0]).toHaveTextContent('PKR 5,000 (2 × PKR 2,500)');
    expect(within(cards[1]).getByText('1 seat')).toBeInTheDocument();
    await userEvent.click(within(cards[0]).getByRole('button', { name: /Cancel booking/ }));
    expect(await screen.findByText(/Your 2 confirmed seats will be released/)).toBeInTheDocument();
  });
});

describe('driver ride: multi-seat requests', () => {
  const mine = (left: number) => ride({ id: 7, driver_id: 42, departure_time: futureIso(30), available_seats: left });
  const req = (id: number, name: string, seats: number): DriverBooking =>
    ({ id, ride_id: 7, passenger_id: 100 + id, status: 'pending', seats_requested: seats, passenger: { id: 100 + id, name } });

  it('shows "N seats", disables accept with a reason when a request needs more than are left, and accepts one that fits', async () => {
    const state = { left: 2, requests: [req(1, 'Two Seat Passenger', 2), req(2, 'Three Seat Passenger', 3)] };
    renderPassenger('/driver/rides/7', {
      'GET /rides/7': () => json(mine(state.left)),
      'GET /rides/7/bookings': () => json(state.requests),
      'PATCH /bookings/1/respond': () => { state.left -= 2; state.requests[0] = { ...state.requests[0], status: 'accepted' }; return json(state.requests[0]); },
    });
    const three = await screen.findByRole('button', { name: 'Accept request from Three Seat Passenger' });
    expect(three).toBeDisabled();
    expect(three).toHaveAccessibleDescription('Needs 3 seats, but only 2 are left. Reject it, or wait until seats free up.');
    expect(screen.getByRole('button', { name: 'Reject request from Three Seat Passenger' })).toBeEnabled();
    const two = screen.getByRole('button', { name: 'Accept request from Two Seat Passenger' });
    expect(two).toBeEnabled();
    expect(screen.getAllByText('2 seats').length).toBeGreaterThan(0);
    expect(screen.getByText('3 seats')).toBeInTheDocument();
    await userEvent.click(two);
    expect(await screen.findByText('Accepted Two Seat Passenger’s request. 2 seats are now taken.')).toBeInTheDocument();
  });

  it('409 "Not enough seats available" on accept shows specific copy', async () => {
    renderPassenger('/driver/rides/7', {
      'GET /rides/7': () => json(mine(2)),
      'GET /rides/7/bookings': () => json([req(1, 'Racing Passenger', 2)]),
      'PATCH /bookings/1/respond': () => json({ detail: 'Not enough seats available' }, 409),
    });
    await userEvent.click(await screen.findByRole('button', { name: 'Accept request from Racing Passenger' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('This request needs more seats than are left');
  });
});
