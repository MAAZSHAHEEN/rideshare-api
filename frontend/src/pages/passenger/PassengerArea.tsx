import { useEffect } from 'react';
import { Navigate, useParams, useSearchParams } from 'react-router-dom';
import { useAuth } from '../../auth/AuthProvider';
import { Dashboard, DashboardShell } from '../Dashboard';
import { MyBookings } from './MyBookings';
import { RideDetails } from './RideDetails';
import { SearchRides } from './SearchRides';

export type PassengerView = 'overview' | 'search' | 'bookings' | 'ride';

/**
 * Passenger journey views are real paths, like the driver's:
 * /passenger, /passenger/search, /passenger/bookings and /passenger/rides/:id.
 * Reloads and shared links work because the backend serves index.html for
 * /passenger/* (G1, frontend_serving.CLIENT_AREAS). Links from the earlier
 * query-parameter views (/passenger?view=…, /passenger?ride=…) are redirected.
 */
export function PassengerArea({ view }: { view: PassengerView }) {
  const [params] = useSearchParams();
  const { rideId } = useParams();
  const { selectMode } = useAuth();
  useEffect(() => { selectMode('passenger'); }, [selectMode]);
  if (view === 'overview') {
    const legacy = legacyPassengerPath(params);
    if (legacy) return <Navigate to={legacy} replace />;
    return <Dashboard key="passenger" mode="passenger" />;
  }
  if (view === 'ride') {
    const id = rideId && /^[1-9]\d{0,9}$/.test(rideId) ? Number(rideId) : NaN;
    return <DashboardShell mode="passenger" active="search"><RideDetails key={rideId} rideId={id} /></DashboardShell>;
  }
  if (view === 'search') return <DashboardShell mode="passenger" active="search"><SearchRides /></DashboardShell>;
  return <DashboardShell mode="passenger" active="bookings"><MyBookings /></DashboardShell>;
}

const SEARCH_KEYS = ['from', 'to', 'date', 'page'];
const BOOKING_KEYS = ['status', 'page'];

/**
 * Where an old query-parameter passenger link now lives, or null when the URL
 * carries no legacy view. Only known parameters are carried over; the target
 * is always a fixed in-app path, so nothing in the query can redirect elsewhere.
 */
export function legacyPassengerPath(params: URLSearchParams): string | null {
  const keep = (keys: string[]) => {
    const next = new URLSearchParams();
    for (const key of keys) { const value = params.get(key); if (value !== null) next.set(key, value); }
    const query = next.toString();
    return query ? `?${query}` : '';
  };
  const ride = params.get('ride');
  if (ride !== null) return `/passenger/rides/${/^[1-9]\d{0,9}$/.test(ride) ? ride : '0'}`;
  const view = params.get('view');
  if (view === 'search') return `/passenger/search${keep(SEARCH_KEYS)}`;
  if (view === 'bookings') return `/passenger/bookings${keep(BOOKING_KEYS)}`;
  if (view !== null) return '/passenger';
  return null;
}

/**
 * Sets the document title and moves focus to the view heading when a passenger
 * view opens. Views are separate paths, so the layout's own route effect (which
 * focuses <main> and sets a generic title) runs after ours in the same commit:
 * both steps wait one task so they land last (same as useDriverView).
 */
export function usePassengerView(title: string, headingId: string) {
  useEffect(() => {
    const timer = setTimeout(() => {
      document.title = `${title} | RideShare`;
      document.getElementById(headingId)?.focus({ preventScroll: true });
    }, 0);
    return () => clearTimeout(timer);
  }, [title, headingId]);
}
