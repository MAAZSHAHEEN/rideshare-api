import { useEffect, useRef, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { ApiError } from '../../api/client';
import { hasNextPage } from '../../api/client';
import { seatCountLabel, seatsOf } from '../../api/types';
import type { BookingStatus, PassengerBooking } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { Icon } from '../../components/Icon';
import { passengerErrorMessage } from '../../passenger/errors';
import { formatDeparture, formatFare, hasDeparted } from '../../passenger/time';
import { usePassengerView } from './PassengerArea';

export const STATUS_ORDER: BookingStatus[] = ['pending', 'accepted', 'rejected', 'cancelled', 'expired'];
const LABELS: Record<BookingStatus, string> = { pending: 'Pending', accepted: 'Accepted', rejected: 'Rejected', cancelled: 'Cancelled', expired: 'Expired' };
const GROUP_HELP: Record<BookingStatus, string> = {
  pending: 'Waiting for the driver to accept or reject.',
  accepted: 'Your seat is confirmed by the driver.',
  rejected: 'The driver didn’t accept these requests.',
  cancelled: 'Cancelled by you, or because the ride was cancelled or completed.',
  expired: 'The driver didn’t answer in time, or the ride departed first.',
};
/** 'expired' (PR #20) only gets a filter tab once the server accepts it as a status filter. */
const OPTIONAL_TABS: BookingStatus[] = ['expired'];
export const BOOKINGS_PAGE = 20;

/**
 * Matches services/bookings.cancel_booking: pending/accepted bookings on an
 * active ride, except an accepted seat at or after departure (PR #12 cut-off:
 * 409 "Ride has already departed"). A pending request can still be withdrawn.
 */
export function canCancel(booking: PassengerBooking, now = Date.now()): boolean {
  if (booking.ride.status !== 'active') return false;
  if (booking.status === 'pending') return true;
  return booking.status === 'accepted' && !hasDeparted(booking.ride.departure_time, now);
}

export function rideState(booking: PassengerBooking, now = Date.now()): { label: string; tone: string } {
  if (booking.ride.status === 'cancelled') return { label: 'Ride cancelled', tone: 'cancelled' };
  if (booking.ride.status === 'completed') return { label: 'Ride completed', tone: 'completed' };
  if (hasDeparted(booking.ride.departure_time, now)) return { label: 'Departed', tone: 'departed' };
  return { label: 'Upcoming', tone: 'upcoming' };
}

export function groupByStatus(bookings: PassengerBooking[]): [BookingStatus, PassengerBooking[]][] {
  return STATUS_ORDER.map(status => [status, bookings.filter(b => b.status === status)] as [BookingStatus, PassengerBooking[]]).filter(([, items]) => items.length > 0);
}

export function MyBookings() {
  const { api, user } = useAuth();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const raw = params.get('status');
  const status = STATUS_ORDER.includes(raw as BookingStatus) ? raw as BookingStatus : null;
  const page = Math.max(0, Math.min(10_000, Number.parseInt(params.get('page') ?? '0', 10) || 0));
  const limit = BOOKINGS_PAGE;
  const [target, setTarget] = useState<PassengerBooking | null>(null);
  const [dialogError, setDialogError] = useState('');
  const [done, setDone] = useState('');
  const doneRef = useRef<HTMLDivElement>(null);
  const [now] = useState(() => Date.now());
  usePassengerView('My bookings', 'bookings-heading');
  useEffect(() => { if (done) doneRef.current?.focus(); }, [done]);

  const query = useQuery({
    queryKey: ['my-bookings', user!.id, 'list', status ?? 'all', page],
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) => api.myBookingsPage(page * limit, signal, { status: status ?? undefined, limit }),
  });
  // Tab counts from X-Total-Count (G7): one single-row read per status, so counts cover every page.
  const counts = useQuery({
    queryKey: ['my-bookings', user!.id, 'tab-counts'],
    queryFn: async ({ signal }) => {
      // Each count is independent: a status the server doesn't know yet (422) or a failed read only hides that count.
      const read = (status?: BookingStatus) => api.myBookingsPage(0, signal, { status, limit: 1 }).then(p => p.total, () => null);
      const [all, ...each] = await Promise.all([read(), ...STATUS_ORDER.map(option => read(option))]);
      return { all, ...Object.fromEntries(STATUS_ORDER.map((option, i) => [option, each[i]])) } as Record<BookingStatus | 'all', number | null>;
    },
  });
  const cancel = useMutation({
    mutationFn: (booking: PassengerBooking) => api.cancelBooking(booking.id),
    onMutate: () => setDialogError(''),
    onSuccess: (_, booking) => {
      setTarget(null);
      setDone(`Booking cancelled: ${booking.ride.origin} to ${booking.ride.destination}.`);
      void queryClient.invalidateQueries({ queryKey: ['my-bookings'] });
      void queryClient.invalidateQueries({ queryKey: ['ride'] });
      void queryClient.invalidateQueries({ queryKey: ['ride-search'] });
    },
    onError: reason => {
      if (reason instanceof ApiError && reason.status === 401) return;
      setDialogError(passengerErrorMessage(reason, 'cancel'));
      if (reason instanceof ApiError && (reason.status === 409 || reason.status === 404)) void queryClient.invalidateQueries({ queryKey: ['my-bookings'] });
    },
  });

  function select(next: BookingStatus | null) {
    setDone('');
    setParams(next ? new URLSearchParams({ status: next }) : new URLSearchParams());
  }
  function goToPage(next: number) {
    const nextParams = new URLSearchParams(params);
    if (next > 0) nextParams.set('page', String(next)); else nextParams.delete('page');
    setParams(nextParams);
  }

  const bookings = query.data?.items ?? [];
  const total = query.data?.total ?? null;
  const groups = groupByStatus(bookings);
  const tabCount = (option: BookingStatus | null) => counts.data ? counts.data[option ?? 'all'] : null;
  const tabs = STATUS_ORDER.filter(option => !OPTIONAL_TABS.includes(option) || option === status || typeof tabCount(option) === 'number');
  return <div className="passenger-view">
    <div className="view-heading"><span className="eyebrow">MY BOOKINGS</span><h1 id="bookings-heading" tabIndex={-1}>Your seat requests</h1><p>Every request you’ve made, grouped by status. Times are in Pakistan time (PKT).</p></div>
    <nav className="status-tabs" aria-label="Filter bookings by status">
      {[null, ...tabs].map(option => <button key={option ?? 'all'} type="button" aria-pressed={status === option} className={status === option ? 'selected' : ''} onClick={() => select(option)}>{option ? LABELS[option] : 'All'}{tabCount(option) !== null && <span className="tab-count"> ({tabCount(option)})</span>}</button>)}
    </nav>
    {done && <div className="notice success" role="status" tabIndex={-1} ref={doneRef}>{done}</div>}
    <section className="results-panel" aria-labelledby="bookings-list-heading" aria-busy={query.isFetching}>
      <div className="history-heading"><div><h2 id="bookings-list-heading">{status ? `${LABELS[status]} bookings` : 'All bookings'}</h2><p>{status ? GROUP_HELP[status] : 'Newest first within each group.'}{total !== null && total > 0 ? ` ${pageSummary(page * limit, bookings.length, total)}` : ''}</p></div>
        <button className="button button-outline small" onClick={() => void query.refetch()} disabled={query.isFetching}>{query.isFetching ? 'Refreshing…' : 'Refresh'}</button></div>
      {query.isPending ? <div className="state-panel" role="status"><span className="spinner blue" aria-hidden="true" /><p>Loading your bookings…</p></div>
        : query.isError ? <div className="state-panel"><h3>We couldn’t load your bookings.</h3><p role="alert">{passengerErrorMessage(query.error, 'load-bookings')}</p><button className="button small" onClick={() => void query.refetch()}>Try again</button></div>
        : bookings.length === 0 ? <div className="state-panel"><span className="empty-illustration"><Icon name="bag" size={34} /></span><h3>{page ? 'No more bookings.' : status ? `No ${LABELS[status].toLowerCase()} bookings.` : 'No bookings yet.'}</h3><p>{page ? 'Go back to see earlier bookings.' : 'Find an upcoming ride and request a seat.'}</p><Link className="button small" to="/passenger/search">Find a ride</Link></div>
        : <div className="booking-groups">{groups.map(([group, items]) => <section key={group} className="booking-group" aria-labelledby={`group-${group}`}>
          <h3 id={`group-${group}`}>{LABELS[group]} <span className="group-count">({items.length})</span></h3>
          {!status && <p className="group-help">{GROUP_HELP[group]}</p>}
          <ul className="ride-list">{items.map(booking => <li key={booking.id}><BookingCard booking={booking} now={now} onCancel={() => { setDialogError(''); setDone(''); setTarget(booking); }} /></li>)}</ul>
        </section>)}</div>}
      <div className="pagination"><span>Page {page + 1}{total !== null ? ` of ${Math.max(1, Math.ceil(total / limit))}` : ''}</span><div><button disabled={page === 0 || query.isFetching} onClick={() => goToPage(page - 1)}>← Previous</button><button disabled={!hasNextPage(page * limit, bookings.length, limit, total) || query.isFetching || query.isError} onClick={() => goToPage(page + 1)}>Next →</button></div></div>
    </section>
    {target && <ConfirmDialog title="Cancel this booking?" confirmLabel="Cancel booking" cancelLabel="Keep booking" busy={cancel.isPending} error={dialogError}
      onCancel={() => { if (!cancel.isPending) setTarget(null); }} onConfirm={() => cancel.mutate(target)}>
      <p>{target.ride.origin} → {target.ride.destination}, {formatDeparture(target.ride.departure_time)}.</p>
      <p>{target.status === 'accepted' ? (seatsOf(target) === 1 ? 'Your confirmed seat will be released for other passengers.' : `Your ${seatsOf(target)} confirmed seats will be released for other passengers.`) : 'Your request will be withdrawn.'} This can’t be undone.</p>
    </ConfirmDialog>}
  </div>;
}

/** "Showing 21–40 of 53 bookings." (or the whole count on a single page). */
export function pageSummary(offset: number, count: number, total: number): string {
  const noun = `booking${total === 1 ? '' : 's'}`;
  if (count === 0) return `${total} ${noun} in total.`;
  if (offset === 0 && count >= total) return `${total} ${noun}.`;
  return `Showing ${offset + 1}–${offset + count} of ${total} ${noun}.`;
}

function BookingCard({ booking, now, onCancel }: { booking: PassengerBooking; now: number; onCancel: () => void }) {
  const { ride } = booking;
  const state = rideState(booking, now);
  const seats = seatsOf(booking);
  return <article className="ride-card" aria-labelledby={`booking-${booking.id}-title`}>
    <div className="ride-card-main">
      <h4 id={`booking-${booking.id}-title`}>{ride.origin} <span aria-label="to">→</span> {ride.destination}</h4>
      <p className="ride-time"><Icon name="clock" size={14} /><time dateTime={ride.departure_time}>{formatDeparture(ride.departure_time)}</time></p>
      <dl className="ride-facts"><div><dt>Booking</dt><dd><span className={`status-pill ${booking.status}`}>{booking.status}</span></dd></div><div><dt>Ride</dt><dd><span className={`status-pill ${state.tone}`}>{state.label}</span></dd></div><div><dt>Seats</dt><dd className="seat-count">{seatCountLabel(seats)}</dd></div><div><dt>Fare</dt><dd>{seats === 1 ? formatFare(ride.fare_per_seat) : <>{formatFare(seats * ride.fare_per_seat)} <span>({seats} × {formatFare(ride.fare_per_seat)})</span></>}</dd></div></dl>
    </div>
    <div className="card-actions">
      <Link className="button button-outline small" to={`/passenger/rides/${ride.id}`} aria-label={`Details: ${ride.origin} to ${ride.destination}, ${formatDeparture(ride.departure_time)}`}>Details</Link>
      {canCancel(booking, now) && <button className="button button-outline small danger-outline" type="button" onClick={onCancel} aria-label={`Cancel booking: ${ride.origin} to ${ride.destination}, ${formatDeparture(ride.departure_time)}`}>Cancel</button>}
    </div>
  </article>;
}
