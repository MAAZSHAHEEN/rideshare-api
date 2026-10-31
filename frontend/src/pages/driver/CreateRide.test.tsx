import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { json, renderPassenger, ride } from '../../test/passengerHarness';

// PKT wall clock N hours from now, as the datetime-local input expects.
const pktLocal = (hours: number) => new Date(Date.now() + hours * 3_600_000 + 5 * 3_600_000).toISOString().slice(0, 16);

async function fill(values: { from?: string; to?: string; departure?: string; seats?: string; fare?: string }) {
  if (values.from !== undefined) await userEvent.type(screen.getByLabelText('From'), values.from);
  if (values.to !== undefined) await userEvent.type(screen.getByLabelText('To'), values.to);
  if (values.departure !== undefined) fireEvent.change(screen.getByLabelText('Departure (PKT)'), { target: { value: values.departure } });
  if (values.seats !== undefined) { await userEvent.clear(screen.getByLabelText('Seats offered')); if (values.seats) await userEvent.type(screen.getByLabelText('Seats offered'), values.seats); }
  if (values.fare !== undefined) await userEvent.type(screen.getByLabelText('Fare per seat (PKR)'), values.fare);
}

describe('publish a ride form', () => {
  it('blocks invalid input client-side without calling the API', async () => {
    const harness = renderPassenger('/driver/rides/new', {});
    await screen.findByRole('heading', { name: 'Offer seats on a scheduled ride' });
    await fill({ from: 'Lahore', to: 'lahore', departure: pktLocal(-1), seats: '9', fare: '100001' });
    await userEvent.click(screen.getByRole('button', { name: /Publish ride/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Please fix the highlighted ride details.');
    expect(screen.getByLabelText('To')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Departure (PKT)')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Seats offered')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Fare per seat (PKR)')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('Choose a destination different from the starting point.')).toBeInTheDocument();
    expect(screen.getByText(/departure time in the future/)).toBeInTheDocument();
    expect(harness.calls.filter(c => c.method === 'POST')).toHaveLength(0);
  });

  it('requires every field', async () => {
    const harness = renderPassenger('/driver/rides/new', {});
    await screen.findByRole('heading', { name: 'Offer seats on a scheduled ride' });
    await fill({ seats: '' });
    await userEvent.click(screen.getByRole('button', { name: /Publish ride/ }));
    expect(screen.getByText('Enter where the ride leaves from.')).toBeInTheDocument();
    expect(screen.getByText('Enter where the ride goes to.')).toBeInTheDocument();
    expect(screen.getByText('Choose the departure date and time.')).toBeInTheDocument();
    expect(harness.calls.filter(c => c.method === 'POST')).toHaveLength(0);
  });

  it('publishes with a +05:00 departure and opens the new ride', async () => {
    const departure = pktLocal(30);
    let body: Record<string, unknown> = {};
    const created = ride({ id: 31, driver_id: 42, departure_time: `${departure}:00+05:00`, fare_per_seat: 1800, available_seats: 2 });
    const harness = renderPassenger('/driver/rides/new', {
      'POST /rides/': (_, init) => { body = JSON.parse(String(init.body)); return json(created, 201); },
      'GET /rides/31': () => json(created),
      'GET /rides/31/bookings': () => json([]),
    });
    await screen.findByRole('heading', { name: 'Offer seats on a scheduled ride' });
    await fill({ from: ' Lahore ', to: 'Islamabad', departure, seats: '2', fare: '1800' });
    expect(screen.getByText(/^Departs .* PKT\.$/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Publish ride/ }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/driver/rides/31'));
    expect(body).toEqual({ origin: 'Lahore', destination: 'Islamabad', departure_time: `${departure}:00+05:00`, available_seats: 2, fare_per_seat: 1800 });
    expect(await screen.findByText('Ride published.')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'No requests yet.' })).toBeInTheDocument();
    expect(harness.calls.filter(c => c.method === 'POST')).toHaveLength(1);
  });

  it('maps a server 422 onto the field with safe copy (server stays authoritative)', async () => {
    renderPassenger('/driver/rides/new', { 'POST /rides/': () => json({ detail: 'Departure time must be in the future' }, 422) });
    await screen.findByRole('heading', { name: 'Offer seats on a scheduled ride' });
    await fill({ from: 'Lahore', to: 'Islamabad', departure: pktLocal(2), fare: '2000' });
    await userEvent.click(screen.getByRole('button', { name: /Publish ride/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Some ride details weren’t accepted.');
    expect(screen.getByLabelText('Departure (PKT)')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText(/departure time wasn’t accepted/)).toBeInTheDocument();
  });

  it.each([
    [403, { detail: 'Only drivers can create rides' }, {}, 'Your account can’t publish rides.'],
    [429, { detail: 'Too many requests' }, { 'Retry-After': '20' }, 'Please wait 20 seconds'],
    [500, { detail: 'IntegrityError: rides_pkey' }, {}, 'temporarily unavailable'],
  ])('shows a safe message for %s', async (status, body, headers, text) => {
    renderPassenger('/driver/rides/new', { 'POST /rides/': () => json(body, status, headers) });
    await screen.findByRole('heading', { name: 'Offer seats on a scheduled ride' });
    await fill({ from: 'Lahore', to: 'Rawalpindi', departure: pktLocal(5), fare: '2000' });
    await userEvent.click(screen.getByRole('button', { name: /Publish ride/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(screen.queryByText(/IntegrityError|rides_pkey/)).toBeNull();
  });

  it('ends the session on 401 and returns to sign-in', async () => {
    renderPassenger('/driver/rides/new', { 'POST /rides/': () => json({ detail: 'Invalid token' }, 401) });
    await screen.findByRole('heading', { name: 'Offer seats on a scheduled ride' });
    await fill({ from: 'Lahore', to: 'Islamabad', departure: pktLocal(5), fare: '2000' });
    await userEvent.click(screen.getByRole('button', { name: /Publish ride/ }));
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/login'));
  });
});
