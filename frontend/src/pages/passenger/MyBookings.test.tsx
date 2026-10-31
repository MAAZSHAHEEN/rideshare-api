import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { booking, futureIso, json, renderPassenger, ride } from '../../test/passengerHarness';
import { canCancel, groupByStatus } from './MyBookings';

const upcoming = (id: number, overrides = {}) => ride({ id, departure_time: futureIso(24 + id), ...overrides });
const list = () => [
  booking({ id: 1, ride_id: 1, status: 'pending', ride: upcoming(1, { origin: 'Lahore', destination: 'Islamabad' }) }),
  booking({ id: 2, ride_id: 2, status: 'accepted', ride: upcoming(2, { origin: 'Islamabad', destination: 'Lahore' }) }),
  booking({ id: 3, ride_id: 3, status: 'rejected', ride: upcoming(3, { origin: 'Lahore', destination: 'Rawalpindi' }) }),
  booking({ id: 4, ride_id: 4, status: 'cancelled', ride: upcoming(4, { origin: 'Rawalpindi', destination: 'Lahore' }) }),
  booking({ id: 5, ride_id: 5, status: 'pending', ride: ride({ id: 5, origin: 'Multan', destination: 'Lahore', departure_time: new Date(Date.now() - 3_600_000).toISOString() }) }),
];

describe('my bookings', () => {
  it('groups by status in a fixed order and labels departed rides', async () => {
    renderPassenger('/passenger?view=bookings', { 'GET /bookings/me': () => json(list()) });
    const groups = await screen.findAllByRole('heading', { level: 3 });
    expect(groups.map(g => g.textContent)).toEqual(['Pending (2)', 'Accepted (1)', 'Rejected (1)', 'Cancelled (1)']);
    const departed = screen.getByRole('article', { name: 'Multan to Lahore' });
    expect(within(departed).getByText('Departed')).toBeInTheDocument();
  });

  it('offers cancel only where the API allows it', () => {
    expect(canCancel(booking({ status: 'pending' }))).toBe(true);
    expect(canCancel(booking({ status: 'accepted' }))).toBe(true);
    expect(canCancel(booking({ status: 'rejected' }))).toBe(false);
    expect(canCancel(booking({ status: 'cancelled' }))).toBe(false);
    expect(canCancel(booking({ status: 'pending', ride: ride({ status: 'completed' }) }))).toBe(false);
    expect(groupByStatus([])).toEqual([]);
  });

  it('confirms before cancelling, then shows the booking as cancelled', async () => {
    let bookings = list();
    const harness = renderPassenger('/passenger?view=bookings', {
      'GET /bookings/me': () => json(bookings),
      'PATCH /bookings/1/cancel': () => { bookings = bookings.map(b => b.id === 1 ? { ...b, status: 'cancelled' } : b); return json({ id: 1, ride_id: 1, passenger_id: 42, status: 'cancelled' }); },
    });
    const card = await screen.findByRole('article', { name: 'Lahore to Islamabad' });
    const trigger = within(card).getByRole('button', { name: /^Cancel booking: Lahore to Islamabad/ });
    await userEvent.click(trigger);
    const dialog = screen.getByRole('alertdialog', { name: 'Cancel this booking?' });
    expect(within(dialog).getByRole('button', { name: 'Keep booking' })).toHaveFocus();
    // Keyboard: Escape keeps the booking and returns focus to the trigger.
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(trigger).toHaveFocus();
    expect(harness.calls.some(c => c.method === 'PATCH')).toBe(false);
    await userEvent.click(trigger);
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel booking' }));
    expect(await screen.findByText('Booking cancelled: Lahore to Islamabad.')).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole('heading', { level: 3 }).map(g => g.textContent)).toEqual(['Pending (1)', 'Accepted (1)', 'Rejected (1)', 'Cancelled (2)']));
    const cancelled = screen.getByRole('article', { name: 'Lahore to Islamabad' });
    expect(within(cancelled).getByText('cancelled')).toBeInTheDocument();
    expect(within(cancelled).queryByRole('button', { name: /Cancel booking/ })).toBeNull();
  });

  it('keeps the dialog open with a friendly message when cancellation conflicts', async () => {
    renderPassenger('/passenger?view=bookings', {
      'GET /bookings/me': () => json(list()),
      'PATCH /bookings/2/cancel': () => json({ detail: 'Booking cannot be cancelled' }, 409),
    });
    const card = await screen.findByRole('article', { name: 'Islamabad to Lahore' });
    await userEvent.click(within(card).getByRole('button', { name: /^Cancel booking/ }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel booking' }));
    expect(await within(screen.getByRole('alertdialog')).findByRole('alert')).toHaveTextContent('can no longer be cancelled');
  });

  it('filters by status with the server-side status parameter', async () => {
    const harness = renderPassenger('/passenger?view=bookings', { 'GET /bookings/me': url => json(url.searchParams.get('status') === 'accepted' ? [list()[1]] : list()) });
    await screen.findAllByRole('article');
    await userEvent.click(screen.getByRole('button', { name: 'Accepted' }));
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(1));
    expect(screen.getByRole('button', { name: 'Accepted' })).toHaveAttribute('aria-pressed', 'true');
    const last = harness.calls.at(-1)!.url;
    expect(last.searchParams.get('status')).toBe('accepted');
    expect(last.searchParams.get('limit')).toBe('20');
  });

  it('has empty and error states', async () => {
    renderPassenger('/passenger?view=bookings', { 'GET /bookings/me': () => json([]) });
    expect(await screen.findByRole('heading', { name: 'No bookings yet.' })).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'All bookings' })).getByRole('link', { name: 'Find a ride' })).toHaveAttribute('href', '/passenger?view=search');
  });
  it('shows a load error with retry', async () => {
    renderPassenger('/passenger?view=bookings', { 'GET /bookings/me': () => json({ detail: 'x' }, 500) });
    expect(await screen.findByText('We couldn’t load your bookings.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });
});
