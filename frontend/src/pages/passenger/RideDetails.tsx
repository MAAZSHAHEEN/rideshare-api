import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation } from 'react-router-dom';
import { ApiError } from '../../api/client';
import { useIdempotencyKeys } from '../../api/idempotency';
import { MAX_SEATS_PER_BOOKING, seatCountLabel, seatsOf } from '../../api/types';
import type { Booking, PassengerBooking, Ride } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { Icon } from '../../components/Icon';
import { passengerErrorMessage } from '../../passenger/errors';
import { formatDeparture, formatFare, hasDeparted, seatsLabel } from '../../passenger/time';
import { usePassengerView } from './PassengerArea';

/** Why a ride can't be requested, or null when it can. Mirrors the server's checks plus AST-001's departure guard. */
export function unavailableReason(ride: Ride, now = Date.now()): string | null {
  if (ride.status === 'cancelled') return 'This ride was cancelled by the driver.';
  if (ride.status === 'completed') return 'This ride has been completed.';
  if (hasDeparted(ride.departure_time, now)) return 'This ride has already departed, so it can’t be booked.';
  if (ride.available_seats < 1) return 'This ride is full. There are no seats left to request.';
  return null;
}

export const MY_RIDE_BOOKINGS_LIMIT = 5;

/** Largest seat count the selector offers: what is left, capped at the server's per-booking maximum (0 when full). */
export function maxSelectableSeats(ride: Pick<Ride, 'available_seats'>): number {
  return Math.max(0, Math.min(ride.available_seats, MAX_SEATS_PER_BOOKING));
}

/**
 * Idempotency scope for one "request seats" action. The seat count is part of
 * it: PR #22 fingerprints `seats`, so retrying a failed 2-seat request after
 * switching to 3 must use a fresh key (reusing it would be a 422).
 */
export function bookScope(rideId: number, seats: number): string {
  return `book:${rideId}:${seats}`;
}

/** The caller's live (pending or accepted) booking for this ride, if the lookup returned one. */
export function liveBookingFor(bookings: PassengerBooking[] | undefined, rideId: number): PassengerBooking | undefined {
  return bookings?.find(b => b.ride_id === rideId && (b.status === 'pending' || b.status === 'accepted'));
}

/**
 * "Back to search" target. The search page passes its own query string in
 * navigation state; only a query string is accepted and it is appended to a
 * fixed path, so state can never point the link anywhere else.
 */
export function searchBackPath(back: unknown): string {
  if (typeof back === 'string' && back.length <= 600 && (back === '' || (back.startsWith('?') && !back.includes('#')))) return `/passenger/search${back}`;
  return '/passenger/search';
}

export function RideDetails({ rideId }: { rideId: number }) {
  const { api, user } = useAuth();
  const queryClient = useQueryClient();
  const location = useLocation();
  const back = searchBackPath((location.state as { back?: unknown } | null)?.back);
  const [now, setNow] = useState(() => Date.now());
  const [created, setCreated] = useState<Booking | null>(null);
  const [error, setError] = useState('');
  const [seatChoice, setSeatChoice] = useState(1);
  const errorRef = useRef<HTMLDivElement>(null);
  const successRef = useRef<HTMLDivElement>(null);
  usePassengerView('Ride details', 'ride-heading');
  // Re-evaluate "departed" while the page stays open.
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(timer); }, []);
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);
  useEffect(() => { if (created) successRef.current?.focus(); }, [created]);

  const valid = Number.isSafeInteger(rideId) && rideId > 0;
  const ride = useQuery({ queryKey: ['ride', user!.id, rideId], enabled: valid, queryFn: ({ signal }) => api.getRide(rideId, signal) });
  // G2: the caller's bookings for this ride only, newest first. At most one is
  // live (pending/accepted, partial unique index), so a handful covers history.
  const mine = useQuery({
    queryKey: ['my-bookings', user!.id, 'ride', rideId], enabled: valid,
    queryFn: ({ signal }) => api.myBookings(0, signal, { rideId, limit: MY_RIDE_BOOKINGS_LIMIT }),
  });
  const existing = liveBookingFor(mine.data, rideId);

  // A refetch can lower what is left; never offer or send more than that.
  const maxSeats = ride.data ? maxSelectableSeats(ride.data) : 0;
  const seats = Math.max(1, Math.min(seatChoice, maxSeats));

  const idempotency = useIdempotencyKeys();
  const book = useMutation({
    // The same key is reused if this request is retried after a network/429/5xx failure (PR #18).
    mutationFn: ({ key, seats }: { key: string; seats: number }) => api.bookRide(rideId, undefined, key, seats),
    onMutate: () => { setError(''); setCreated(null); },
    onSettled: (_, reason, { seats }) => idempotency.settle(bookScope(rideId, seats), reason ?? undefined),
    onSuccess: booking => {
      setCreated(booking);
      void queryClient.invalidateQueries({ queryKey: ['my-bookings'] });
      void queryClient.invalidateQueries({ queryKey: ['ride-search'] });
    },
    onError: reason => {
      // A 401 ends the session; the route guard then sends the visitor to sign in and back here.
      if (reason instanceof ApiError && reason.status === 401) return;
      setError(passengerErrorMessage(reason, 'book'));
      if (reason instanceof ApiError && (reason.status === 409 || reason.status === 404)) {
        void queryClient.invalidateQueries({ queryKey: ['ride', user!.id, rideId] });
        void queryClient.invalidateQueries({ queryKey: ['my-bookings'] });
      }
    },
  });

  function request() {
    if (book.isPending || !ride.data) return;
    const reason = unavailableReason(ride.data, Date.now());
    if (reason) { setNow(Date.now()); setError(reason); return; }
    book.mutate({ key: idempotency.keyFor(bookScope(rideId, seats)), seats });
  }

  const heading = <h1 id="ride-heading" tabIndex={-1}>{ride.data ? <>{ride.data.origin} <span aria-label="to">→</span> {ride.data.destination}</> : 'Ride details'}</h1>;
  if (!valid || (ride.error instanceof ApiError && ride.error.status === 404)) {
    return <div className="passenger-view"><Link className="back-link" to={back}>← Back to search</Link><div className="view-heading">{heading}</div>
      <div className="state-panel bordered"><h2 className="state-title">Ride not found</h2><p role="alert">{passengerErrorMessage(new ApiError(404, ''), 'load-ride')}</p><Link className="button small" to="/passenger/search">Search rides</Link></div></div>;
  }
  if (ride.isPending) return <div className="passenger-view"><Link className="back-link" to={back}>← Back to search</Link><div className="view-heading">{heading}</div><div className="state-panel bordered" role="status"><span className="spinner blue" aria-hidden="true" /><p>Loading ride details…</p></div></div>;
  if (ride.isError) return <div className="passenger-view"><Link className="back-link" to={back}>← Back to search</Link><div className="view-heading">{heading}</div><div className="state-panel bordered"><h2 className="state-title">We couldn’t load this ride.</h2><p role="alert">{passengerErrorMessage(ride.error, 'load-ride')}</p><button className="button small" onClick={() => void ride.refetch()}>Try again</button></div></div>;

  const data = ride.data;
  const departed = hasDeparted(data.departure_time, now);
  const reason = unavailableReason(data, now);
  const current = created ?? existing;
  const currentSeats = current ? seatsOf(current) : 1;
  return <div className="passenger-view">
    <Link className="back-link" to={back}>← Back to search</Link>
    <div className="view-heading"><span className="eyebrow">RIDE DETAILS</span>{heading}<p><time dateTime={data.departure_time}>{formatDeparture(data.departure_time)}</time></p></div>
    <section className="detail-card" aria-label="Ride summary">
      <dl className="detail-facts">
        <div><dt>Departure</dt><dd>{formatDeparture(data.departure_time)}{departed && <span className="status-pill departed">Departed</span>}</dd></div>
        <div><dt>Fare</dt><dd>{formatFare(data.fare_per_seat)} per seat</dd></div>
        <div><dt>Seats available</dt><dd>{seatsLabel(data.available_seats)}</dd></div>
        <div><dt>Ride status</dt><dd><span className={`status-pill ${data.status}`}>{data.status}</span></dd></div>
      </dl>
      <div className="booking-box">
        {created ? <div className="notice success" role="status" tabIndex={-1} ref={successRef}><strong>{currentSeats === 1 ? 'Seat requested.' : `${currentSeats} seats requested.`}</strong> Your booking is <span className="status-pill pending">pending</span> until the driver accepts or rejects it. <Link to="/passenger/bookings">View my bookings</Link></div>
          : current ? <div className="notice" role="status">You’ve already requested {currentSeats === 1 ? 'a seat' : `${currentSeats} seats`} on this ride. Status: <span className={`status-pill ${current.status}`}>{current.status}</span> <Link to="/passenger/bookings">Manage in My bookings</Link></div>
          : reason ? <div className="notice" role="status"><strong>Not available to book.</strong> {reason}</div>
          : <>
            <p className="booking-hint" id="seat-hint">Choose how many seats you need. The driver accepts or rejects the whole request. You can cancel from My bookings.</p>
            <div className="field seat-field">
              <label htmlFor="seat-count">Seats</label>
              <select id="seat-count" value={seats} aria-describedby="seat-hint seat-total" disabled={book.isPending}
                onChange={event => { setSeatChoice(Number(event.target.value)); setError(''); }}>
                {Array.from({ length: maxSeats }, (_, i) => i + 1).map(n => <option key={n} value={n}>{seatCountLabel(n)}</option>)}
              </select>
            </div>
            <p className="seat-total" id="seat-total" aria-live="polite">Total <strong>{formatFare(seats * data.fare_per_seat)}</strong>{seats > 1 && <> for {seatCountLabel(seats)} ({formatFare(data.fare_per_seat)} each)</>}</p>
          </>}
        {error && <div className="notice error" role="alert" tabIndex={-1} ref={errorRef}>{error}</div>}
        {!created && !current && !reason && <button className="button full-width" onClick={request} disabled={book.isPending}>
          {book.isPending ? <><span className="spinner" aria-hidden="true" />{seats === 1 ? 'Requesting seat…' : 'Requesting seats…'}</> : <>{seats === 1 ? 'Request a seat' : `Request ${seatCountLabel(seats)}`} <Icon name="arrow" size={17} /></>}
        </button>}
      </div>
    </section>
  </div>;
}
