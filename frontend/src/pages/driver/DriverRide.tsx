import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation } from 'react-router-dom';
import { ApiError } from '../../api/client';
import { useIdempotencyKeys } from '../../api/idempotency';
import type { DriverBooking, Ride } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { driverErrorMessage } from '../../driver/errors';
import type { DriverAction } from '../../driver/errors';
import { BOOKING_HELP, BOOKING_LABELS, BOOKING_ORDER, FILTER_LABELS, acceptBlockedReason, canCancelRide, canCompleteRide, canReject, rideFilterOf } from '../../driver/state';
import { formatDeparture, formatFare, seatsLabel } from '../../passenger/time';
import { useDriverView } from '../../driver/useDriverView';
import { useRideBookingCounts } from '../../driver/useRideBookingCounts';

export const REQUESTS_LIMIT = 100;
type Pending = { kind: 'reject'; booking: DriverBooking } | { kind: 'cancel-ride' } | { kind: 'complete-ride' };

export function DriverRide({ rideId }: { rideId: number }) {
  const { api, user } = useAuth();
  const queryClient = useQueryClient();
  const location = useLocation();
  const [published] = useState(() => Boolean((location.state as { published?: unknown } | null)?.published));
  const [now, setNow] = useState(() => Date.now());
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState<Pending | null>(null);
  const [dialogError, setDialogError] = useState('');
  const errorRef = useRef<HTMLDivElement>(null);
  const noticeRef = useRef<HTMLDivElement>(null);
  useDriverView('Manage ride', 'driver-ride-heading');
  // "Departed" is derived from the clock; re-evaluate while the page stays open.
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(timer); }, []);
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);
  useEffect(() => { if (notice) noticeRef.current?.focus(); }, [notice]);

  const valid = Number.isSafeInteger(rideId) && rideId > 0;
  const rideKey = ['ride', user!.id, rideId];
  const requestsKey = ['ride-bookings', user!.id, rideId];
  const ride = useQuery({ queryKey: rideKey, enabled: valid, queryFn: ({ signal }) => api.getRide(rideId, signal) });
  const owner = ride.data?.driver_id === user!.id;
  const requests = useQuery({
    queryKey: requestsKey, enabled: valid && owner, refetchInterval: 30_000,
    queryFn: ({ signal }) => api.rideBookingsPage(rideId, signal, { limit: REQUESTS_LIMIT }),
  });
  // D2: server counts stay exact even when the ride has more than REQUESTS_LIMIT requests.
  const counts = useRideBookingCounts(valid && owner ? [rideId] : []);

  /** After any change or conflict, reload the ride, its requests and the ride lists from the server. */
  function refresh() {
    void queryClient.invalidateQueries({ queryKey: rideKey });
    void queryClient.invalidateQueries({ queryKey: requestsKey });
    void queryClient.invalidateQueries({ queryKey: ['my-rides'] });
    void queryClient.invalidateQueries({ queryKey: ['booking-counts'] });
  }
  function failed(reason: unknown, action: DriverAction, inDialog: boolean) {
    if (reason instanceof ApiError && reason.status === 401) return; // the session ends; the guard sends the driver to sign in
    const message = driverErrorMessage(reason, action);
    if (inDialog) setDialogError(message); else setError(message);
    if (reason instanceof ApiError && (reason.status === 409 || reason.status === 404 || reason.status === 403)) refresh();
  }

  const idempotency = useIdempotencyKeys();
  const respondScope = (booking: DriverBooking, accept: boolean) => `respond:${booking.id}:${accept ? 'accept' : 'reject'}`;
  const respond = useMutation({
    // One key per accept/reject action, reused if that action is retried after a network/429/5xx failure (PR #18).
    mutationFn: ({ booking, accept }: { booking: DriverBooking; accept: boolean }) =>
      api.respondToBooking(booking.id, accept, undefined, idempotency.keyFor(respondScope(booking, accept))),
    onMutate: () => { setError(''); setNotice(''); setDialogError(''); },
    onSettled: (_, reason, { booking, accept }) => idempotency.settle(respondScope(booking, accept), reason ?? undefined),
    onSuccess: (_, { booking, accept }) => {
      setConfirm(null);
      setNotice(accept ? `Accepted ${booking.passenger.name}’s request. One seat is now taken.` : `Rejected ${booking.passenger.name}’s request.`);
      refresh();
    },
    onError: (reason, { accept }) => failed(reason, accept ? 'accept' : 'reject', !accept),
  });
  const rideAction = useMutation({
    mutationFn: (kind: 'cancel-ride' | 'complete-ride') => kind === 'cancel-ride' ? api.cancelRide(rideId) : api.completeRide(rideId),
    onMutate: () => { setError(''); setNotice(''); setDialogError(''); },
    onSuccess: (_, kind) => {
      setConfirm(null);
      setNotice(kind === 'cancel-ride' ? 'Ride cancelled. Its requests were cancelled and passengers will see it as cancelled.' : 'Ride marked as completed. Any pending requests were cancelled.');
      refresh();
    },
    onError: (reason, kind) => failed(reason, kind, true),
  });
  const busy = respond.isPending || rideAction.isPending;

  function accept(booking: DriverBooking, current: Ride) {
    if (busy) return;
    const blocked = acceptBlockedReason(current, Date.now());
    if (blocked) { setNow(Date.now()); setNotice(''); setError(blocked); return; }
    respond.mutate({ booking, accept: true });
  }

  const heading = <h1 id="driver-ride-heading" tabIndex={-1}>{ride.data ? <>{ride.data.origin} <span aria-label="to">→</span> {ride.data.destination}</> : 'Manage ride'}</h1>;
  const back = <Link className="back-link" to="/driver/rides">← My rides</Link>;
  if (!valid || (ride.error instanceof ApiError && ride.error.status === 404)) {
    return <div className="passenger-view driver-view">{back}<div className="view-heading">{heading}</div>
      <div className="state-panel bordered"><h2 className="state-title">Ride not found</h2><p role="alert">{driverErrorMessage(new ApiError(404, ''), 'load-ride')}</p><Link className="button small" to="/driver/rides">Back to my rides</Link></div></div>;
  }
  if (ride.isPending) return <div className="passenger-view driver-view">{back}<div className="view-heading">{heading}</div><div className="state-panel bordered" role="status"><span className="spinner blue" aria-hidden="true" /><p>Loading ride…</p></div></div>;
  if (ride.isError) return <div className="passenger-view driver-view">{back}<div className="view-heading">{heading}</div><div className="state-panel bordered"><h2 className="state-title">We couldn’t load this ride.</h2><p role="alert">{driverErrorMessage(ride.error, 'load-ride')}</p><button className="button small" onClick={() => void ride.refetch()}>Try again</button></div></div>;

  const data = ride.data;
  if (!owner) {
    // UI courtesy only: the API refuses another driver's requests and actions with 403.
    return <div className="passenger-view driver-view">{back}<div className="view-heading">{heading}</div>
      <div className="state-panel bordered"><h2 className="state-title">Not your ride</h2><p role="alert">This ride belongs to another driver. Only its driver can see its requests or change it.</p><Link className="button small" to="/driver/rides">Back to my rides</Link></div></div>;
  }

  const state = rideFilterOf(data, now);
  const blocked = acceptBlockedReason(data, now);
  const list = requests.data?.items ?? [];
  const requestTotal = requests.data?.total ?? null;
  const serverCounts = counts.data?.get(rideId);
  const pendingCount = serverCounts?.pending ?? list.filter(b => b.status === 'pending').length;
  const acceptedCount = serverCounts?.accepted ?? list.filter(b => b.status === 'accepted').length;
  const groups = BOOKING_ORDER.map(status => [status, list.filter(b => b.status === status)] as const).filter(([, items]) => items.length > 0);
  return <div className="passenger-view driver-view">
    {back}
    <div className="view-heading"><span className="eyebrow">MANAGE RIDE</span>{heading}<p><time dateTime={data.departure_time}>{formatDeparture(data.departure_time)}</time></p></div>
    {published && <div className="notice success" role="status"><strong>Ride published.</strong> Passengers searching this route and date can now request seats.</div>}
    <section className="detail-card" aria-label="Ride summary">
      <dl className="detail-facts">
        <div><dt>Departure</dt><dd>{formatDeparture(data.departure_time)}</dd></div>
        <div><dt>Ride status</dt><dd><span className={`status-pill ${state}`}>{FILTER_LABELS[state]}</span></dd></div>
        <div><dt>Seats left</dt><dd>{seatsLabel(data.available_seats)}</dd></div>
        <div><dt>Fare</dt><dd>{formatFare(data.fare_per_seat)} per seat</dd></div>
      </dl>
      {(canCancelRide(data) || canCompleteRide(data, now)) && <div className="booking-box ride-actions">
        {canCompleteRide(data, now) && <button className="button button-outline small" type="button" disabled={busy} onClick={() => { setDialogError(''); setConfirm({ kind: 'complete-ride' }); }}>Mark ride completed</button>}
        {canCancelRide(data) && <button className="button button-outline small danger-outline" type="button" disabled={busy} onClick={() => { setDialogError(''); setConfirm({ kind: 'cancel-ride' }); }}>Cancel ride</button>}
        {state === 'upcoming' && <p className="field-help">You can mark the ride completed after its departure time.</p>}
      </div>}
    </section>

    <section className="results-panel" aria-labelledby="requests-heading" aria-busy={requests.isFetching}>
      <div className="history-heading"><div><h2 id="requests-heading">Seat requests</h2><p>{requests.isSuccess ? `${pendingCount} pending · ${acceptedCount} accepted` : 'Requests from passengers for this ride.'}</p></div>
        <button className="button button-outline small" onClick={() => refresh()} disabled={requests.isFetching}>{requests.isFetching ? 'Refreshing…' : 'Refresh'}</button></div>
      {notice && <div className="notice success panel-notice" role="status" tabIndex={-1} ref={noticeRef}>{notice}</div>}
      {error && <div className="notice error panel-notice" role="alert" tabIndex={-1} ref={errorRef}>{error}</div>}
      {pendingCount > 0 && blocked && <div className="notice panel-notice" role="status"><strong>Accepting is paused.</strong> {blocked}</div>}
      {requests.isPending ? <div className="state-panel" role="status"><span className="spinner blue" aria-hidden="true" /><p>Loading requests…</p></div>
        : requests.isError ? <div className="state-panel"><h3>We couldn’t load the requests.</h3><p role="alert">{driverErrorMessage(requests.error, 'load-requests')}</p><button className="button small" onClick={() => void requests.refetch()}>Try again</button></div>
        : list.length === 0 ? <div className="state-panel"><h3>No requests yet.</h3><p>{state === 'upcoming' ? 'When passengers request a seat, they’ll appear here. This list refreshes every 30 seconds.' : 'This ride received no seat requests.'}</p></div>
        : <div className="booking-groups">{groups.map(([group, items]) => <section key={group} className="booking-group" aria-labelledby={`requests-${group}`}>
          <h3 id={`requests-${group}`}>{BOOKING_LABELS[group]} <span className="group-count">({items.length})</span></h3>
          <p className="group-help">{BOOKING_HELP[group]}</p>
          <ul className="ride-list">{items.map(booking => <li key={booking.id}><article className="ride-card request-card" aria-labelledby={`request-${booking.id}`}>
            <div className="ride-card-main"><h4 id={`request-${booking.id}`}>{booking.passenger.name}</h4>
              <dl className="ride-facts"><div><dt>Request</dt><dd><span className={`status-pill ${booking.status}`}>{booking.status}</span></dd></div><div><dt>Seats</dt><dd>1</dd></div></dl></div>
            {booking.status === 'pending' && <div className="card-actions">
              {!blocked && <button className="button small" type="button" disabled={busy} onClick={() => accept(booking, data)} aria-label={`Accept request from ${booking.passenger.name}`}>{respond.isPending && respond.variables?.booking.id === booking.id && respond.variables.accept ? 'Accepting…' : 'Accept'}</button>}
              {canReject(data) && <button className="button button-outline small danger-outline" type="button" disabled={busy} onClick={() => { setDialogError(''); setConfirm({ kind: 'reject', booking }); }} aria-label={`Reject request from ${booking.passenger.name}`}>Reject</button>}
            </div>}
          </article></li>)}</ul>
        </section>)}</div>}
      {list.length > 0 && (requestTotal !== null ? requestTotal > list.length : list.length >= REQUESTS_LIMIT) && <p className="field-help panel-footnote">Showing the newest {list.length} of {requestTotal ?? `${REQUESTS_LIMIT}+`} requests.</p>}
    </section>

    {confirm?.kind === 'reject' && <ConfirmDialog title="Reject this request?" confirmLabel="Reject request" cancelLabel="Keep request" busy={busy} error={dialogError}
      onCancel={() => { if (!busy) setConfirm(null); }} onConfirm={() => respond.mutate({ booking: confirm.booking, accept: false })}>
      <p>{confirm.booking.passenger.name} asked for a seat on {data.origin} → {data.destination}, {formatDeparture(data.departure_time)}.</p>
      <p>They’ll see the request as rejected. This can’t be undone.</p>
    </ConfirmDialog>}
    {confirm?.kind === 'cancel-ride' && <ConfirmDialog title="Cancel this ride?" confirmLabel="Cancel ride" cancelLabel="Keep ride" busy={busy} error={dialogError}
      onCancel={() => { if (!busy) setConfirm(null); }} onConfirm={() => rideAction.mutate('cancel-ride')}>
      <p>{data.origin} → {data.destination}, {formatDeparture(data.departure_time)}.</p>
      <p>{requests.isSuccess ? `${acceptedCount} accepted passenger${acceptedCount === 1 ? '' : 's'} and ${pendingCount} pending request${pendingCount === 1 ? '' : 's'} will be cancelled.` : 'Every accepted and pending request will be cancelled.'} This can’t be undone.</p>
    </ConfirmDialog>}
    {confirm?.kind === 'complete-ride' && <ConfirmDialog title="Mark this ride completed?" confirmLabel="Mark completed" cancelLabel="Not yet" busy={busy} error={dialogError}
      onCancel={() => { if (!busy) setConfirm(null); }} onConfirm={() => rideAction.mutate('complete-ride')}>
      <p>{data.origin} → {data.destination}, {formatDeparture(data.departure_time)}.</p>
      <p>{pendingCount ? `${pendingCount} pending request${pendingCount === 1 ? '' : 's'} will be cancelled. ` : ''}Accepted seats stay as they are. This can’t be undone.</p>
    </ConfirmDialog>}
  </div>;
}
