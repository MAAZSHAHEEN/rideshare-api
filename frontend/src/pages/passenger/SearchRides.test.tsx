import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { json, renderPassenger, ride } from '../../test/passengerHarness';
import { karachiToday } from '../../passenger/time';
import { validateFilters } from './SearchRides';

const tomorrow = () => new Date(Date.parse(`${karachiToday()}T00:00:00Z`) + 86_400_000).toISOString().slice(0, 10);

describe('ride search filters', () => {
  it('sends from, to and a PKT day window, and shows seats, PKR fare, PKT time and status', async () => {
    const day = tomorrow();
    const harness = renderPassenger('/passenger/search', {
      'GET /rides/': url => json(url.searchParams.get('origin') ? [ride({ available_seats: 1 })] : []),
    });
    expect(await screen.findByText('No rides found.')).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('From'), ' Lahore ');
    await userEvent.type(screen.getByLabelText('To'), 'Islamabad');
    await userEvent.type(screen.getByLabelText('Date (PKT)'), day);
    await userEvent.click(screen.getByRole('button', { name: /Search rides/ }));
    const card = await screen.findByRole('article', { name: 'Lahore to Islamabad' });
    const last = harness.calls.filter(c => c.url.pathname === '/rides/').at(-1)!.url;
    const next = new Date(Date.parse(`${day}T00:00:00Z`) + 86_400_000).toISOString().slice(0, 10);
    expect(Object.fromEntries(last.searchParams)).toEqual({ origin: 'Lahore', destination: 'Islamabad', departure_from: `${day}T00:00:00+05:00`, departure_before: `${next}T00:00:00+05:00`, limit: '20', offset: '0' });
    expect(within(card).getByText('PKR 2,500')).toBeInTheDocument();
    expect(within(card).getByText('1 seat left')).toBeInTheDocument();
    expect(within(card).getByText('Wed 12 Jun 2030, 9:30 am PKT')).toBeInTheDocument();
    expect(within(card).getByText('active')).toBeInTheDocument();
    expect(screen.getByText('1 ride found.')).toBeInTheDocument();
    // Filters live in the URL so the search survives reloads and the back button.
    expect(screen.getByTestId('location').textContent).toBe(`/passenger/search?from=Lahore&to=Islamabad&date=${day}`);
  });

  it('blocks a past date and identical places before calling the API', async () => {
    const harness = renderPassenger('/passenger/search', { 'GET /rides/': () => json([]) });
    await screen.findByText('No rides found.');
    const before = harness.calls.length;
    await userEvent.type(screen.getByLabelText('From'), 'Lahore');
    await userEvent.type(screen.getByLabelText('To'), 'lahore');
    await userEvent.type(screen.getByLabelText('Date (PKT)'), '2020-01-01');
    await userEvent.click(screen.getByRole('button', { name: /Search rides/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Please check the highlighted search filters.');
    expect(screen.getByLabelText('To')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('To')).toHaveAccessibleDescription('Choose a destination different from where you’re leaving.');
    expect(screen.getByLabelText('Date (PKT)')).toHaveAttribute('aria-invalid', 'true');
    expect(harness.calls.length).toBe(before);
  });

  it('validates hand-edited URLs too and never sends them', async () => {
    const harness = renderPassenger('/passenger/search?date=2026-02-30', { 'GET /rides/': () => json([]) });
    expect(await screen.findByText('Fix the filters to search.')).toBeInTheDocument();
    expect(harness.calls.some(c => c.url.pathname === '/rides/')).toBe(false);
    expect(validateFilters({ from: 'a'.repeat(101), to: '', date: '' }).from).toBeTruthy();
  });

  it('shows an error state with retry that recovers, without leaking server text', async () => {
    let fail = true;
    renderPassenger('/passenger/search', { 'GET /rides/': () => fail ? json({ detail: 'asyncpg pool exhausted' }, 503) : json([ride()]) });
    expect(await screen.findByText('We couldn’t load rides.')).toBeInTheDocument();
    expect(screen.queryByText(/asyncpg/)).toBeNull();
    fail = false;
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('article', { name: 'Lahore to Islamabad' })).toBeInTheDocument();
  });

  it('pages with offset and disables Next on a short page', async () => {
    const harness = renderPassenger('/passenger/search', {
      'GET /rides/': url => json(url.searchParams.get('offset') === '0' ? Array.from({ length: 20 }, (_, i) => ride({ id: i + 1 })) : [ride({ id: 99 })]),
    });
    await screen.findAllByRole('article');
    await userEvent.click(screen.getByRole('button', { name: 'Next →' }));
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(1));
    expect(harness.calls.at(-1)!.url.searchParams.get('offset')).toBe('20');
    expect(screen.getByRole('button', { name: 'Next →' })).toBeDisabled();
  });
});
