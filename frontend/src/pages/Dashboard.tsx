import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { dashboardPath, useAuth } from '../auth/AuthProvider';
import { Icon } from '../components/Icon';
import type { Ride } from '../api/types';

export function Dashboard() {
  const { user, api, logout } = useAuth();
  const [offset, setOffset] = useState(0);
  const driver = user!.role === 'driver';
  const query = useQuery({
    queryKey: [driver ? 'my-rides' : 'my-bookings', user!.id, offset],
    queryFn: async ({ signal }) => driver
      ? (await api.myRides(offset, signal)).map(ride => ({ id: ride.id, ride, status: ride.status }))
      : (await api.myBookings(offset, signal)).map(booking => ({ id: booking.id, ride: booking.ride, status: booking.status })),
  });
  const rows = query.data ?? [];
  return <div className="dashboard-shell container"><aside className="dashboard-sidebar"><span className="sidebar-caption">YOUR SPACE</span><div className="role-label"><span className="icon-tile"><Icon name={driver ? 'car' : 'bag'} /></span><strong>{driver ? 'Driver' : 'Passenger'} dashboard</strong></div><nav aria-label="Dashboard navigation"><Link className="sidebar-active" aria-current="page" to={dashboardPath(user!)}><Icon name="grid" size={19} />Overview</Link><a href="#history"><Icon name="road" size={19} />{driver ? 'My rides' : 'My bookings'}</a><Link to="/account"><Icon name="user" size={19} />My account</Link></nav><div className="sidebar-bottom"><span className="avatar">{Array.from(user!.name)[0]?.toUpperCase() || 'R'}</span><div><strong>{user!.name}</strong><span>{driver ? 'Sharing the road' : 'Enjoying the journey'}</span></div><button aria-label="Log out" className="icon-button" onClick={logout}><Icon name="logout" size={19} /></button></div></aside>
    <div className="dashboard-content"><div className="dashboard-heading"><div><span className="eyebrow">{driver ? 'IN THE DRIVER’S SEAT' : 'YOUR JOURNEY, YOUR SPACE'}</span><h1>Welcome, {user!.name.split(' ')[0] || 'traveller'}.</h1><p>{driver ? 'Your rides. Your plans. All in one place.' : 'A little less planning. A little more looking forward.'}</p></div><span className="account-badge"><span className="little-dot" />{driver ? 'Driver account' : 'Passenger account'}</span></div>
      <div className="dashboard-banner"><div><span className="eyebrow">{driver ? 'MAKE ROOM FOR POSSIBILITY' : 'GOOD THINGS ARE AHEAD'}</span><h2>{driver ? 'Every shared seat\nis a new connection.' : 'Your next chapter\nstarts with a journey.'}</h2><p>{driver ? 'Keep your ride history close at hand.' : 'Keep an eye on the journeys you’ve requested.'}</p></div><img src="/journey.svg" alt="" /></div>
      <section className="history-panel" id="history"><div className="history-heading"><div><h2>{driver ? 'My rides' : 'My bookings'}</h2><p>Current plans and past journeys, together.</p></div><button className="button button-outline small" onClick={() => void query.refetch()} disabled={query.isFetching}>{query.isFetching ? 'Refreshing…' : 'Refresh'}</button></div>
        {query.isPending ? <div className="state-panel" role="status"><span className="spinner blue" /><p>Loading your journeys…</p></div> : query.isError ? <div className="state-panel"><span className="icon-tile"><Icon name="road" /></span><h3>We couldn’t load your journeys.</h3><p role="alert">{query.error.message}</p><button className="button small" onClick={() => void query.refetch()}>Try again</button></div> : rows.length === 0 ? <div className="state-panel"><span className="empty-illustration"><Icon name={driver ? 'car' : 'bag'} size={34} /></span><h3>{offset ? 'You’ve reached the end.' : driver ? 'Your road ahead is open.' : 'Your journey is just beginning.'}</h3><p>{offset ? 'Go back to see your earlier results.' : driver ? 'When you create a ride, it will appear here.' : 'When you request a booking, you’ll find it here.'}</p><span className="state-footnote">{!offset && 'A little company. A better journey.'}</span></div> : <div className="journey-list">{rows.map(row => <JourneyRow key={row.id} ride={row.ride} status={row.status} driver={driver} />)}</div>}
        <div className="pagination"><span>Page {offset / 20 + 1}{!query.isPending && !query.isError ? ` · ${rows.length} ${driver ? 'ride' : 'booking'}${rows.length === 1 ? '' : 's'} shown` : ''}</span><div><button disabled={offset === 0 || query.isFetching} onClick={() => setOffset(Math.max(0, offset - 20))}>← Previous</button><button disabled={rows.length < 20 || query.isFetching || query.isError} onClick={() => setOffset(offset + 20)}>Next →</button></div></div>
      </section>
    </div>
  </div>;
}

function JourneyRow({ ride, status, driver }: { ride: Ride; status: string; driver: boolean }) {
  return <article className="journey-row"><span className="icon-tile"><Icon name="road" size={21} /></span><div className="journey-route"><h3>{ride.origin} <span aria-label="to">→</span> {ride.destination}</h3><p><Icon name="clock" size={14} /><time dateTime={ride.departure_time}>{new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(ride.departure_time))}</time></p>{!driver && <span className="ride-state">Ride: {ride.status}</span>}</div><div className="journey-status"><span className={`status-pill ${status}`}>{status}</span>{driver && <span>{ride.available_seats} seats remaining</span>}</div></article>;
}

export function Account() {
  const { user, logout } = useAuth();
  return <section className="container account-page"><Link className="back-link" to={user!.role === 'admin' ? '/' : dashboardPath(user!)}>← {user!.role === 'admin' ? 'Home' : 'Back to dashboard'}</Link><span className="eyebrow">YOUR RIDESHARE ACCOUNT</span><h1>A little about you.</h1><p>Your account details, verified by RideShare.</p><div className="account-card"><span className="avatar large"><Icon name="user" size={30} /></span><dl><div><dt>Name</dt><dd>{user!.name}</dd></div><div><dt>Email</dt><dd>{user!.email}</dd></div><div><dt>Account type</dt><dd className="capitalize">{user!.role}</dd></div></dl><button className="button button-outline" onClick={logout}>Log out <Icon name="logout" size={17} /></button></div>{user!.role === 'admin' && <p className="notice">Passenger and driver dashboards are reserved for those account roles. This frontend does not provide an admin console.</p>}</section>;
}
