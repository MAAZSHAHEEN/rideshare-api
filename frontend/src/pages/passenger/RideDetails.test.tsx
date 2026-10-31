import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { booking, futureIso, json, renderPassenger, ride } from '../../test/passengerHarness';
import { SESSION_KEY } from '../../auth/sessionStore';
import { unavailableReason } from './RideDetails';

const upcoming = () => ride({ departure_time: futureIso() });

describe('ride details and seat request', () => {
  it('requests a seat with POST /bookings/{ride_id} and shows the pending state', async () => {
    const harness = renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()),
      'GET /bookings/me': () => json([]),
      'POST /bookings/7': () => json({ id: 70, ride_id: 7, passenger_id: 42, status: 'pending' }, 201),
    });
    expect(await screen.findByRole('heading', { level: 1, name: 'Lahore to Islamabad' })).toBeInTheDocument();
    expect(screen.getByText('PKR 2,500 per seat')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Request a seat/ }));
    const status = await screen.findByText('Seat requested.');
    expect(status.closest('[role=status]')).toHaveTextContent('pending until the driver accepts or rejects it');
    expect(harness.calls.filter(c => c.method === 'POST')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: /Request a seat/ })).toBeNull();
  });

  it.each([
    ['No seats available', 'This ride is full'],
    ['You already booked this ride', 'already requested a seat'],
    ['Ride has already departed', 'already departed'],
    ['Ride is not active', 'no longer taking requests'],
  ])('maps 409 "%s" to a friendly alert', async (detail, text) => {
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([]),
      'POST /bookings/7': () => json({ detail }, 409),
    });
    await userEvent.click(await screen.findByRole('button', { name: /Request a seat/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(screen.getByRole('alert')).toHaveFocus();
  });

  it.each([
    [403, { detail: 'You cannot book your own ride' }, {}, 'ride you are driving'],
    [404, { detail: 'Ride not found' }, {}, 'couldn’t find this ride'],
    [422, { detail: [{ loc: ['path', 'ride_id'], msg: 'bad' }] }, {}, 'weren’t accepted'],
    [429, { detail: 'Too many requests' }, { 'Retry-After': '42' }, 'wait 42 seconds'],
    [502, { detail: 'upstream secret' }, {}, 'temporarily unavailable'],
  ])('maps %s to a friendly alert', async (status, body, headers, text) => {
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([]),
      'POST /bookings/7': () => json(body, status as number, headers as Record<string, string>),
    });
    await userEvent.click(await screen.findByRole('button', { name: /Request a seat/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(text as string);
    expect(screen.queryByText(/secret/)).toBeNull();
  });

  it('a network failure leaves the button available to retry', async () => {
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([]),
      'POST /bookings/7': () => Promise.reject(new TypeError('Failed to fetch')),
    });
    await userEvent.click(await screen.findByRole('button', { name: /Request a seat/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Check your connection');
    expect(screen.getByRole('button', { name: /Request a seat/ })).toBeEnabled();
  });

  it('a 401 ends the session and sends the passenger to sign in, keeping the return path', async () => {
    renderPassenger('/passenger/rides/7', {
      'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([]),
      'POST /bookings/7': () => json({ detail: 'Invalid token' }, 401),
    });
    await userEvent.click(await screen.findByRole('button', { name: /Request a seat/ }));
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/login'));
    expect(screen.getByText('Your session has ended. Please sign in again.')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it.each([
    ['departed', { departure_time: new Date(Date.now() - 60_000).toISOString() }, 'already departed'],
    ['full', { available_seats: 0 }, 'This ride is full'],
    ['cancelled', { status: 'cancelled' as const }, 'cancelled by the driver'],
    ['completed', { status: 'completed' as const }, 'has been completed'],
  ])('a %s ride is shown as such and is not bookable', async (_, overrides, text) => {
    renderPassenger('/passenger/rides/7', { 'GET /rides/7': () => json({ ...upcoming(), ...overrides }), 'GET /bookings/me': () => json([]) });
    expect(await screen.findByText('Not available to book.')).toBeInTheDocument();
    expect(screen.getByText(text, { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Request a seat/ })).toBeNull();
  });

  it('shows an existing active request instead of offering a duplicate', async () => {
    renderPassenger('/passenger/rides/7', { 'GET /rides/7': () => json(upcoming()), 'GET /bookings/me': () => json([booking({ status: 'accepted' })]) });
    expect(await screen.findByText(/already requested a seat on this ride/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Request a seat/ })).toBeNull();
  });

  it('shows not found for a missing or malformed ride id', async () => {
    renderPassenger('/passenger/rides/999', { 'GET /rides/999': () => json({ detail: 'Ride not found' }, 404), 'GET /bookings/me': () => json([]) });
    expect(await screen.findByRole('heading', { name: 'Ride not found' })).toBeInTheDocument();
  });
  it('never calls the API for a malformed id', async () => {
    const harness = renderPassenger('/passenger/rides/abc', {});
    expect(await screen.findByRole('heading', { name: 'Ride not found' })).toBeInTheDocument();
    expect(harness.calls.some(c => c.url.pathname.startsWith('/rides'))).toBe(false);
  });

  it('unavailableReason treats the exact departure instant as departed', () => {
    const now = Date.parse('2030-06-12T04:30:00Z');
    expect(unavailableReason(ride(), now)).toContain('departed');
    expect(unavailableReason(ride(), now - 1)).toBeNull();
  });
});
