import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import type { Ride } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { Icon } from '../../components/Icon';
import { driverErrorMessage } from '../../driver/errors';
import type { RideFilter } from '../../driver/state';
import { FILTER_LABELS, RIDE_FILTERS, rideFilterOf } from '../../driver/state';
import { formatDeparture, formatFare, seatsLabel } from '../../passenger/time';
import { useDriverView } from '../../driver/useDriverView';
import { useState } from 'react';

export const RIDES_PAGE = 20;
// Upcoming and departed are both "active" on the server and are split here by
// departure time, so those tabs read a larger page of active rides.
export const ACTIVE_PAGE = 100;
const FILTER_HELP: Record<RideFilter | 'all', string> = {
  all: 'Every ride you’ve published, latest departure first.',
  upcoming: 'Active rides that haven’t departed yet, soonest first. You can accept requests on these.',
  departed: 'Active rides whose departure time has passed. Requests can’t be accepted; mark them completed when done.',
  completed: 'Rides you marked as completed.',
  cancelled: 'Rides you cancelled. Their requests were cancelled too.',
};

export function MyRides() {
  const { api, user } = useAuth();
  const [params, setParams] = useSearchParams();
  const raw = params.get('filter');
  const filter = RIDE_FILTERS.includes(raw as RideFilter) ? raw as RideFilter : null;
  const page = Math.max(0, Math.min(10_000, Number.parseInt(params.get('page') ?? '0', 10) || 0));
  const derived = filter === 'upcoming' || filter === 'departed';
  const limit = derived ? ACTIVE_PAGE : RIDES_PAGE;
  const [now] = useState(() => Date.now());
  useDriverView('My rides', 'rides-heading');

  const query = useQuery({
    queryKey: ['my-rides', user!.id, 'list', filter ?? 'all', page],
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) => api.myRides(page * limit, signal, {
      limit, status: derived ? 'active' : filter === 'completed' || filter === 'cancelled' ? filter : undefined,
    }),
  });
  const fetched = query.data ?? [];
  let rides: Ride[] = derived ? fetched.filter(ride => rideFilterOf(ride, now) === filter) : fetched;
  if (filter === 'upcoming') rides = [...rides].sort((a, b) => Date.parse(a.departure_time) - Date.parse(b.departure_time) || a.id - b.id);

  function select(next: RideFilter | null) {
    setParams(next ? new URLSearchParams({ filter: next }) : new URLSearchParams());
  }
  function goToPage(next: number) {
    const nextParams = new URLSearchParams(params);
    if (next > 0) nextParams.set('page', String(next)); else nextParams.delete('page');
    setParams(nextParams);
  }

  const title = filter ? `${FILTER_LABELS[filter]} rides` : 'All rides';
  return <div className="passenger-view driver-view">
    <div className="view-heading"><span className="eyebrow">MY RIDES</span><h1 id="rides-heading" tabIndex={-1}>Rides you’re driving</h1><p>Open a ride to answer seat requests. Times are in Pakistan time (PKT).</p></div>
    <div className="driver-toolbar"><nav className="status-tabs" aria-label="Filter rides">
      {[null, ...RIDE_FILTERS].map(option => <button key={option ?? 'all'} type="button" aria-pressed={filter === option} className={filter === option ? 'selected' : ''} onClick={() => select(option)}>{option ? FILTER_LABELS[option] : 'All'}</button>)}
    </nav><Link className="button small" to="/driver/rides/new">Publish a ride</Link></div>
    <section className="results-panel" aria-labelledby="rides-list-heading" aria-busy={query.isFetching}>
      <div className="history-heading"><div><h2 id="rides-list-heading">{title}</h2><p>{FILTER_HELP[filter ?? 'all']}</p></div>
        <button className="button button-outline small" onClick={() => void query.refetch()} disabled={query.isFetching}>{query.isFetching ? 'Refreshing…' : 'Refresh'}</button></div>
      {query.isPending ? <div className="state-panel" role="status"><span className="spinner blue" aria-hidden="true" /><p>Loading your rides…</p></div>
        : query.isError ? <div className="state-panel"><h3>We couldn’t load your rides.</h3><p role="alert">{driverErrorMessage(query.error, 'load-rides')}</p><button className="button small" onClick={() => void query.refetch()}>Try again</button></div>
        : rides.length === 0 ? <div className="state-panel"><span className="empty-illustration"><Icon name="car" size={34} /></span><h3>{page ? 'No more rides.' : filter ? `No ${FILTER_LABELS[filter].toLowerCase()} rides.` : 'No rides yet.'}</h3><p>{page ? 'Go back to see earlier rides.' : 'Publish a scheduled ride and passengers can request seats.'}</p>{!page && <Link className="button small" to="/driver/rides/new">Publish a ride</Link>}</div>
        : <ul className="ride-list">{rides.map(ride => <li key={ride.id}><DriverRideCard ride={ride} now={now} /></li>)}</ul>}
      <div className="pagination"><span>Page {page + 1}</span><div><button disabled={page === 0 || query.isFetching} onClick={() => goToPage(page - 1)}>← Previous</button><button disabled={fetched.length < limit || query.isFetching || query.isError} onClick={() => goToPage(page + 1)}>Next →</button></div></div>
    </section>
  </div>;
}

function DriverRideCard({ ride, now }: { ride: Ride; now: number }) {
  const state = rideFilterOf(ride, now);
  const name = `${ride.origin} to ${ride.destination}, ${formatDeparture(ride.departure_time)}`;
  // The accessible name starts with the visible text (WCAG 2.5.3).
  const label = state === 'upcoming' ? 'Manage requests' : 'View ride';
  return <article className="ride-card" aria-labelledby={`driver-ride-${ride.id}-title`}>
    <div className="ride-card-main">
      <h3 id={`driver-ride-${ride.id}-title`}>{ride.origin} <span aria-label="to">→</span> {ride.destination}</h3>
      <p className="ride-time"><Icon name="clock" size={14} /><time dateTime={ride.departure_time}>{formatDeparture(ride.departure_time)}</time></p>
      <dl className="ride-facts"><div><dt>Ride</dt><dd><span className={`status-pill ${state}`}>{FILTER_LABELS[state]}</span></dd></div><div><dt>Seats</dt><dd>{seatsLabel(ride.available_seats)}</dd></div><div><dt>Fare</dt><dd>{formatFare(ride.fare_per_seat)} <span>per seat</span></dd></div></dl>
    </div>
    <Link className="button button-outline small" to={`/driver/rides/${ride.id}`} aria-label={`${label}: ${name}`}>{label}</Link>
  </article>;
}
